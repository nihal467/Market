"""Paper-trading bot: simulate investing a virtual Rs 5,00,000 using the daily
signals, mark to market after each market close, log daily P&L, and persist
everything to the data store (data branch).

Strategy (transparent, rule-based — NOT advice):
  - Start with Rs 5,00,000 in virtual cash on inception.
  - Each trading day, after close, read the latest end-of-day analysis
    (data/daily/latest.json) which holds a BUY/HOLD/SELL signal + score +
    close price for every Top-50 watchlist stock.
  - Incubation profile: rank by 3-month momentum, buy the top TOP_N names, and
    only rebalance weekly. Existing positions are held until they fall outside
    HOLD_UNTIL_RANK, which reduces churn during the one-month dummy run.
  - NEXT-OPEN execution (no look-ahead): signals computed from day T's close
    are queued as ``pending_orders`` and FILLED at day T+1's adjusted open.
    Each EOD run therefore (a) fills yesterday's queue at today's open,
    (b) marks the book at today's close, (c) queues fresh orders for the next
    session. Fills pay slippage (SLIPPAGE_BPS against the trade) plus
    per-side transaction costs (COST_BPS_PER_SIDE) so returns are not
    overstated; cumulative cost drag is tracked in ``total_costs``.
  - Circuit-breaker: while the book is more than MAX_DRAWDOWN_PAUSE_PCT below
    its peak, no new buys are queued (protective sells still are).
  - Mark the book to market at the day's close, record the day's P&L, and
    append a dated history row.

State files (on the data branch):
  paper/state.json    -> full state: cash, positions, pending_orders, history
  paper/latest.json   -> compact view for the dashboard (today + equity curve)
  paper/history.jsonl -> one appended line per trading day

Idempotent per date: a second run on the same trading date is a no-op, so the
twice-daily Daily Analysis workflow won't double-trade.
"""
from __future__ import annotations

import sys
from datetime import date, datetime

import datastore as ds
from market_calendar import MARKET_CLOSE, now_ist
from strategy_config import (
    ACTIVE_PROFILE,
    COST_BPS_PER_SIDE,
    COST_PER_SIDE,
    EXECUTION_MODEL,
    HOLD_UNTIL_RANK,
    INTRADAY_SHOCK_BOOK_LOSS_PCT,
    INTRADAY_SHOCK_INDEX_DROP_PCT,
    INTRADAY_SHOCK_WATCHLIST_DOWN_FRACTION,
    INTRADAY_SHOCK_WATCHLIST_MEDIAN_DROP_PCT,
    INTRADAY_WEAK_HOLDING_DAY_DROP_PCT,
    INTRADAY_WEAK_HOLDING_LOSS_PCT,
    MAX_DAILY_LOSS_PCT,
    MAX_DRAWDOWN_PAUSE_PCT,
    MAX_DRAWDOWN_PCT,
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    REBALANCE_INTERVAL,
    SLIPPAGE_BPS,
    START_CAPITAL,
    STOP_LOSS_PCT,
    TOP_N,
    TRAILING_STOP_PCT,
    USE_MARKET_REGIME_GUARD,
    strategy_metadata,
)

BENCHMARK = "^NSEI"      # NIFTY 50 — the "do nothing, just hold the index" baseline

STATE_FILE = "paper/state.json"
LATEST_FILE = "paper/latest.json"
HISTORY_FILE = "paper/history.jsonl"


def _empty_state() -> dict:
    return {
        "inception": None,
        "start_capital": START_CAPITAL,
        "cash": START_CAPITAL,
        "positions": {},   # symbol -> {qty, avg_price, name}
        "last_date": None,
        "history": [],     # list of daily snapshots (also mirrored to jsonl)
        # Orders decided from today's close, to be FILLED at the next session's
        # open (next-open execution — no look-ahead). Old states without this
        # key migrate gracefully via _load_state's backfill.
        "pending_orders": [],
        # Cumulative transaction costs + slippage-inclusive cost drag (Rs).
        "total_costs": 0.0,
        # All-time peak book value, for the drawdown circuit-breaker.
        "peak_value": None,
        # Benchmark anchor: NIFTY level on the first day we could read it, so we
        # can compare the bot against simply holding the index from day one.
        "benchmark_inception": None,        # index level at anchor
        "benchmark_inception_date": None,   # date that anchor was set
    }


def _normalize_history(history: list[dict], inception: str | None = None) -> list[dict]:
    """Deduplicate by trading date and return rows in chronological order."""
    by_date = {
        row.get("date"): row for row in history
        if row.get("date") and (inception is None or row.get("date") >= inception)
    }
    return [by_date[d] for d in sorted(by_date)]


def _benchmark_level() -> float | None:
    """Latest NIFTY 50 closing level, or None if it can't be fetched.

    Wrapped defensively: a benchmark hiccup must never break the actual paper
    trade. yfinance is imported lazily so module import stays cheap.
    """
    try:
        import yfinance as yf

        data = yf.download(BENCHMARK, period="5d", interval="1d",
                           auto_adjust=True, progress=False)
        if data is None or data.empty:
            return None
        close = data["Close"]
        # Single-ticker downloads can come back as a 1-column DataFrame; collapse
        # to a Series so .iloc[-1] is a scalar, not a 1-element Series.
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        close = close.dropna()
        if close.empty:
            return None
        return float(close.iloc[-1])
    except Exception as exc:  # noqa: BLE001 — benchmark is best-effort only
        print(f"  ! benchmark fetch failed: {exc}")
        return None


def _fetch_opens(symbols: list[str], trading_date: str) -> dict[str, float]:
    """Adjusted OPEN price per symbol for the given trading date.

    Used to fill orders queued at the previous session's close at TODAY's open
    (the honest "signal at close, fill at next open" model). Symbols with no
    bar for the date are simply absent from the result — their orders get
    dropped with a note. Retries with exponential backoff like intraday.py.
    yfinance is imported lazily so tests can stub this without network access.
    """
    if not symbols:
        return {}
    out: dict[str, float] = {}
    try:
        import time as _time

        import pandas as pd
        import yfinance as yf

        data = None
        for attempt in range(1, 4):
            try:
                data = yf.download(sorted(symbols), period="5d", interval="1d",
                                   auto_adjust=True, progress=False,
                                   group_by="ticker", threads=True)
                if data is not None and not data.empty:
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"  ! open-price download attempt {attempt}/3 failed: {exc}")
            _time.sleep(2 ** attempt)
        if data is None or data.empty:
            return {}
        want = pd.Timestamp(trading_date)
        for sym in symbols:
            try:
                # Grouped (per-ticker) columns first; flat single-ticker frame
                # as fallback, since yfinance varies by version/input shape.
                try:
                    series = data[sym]["Open"]
                except (KeyError, TypeError):
                    series = data["Open"]
                series = series.dropna()
                if series.empty:
                    continue
                idx = series.index
                try:
                    idx = idx.tz_localize(None)
                except (AttributeError, TypeError):
                    pass
                hits = series[idx.normalize() == want]
                if not hits.empty:
                    out[sym] = float(hits.iloc[-1])
            except Exception:  # noqa: BLE001 — one bad symbol must not kill fills
                continue
    except Exception as exc:  # noqa: BLE001
        print(f"  ! open-price fetch failed: {exc}")
    return out


def _execute_pending(state: dict, today: str, names: dict, ist) -> tuple[list, list, float, list]:
    """Fill yesterday's queued orders at TODAY's open.

    Returns (trades, dropped, costs, carried). Sells fill first so their
    proceeds can fund the buys. Buys pay SLIPPAGE_BPS above the open, sells
    receive SLIPPAGE_BPS below it, and every fill is charged
    COST_BPS_PER_SIDE on notional. Orders that cannot be priced today are
    dropped with a note (recorded in the day's snapshot). Orders queued TODAY
    (possible only when a same-day snapshot is being replaced) are never
    filled — no order fills on the bar that produced it — and are returned in
    ``carried`` so they stay queued for the next session.
    """
    pending = state.get("pending_orders") or []
    trades: list[dict] = []
    dropped: list[dict] = []
    carried: list[dict] = []
    costs = 0.0
    if not pending:
        return trades, dropped, costs, carried
    fillable = []
    for order in pending:
        if order.get("queued_date") == today:
            carried.append(order)
        else:
            fillable.append(order)
    if not fillable:
        return trades, dropped, costs, carried
    opens = _fetch_opens(
        sorted({o.get("symbol") for o in fillable if o.get("symbol")}), today)
    slip = SLIPPAGE_BPS / 10000.0
    cost_rate = COST_BPS_PER_SIDE / 10000.0
    # Sells first, then buys, keeping each group in queue (rank) order.
    ordered = sorted(fillable, key=lambda o: 0 if o.get("action") == "SELL" else 1)
    for order in ordered:
        sym = order.get("symbol")
        action = order.get("action")
        open_px = opens.get(sym)
        if not sym or action not in ("BUY", "SELL"):
            dropped.append({**order, "drop_reason": "malformed_order"})
            continue
        if not open_px or open_px <= 0:
            dropped.append({**order, "drop_reason": "no_price_data_today"})
            continue
        if action == "SELL":
            pos = state["positions"].get(sym)
            if not pos or pos.get("qty", 0) <= 0:
                # e.g. the intraday stop already exited this name.
                dropped.append({**order, "drop_reason": "position_already_closed"})
                continue
            qty = pos["qty"]   # whole-position exits (stop / rank-out)
            fill_px = open_px * (1 - slip)
            proceeds = qty * fill_px
            cost = proceeds * cost_rate
            costs += cost
            state["cash"] += proceeds - cost
            state["positions"].pop(sym)
            trades.append({
                "action": "SELL", "symbol": sym,
                "name": names.get(sym, pos.get("name", sym)), "qty": qty,
                "price": round(fill_px, 2), "cost": round(cost, 2),
                "reason": order.get("reason", "queued_sell"),
                "queued_date": order.get("queued_date"),
                "date": today, "ist": ist.isoformat(), "phase": "eod_open_fill"})
        else:
            budget = float(order.get("budget") or 0.0)
            fill_px = open_px * (1 + slip)
            denom = fill_px * (1 + cost_rate)
            qty = int(budget / denom) if denom > 0 else 0
            qty = min(qty, int(state["cash"] / denom)) if denom > 0 else 0
            if qty <= 0:
                dropped.append({**order, "drop_reason": "insufficient_cash_or_budget"})
                continue
            spend = qty * fill_px
            cost = spend * cost_rate
            costs += cost
            state["cash"] -= spend + cost
            old = state["positions"].get(sym)
            if old:
                new_qty = old["qty"] + qty
                old["avg_price"] = round(
                    (old["avg_price"] * old["qty"] + spend) / new_qty, 2)
                old["qty"] = new_qty
                old["peak_price"] = round(max(old.get("peak_price", fill_px), fill_px), 2)
            else:
                state["positions"][sym] = {
                    "qty": qty, "avg_price": round(fill_px, 2),
                    "peak_price": round(fill_px, 2),
                    "name": order.get("name") or names.get(sym, sym)}
            trades.append({
                "action": "BUY", "symbol": sym,
                "name": order.get("name") or names.get(sym, sym), "qty": qty,
                "price": round(fill_px, 2), "cost": round(cost, 2),
                "reason": order.get("reason", "queued_buy"),
                "queued_date": order.get("queued_date"),
                "date": today, "ist": ist.isoformat(), "phase": "eod_open_fill"})
    for d in dropped:
        print(f"  ! dropped pending {d.get('action')} {d.get('symbol')}: {d.get('drop_reason')}")
    return trades, dropped, costs, carried


def _load_state() -> dict:
    st = ds.read_json(STATE_FILE, default=None)
    if not st:
        return _empty_state()
    # Backfill any missing keys for forward-compatibility.
    base = _empty_state()
    base.update(st)
    base["history"] = _normalize_history(base.get("history") or [], base.get("inception"))
    if base["history"]:
        base["last_date"] = base["history"][-1].get("date")
    return base


def _persist_idempotent_cleanup(state: dict) -> None:
    """Persist normalized state/latest data without creating another trade row."""
    ds.write_json(STATE_FILE, state)
    latest = ds.read_json(LATEST_FILE, default=None)
    if not latest:
        return
    last_snapshot = state.get("history", [])[-1] if state.get("history") else {}
    latest["inception"] = state.get("inception")
    latest["start_capital"] = state.get("start_capital")
    latest["cash"] = round(state.get("cash", 0.0), 2)
    latest["n_positions"] = len(state.get("positions", {}))
    latest["risk_settings"] = strategy_metadata()
    latest["strategy_config"] = strategy_metadata()
    latest["active_profile"] = ACTIVE_PROFILE
    latest["pending_orders"] = state.get("pending_orders") or []
    latest["total_costs"] = state.get("total_costs")
    latest["execution_model"] = EXECUTION_MODEL
    for key in (
        "value",
        "day_pnl",
        "day_pnl_pct",
        "total_pnl",
        "total_pnl_pct",
        "benchmark_pct",
        "benchmark_value",
        "alpha_pct",
        "circuit_breaker",
    ):
        if key in last_snapshot:
            latest[key] = last_snapshot[key]
    latest["history"] = state.get("history", [])[-120:]
    ds.write_json(LATEST_FILE, latest)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_snapshot_is_final(trading_date: str) -> bool:
    latest = ds.read_json(LATEST_FILE, default={}) or {}
    generated = _parse_iso_dt(latest.get("ist") or latest.get("generated_at"))
    if not generated:
        return True
    try:
        trade_day = date.fromisoformat(trading_date)
    except ValueError:
        return True
    if generated.date() > trade_day:
        return True
    generated_time = generated.time().replace(tzinfo=None)
    return generated.date() == trade_day and generated_time > MARKET_CLOSE


def _market_value(positions: dict, prices: dict) -> float:
    total = 0.0
    for sym, pos in positions.items():
        px = prices.get(sym, pos.get("avg_price", 0.0))
        total += pos["qty"] * px
    return total


def _pick_targets(analysis: list[dict]) -> list[dict]:
    return _rank_candidates(analysis)[:TOP_N]


def _rank_candidates(analysis: list[dict]) -> list[dict]:
    ranked: list[dict] = []
    fallback: list[dict] = []
    for row in analysis:
        if not row.get("price"):
            continue
        item = dict(row)
        if ACTIVE_PROFILE == "momentum_weekly_churn_control":
            ret_3m = item.get("ret_3m")
            if ret_3m is not None and ret_3m > 0:
                item["_sort_key"] = (
                    float(ret_3m),
                    float(item.get("score") or 0),
                    -(item.get("pct_from_high") or 0),
                )
                ranked.append(item)
                continue
            if ret_3m is None and item.get("signal") == "BUY":
                fb = dict(item)
                fb["_sort_key"] = (float(fb.get("score") or 0), -(fb.get("pct_from_high") or 0))
                fb["ranking_method"] = "buy_score_fallback"
                fallback.append(fb)
                continue
        else:
            if item.get("signal") != "BUY":
                continue
            item["_sort_key"] = (float(item.get("score") or 0), -(item.get("pct_from_high") or 0))
            ranked.append(item)
    if not ranked:
        ranked = fallback
    ranked.sort(key=lambda a: a["_sort_key"], reverse=True)
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx
        item.pop("_sort_key", None)
    return ranked


def _rebalance_key(trading_date: str) -> str:
    try:
        d = date.fromisoformat(trading_date)
    except ValueError:
        d = now_ist().date()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _sector_map() -> dict:
    """symbol -> sector, sourced from the weekly watchlist (best-effort)."""
    wl = ds.read_json("watchlist/latest.json", default={}) or {}
    out = {}
    for r in wl.get("watchlist", []):
        if r.get("symbol"):
            out[r["symbol"]] = r.get("sector") or "Unknown"
    return out


def run() -> dict:
    ist = now_ist()

    daily = ds.read_json("daily/latest.json", default=None)
    if not daily or not daily.get("analysis"):
        print("No daily analysis available yet — run daily_analysis first.")
        return {"skipped": True, "reason": "no_daily"}
    today = daily.get("trading_date") or ist.strftime("%Y-%m-%d")

    analysis = daily["analysis"]
    prices = {a["symbol"]: a["price"] for a in analysis if a.get("price")}
    names = {a["symbol"]: a.get("name", a["symbol"]) for a in analysis}

    state = _load_state()
    if state["last_date"] and today < state["last_date"]:
        _persist_idempotent_cleanup(state)
        print(
            f"Daily analysis is stale ({today}) vs paper state "
            f"({state['last_date']}). Skipping to keep the book chronological."
        )
        return {
            "skipped": True,
            "reason": "stale_trading_date",
            "date": today,
            "last_date": state["last_date"],
        }
    ist_time = ist.time().replace(tzinfo=None)
    same_day_before_close = today == ist.date().isoformat() and ist_time <= MARKET_CLOSE
    replacing_preliminary = False
    if state["last_date"] == today:
        if not _latest_snapshot_is_final(today):
            if same_day_before_close:
                _persist_idempotent_cleanup(state)
                print(
                    f"Market has not closed for {today}. Skipping paper EOD "
                    "so the daily book is not locked with an intraday price."
                )
                return {"skipped": True, "reason": "market_not_closed", "date": today}
            replacing_preliminary = True
            state["history"] = [row for row in state["history"] if row.get("date") != today]
            state["last_date"] = state["history"][-1].get("date") if state["history"] else None
        else:
            _persist_idempotent_cleanup(state)
            print(f"Paper trade already run for {today}. Skipping (idempotent).")
            return {"skipped": True, "reason": "already_done", "date": today}

    if same_day_before_close:
        _persist_idempotent_cleanup(state)
        print(
            f"Market has not closed for {today}. Skipping paper EOD "
            "so the daily book is not locked with an intraday price."
        )
        return {"skipped": True, "reason": "market_not_closed", "date": today}

    if state["inception"] is None:
        state["inception"] = today

    # Day P&L is measured as today's closing NAV minus the PREVIOUS trading
    # day's stored closing NAV (true day-over-day mark-to-market). On the first
    # ever run there is no prior snapshot, so we anchor to the start capital.
    prev_value = state["history"][-1]["value"] if state["history"] else state["start_capital"]
    equity_peak = max([h.get("value", state["start_capital"]) for h in state["history"]] or
                      [state["start_capital"]])

    regime = daily.get("market_regime") or {}
    partial_data = bool(daily.get("partial"))
    sectors = _sector_map()
    risk_events: list[dict] = []

    # --- (a) EXECUTE: fill orders queued at the previous session's close at
    # TODAY's adjusted open (next-open execution — a signal never fills on the
    # same bar that produced it).
    trades, dropped_orders, cost_total, carried_orders = _execute_pending(state, today, names, ist)
    state["pending_orders"] = []
    state["total_costs"] = round(float(state.get("total_costs") or 0.0) + cost_total, 2)

    # --- (b) MARK the book at today's close.
    end_value = state["cash"] + _market_value(state["positions"], prices)

    # Circuit-breaker: track the all-time peak book value; while the drawdown
    # from that peak is at least MAX_DRAWDOWN_PAUSE_PCT, queue NO new buys
    # (protective sells/stops below still run). Resets naturally once the book
    # value recovers inside the threshold.
    peak_value = max(float(state.get("peak_value") or state["start_capital"]), end_value)
    state["peak_value"] = round(peak_value, 2)
    drawdown_from_peak_pct = round((peak_value - end_value) / peak_value * 100, 3) \
        if peak_value else 0.0
    circuit_breaker = drawdown_from_peak_pct >= MAX_DRAWDOWN_PAUSE_PCT
    if circuit_breaker:
        risk_events.append({"type": "circuit_breaker",
                            "drawdown_from_peak_pct": drawdown_from_peak_pct,
                            "threshold_pct": MAX_DRAWDOWN_PAUSE_PCT})
        print(f"Circuit-breaker active: {drawdown_from_peak_pct:.2f}% below peak "
              f"(threshold {MAX_DRAWDOWN_PAUSE_PCT}%) — no new buys will be queued.")

    # --- (c) DECIDE from today's close and QUEUE orders for the NEXT session.
    ranked_candidates = _rank_candidates(analysis)
    candidate_rank = {r["symbol"]: r["rank"] for r in ranked_candidates}
    targets = ranked_candidates[:TOP_N]
    rebalance_key = _rebalance_key(today)
    rebalance_due = (
        REBALANCE_INTERVAL != "weekly" or
        state.get("last_rebalance_key") != rebalance_key
    )
    pending_next: list[dict] = []
    risk_block_new_buys = bool(circuit_breaker)
    queue_skipped_reason = None
    if partial_data:
        # Thin analysis coverage: don't make ANY new decisions off it. Already
        # queued orders were still filled above and the book is still marked;
        # the rebalance key is left unconsumed so tomorrow retries.
        queue_skipped_reason = "partial_daily_data"
        risk_events.append({"type": "partial_daily_data",
                            "coverage_pct": daily.get("coverage_pct")})
        print("  ! daily analysis flagged partial — no new orders queued today.")

    if queue_skipped_reason is None:
        # 1a) STOP-LOSS: queue an exit for any holding down more than
        # STOP_LOSS_PCT from its average buy price (or TRAILING_STOP_PCT off
        # its peak), to fill at the next session's open. These symbols are
        # blocked from being re-queued as buys today so we don't churn back in.
        blocked: set[str] = set()
        queued_sells: set[str] = set()
        for sym in list(state["positions"].keys()):
            pos = state["positions"][sym]
            px = prices.get(sym, pos.get("avg_price", 0.0))
            pos["peak_price"] = round(max(pos.get("peak_price", px), px), 2)
            avg = pos.get("avg_price", 0.0)
            stop_reason = None
            if avg and px <= avg * (1 - STOP_LOSS_PCT):
                stop_reason = "stop_loss"
            elif px <= pos["peak_price"] * (1 - TRAILING_STOP_PCT):
                stop_reason = "trailing_stop"
            if stop_reason:
                loss_pct = round((px / avg - 1) * 100, 2) if avg else 0.0
                pending_next.append({"action": "SELL", "symbol": sym,
                                     "name": pos.get("name", names.get(sym, sym)),
                                     "qty": pos["qty"], "reason": stop_reason,
                                     "queued_date": today, "ref_close": round(px, 2)})
                risk_events.append({"type": stop_reason, "symbol": sym,
                                    "name": names.get(sym, sym), "loss_pct": loss_pct,
                                    "peak_price": pos.get("peak_price")})
                blocked.add(sym)
                queued_sells.add(sym)

        # Drop blocked names from today's target set.
        targets = [t for t in targets if t["symbol"] not in blocked]
        target_syms = {t["symbol"] for t in targets}

        # 1b) On weekly rebalance only: queue exits for holdings that have
        # fallen outside the hold band. On non-rebalance days, only stops can
        # change the book.
        if rebalance_due:
            keep_syms = {
                sym for sym in state["positions"]
                if candidate_rank.get(sym, 10**9) <= HOLD_UNTIL_RANK
            }
            target_syms |= keep_syms
            for sym in list(state["positions"].keys()):
                if sym not in target_syms and sym not in queued_sells:
                    pos = state["positions"][sym]
                    px = prices.get(sym, pos.get("avg_price", 0.0))
                    pending_next.append({"action": "SELL", "symbol": sym,
                                         "name": pos.get("name", names.get(sym, sym)),
                                         "qty": pos["qty"], "reason": "exit_signal",
                                         "queued_date": today, "ref_close": round(px, 2)})
                    queued_sells.add(sym)
        else:
            target_syms = set(state["positions"].keys()) - queued_sells
            risk_events.append({
                "type": "rebalance_skip",
                "reason": f"{REBALANCE_INTERVAL} cadence",
                "next_rebalance_key": state.get("last_rebalance_key"),
            })

        # 2) Risk guards for NEW buys, evaluated at today's close.
        total_value = end_value
        if prev_value and (total_value / prev_value - 1) <= -MAX_DAILY_LOSS_PCT:
            risk_block_new_buys = True
            risk_events.append({"type": "daily_loss_guard", "loss_pct": round((total_value / prev_value - 1) * 100, 2)})
        if equity_peak and (total_value / equity_peak - 1) <= -MAX_DRAWDOWN_PCT:
            risk_block_new_buys = True
            risk_events.append({"type": "drawdown_guard", "drawdown_pct": round((total_value / equity_peak - 1) * 100, 2)})
        if USE_MARKET_REGIME_GUARD and regime and not regime.get("risk_on", True):
            risk_block_new_buys = True
            risk_events.append({"type": "market_regime_guard", "reason": regime.get("reason")})

        if rebalance_due and targets and not risk_block_new_buys:
            # Equal-weight budget per target with position & sector caps, all
            # based on today's close. Queued sells free their value for
            # tomorrow's buys, so budget against the full book value while
            # tracking estimated cash so the queue stays fundable in rank
            # order (fills are hard-capped by actual cash anyway).
            base_budget = total_value / len(targets)
            pos_cap = MAX_POSITION_PCT * total_value
            sector_cap = MAX_SECTOR_PCT * total_value
            est_cash = state["cash"]
            # Running sector exposure (value that will still be held tomorrow).
            sector_alloc: dict[str, float] = {}
            for sym, pos in state["positions"].items():
                if sym in queued_sells:
                    est_cash += pos["qty"] * prices.get(sym, pos.get("avg_price", 0.0))
                    continue
                sec = sectors.get(sym, "Unknown")
                sector_alloc[sec] = sector_alloc.get(sec, 0.0) + pos["qty"] * prices.get(sym, 0)

            # 3) Queue a BUY/top-up for each target toward its capped budget
            # (highest rank first). Only the rupee budget is queued; quantity
            # is decided at fill time from the actual next-session open.
            for t in targets:
                sym = t["symbol"]
                px = prices[sym]
                sec = sectors.get(sym, "Unknown")
                held_qty = 0 if sym in queued_sells \
                    else state["positions"].get(sym, {}).get("qty", 0)
                held_val = held_qty * px

                # Target value for this name = equal weight, capped by position cap.
                target_val = min(base_budget, pos_cap)
                # Respect remaining sector capacity.
                sector_room = sector_cap - sector_alloc.get(sec, 0.0)
                if sector_room <= 0:
                    risk_events.append({"type": "sector_cap_skip", "symbol": sym,
                                        "name": names.get(sym, sym), "sector": sec})
                    continue
                target_val = min(target_val, held_val + sector_room)

                want_val = min(target_val - held_val, est_cash)
                if want_val <= px:  # nothing meaningful to add
                    continue
                est_cash -= want_val
                sector_alloc[sec] = sector_alloc.get(sec, 0.0) + want_val
                pending_next.append({"action": "BUY", "symbol": sym,
                                     "name": names.get(sym, sym),
                                     "budget": round(want_val, 2),
                                     "reason": "weekly_rebalance",
                                     "queued_date": today, "ref_close": round(px, 2)})
        elif rebalance_due and targets:
            print("Risk guard active — no new buys queued today.")

        if rebalance_due:
            state["last_rebalance_key"] = rebalance_key

    # Orders queued earlier today (same-day snapshot replacement) stay queued
    # unless this pass re-derived an order for the same symbol+side.
    if carried_orders:
        seen = {(o.get("action"), o.get("symbol")) for o in pending_next}
        for order in carried_orders:
            if (order.get("action"), order.get("symbol")) not in seen:
                pending_next.append(order)
    state["pending_orders"] = pending_next

    # 4) Day P&L from the close-marked book (positions/cash are unchanged by
    # queueing — fills only happen at the next session's open).
    # Absolute fill notional traded today (both sides), for turnover stats.
    traded_value = round(sum(abs(t.get("qty", 0) * t.get("price", 0.0)) for t in trades), 2)

    day_pnl = round(end_value - prev_value, 2)
    day_pnl_pct = round(day_pnl / prev_value * 100, 3) if prev_value else 0.0
    total_pnl = round(end_value - state["start_capital"], 2)
    total_pnl_pct = round(total_pnl / state["start_capital"] * 100, 3)

    # 5) Benchmark: what the same Rs 5L would be worth if simply held in NIFTY 50
    # since inception. Alpha = how much the bot beat (or lagged) just-hold-index.
    bench_level = _benchmark_level()
    if bench_level and state.get("benchmark_inception") is None:
        state["benchmark_inception"] = bench_level
        state["benchmark_inception_date"] = today
    anchor = state.get("benchmark_inception")
    if bench_level and anchor:
        benchmark_pct = round((bench_level / anchor - 1) * 100, 3)
        benchmark_value = round(state["start_capital"] * bench_level / anchor, 2)
    else:
        benchmark_pct = 0.0
        benchmark_value = state["start_capital"]
    alpha_pct = round(total_pnl_pct - benchmark_pct, 3)

    snapshot = {
        "date": today,
        "generated_ist": ist.isoformat(),
        "final": True,
        "value": round(end_value, 2),
        "cash": round(state["cash"], 2),
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "benchmark_pct": benchmark_pct,
        "benchmark_value": benchmark_value,
        "alpha_pct": alpha_pct,
        "n_positions": len(state["positions"]),
        "n_trades": len(trades),
        "n_stops": sum(1 for e in risk_events if e["type"] == "stop_loss"),
        "risk_block_new_buys": risk_block_new_buys,
        "rebalance_due": rebalance_due,
        "rebalance_key": rebalance_key,
        "active_profile": ACTIVE_PROFILE,
        "costs": round(cost_total, 2),
        "total_costs": state["total_costs"],
        "traded_value": traded_value,
        "trades": trades,
        "dropped_orders": dropped_orders,
        "n_queued": len(state["pending_orders"]),
        "circuit_breaker": circuit_breaker,
        "drawdown_from_peak_pct": drawdown_from_peak_pct,
        "queue_skipped": queue_skipped_reason,
        "execution_model": EXECUTION_MODEL,
    }

    state["last_date"] = today
    state["history"].append(snapshot)
    state["history"] = _normalize_history(state["history"], state.get("inception"))[-400:]  # keep ~1.5 yrs
    if state["history"]:
        state["last_date"] = state["history"][-1].get("date")

    # Persist full state + compact dashboard view + history line.
    ds.write_json(STATE_FILE, state)
    ds.append_jsonl(HISTORY_FILE, snapshot)
    if replacing_preliminary:
        print(f"Replaced preliminary paper snapshot for {today} with post-close data.")

    positions_view = []
    for sym, pos in sorted(state["positions"].items(),
                           key=lambda kv: kv[1]["qty"] * prices.get(kv[0], 0),
                           reverse=True):
        px = prices.get(sym, pos["avg_price"])
        mv = round(pos["qty"] * px, 2)
        pnl = round((px - pos["avg_price"]) * pos["qty"], 2)
        positions_view.append({
            "symbol": sym, "name": pos.get("name", sym), "qty": pos["qty"],
            "avg_price": pos["avg_price"], "price": round(px, 2),
            "value": mv, "pnl": pnl,
            "peak_price": pos.get("peak_price"),
            "pnl_pct": round((px / pos["avg_price"] - 1) * 100, 2)
            if pos["avg_price"] else 0.0,
        })

    latest = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "inception": state["inception"],
        "start_capital": state["start_capital"],
        "value": round(end_value, 2),
        "cash": round(state["cash"], 2),
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "benchmark_pct": benchmark_pct,
        "benchmark_value": benchmark_value,
        "benchmark_name": "NIFTY 50",
        "alpha_pct": alpha_pct,
        "n_positions": len(state["positions"]),
        "today_trades": trades,
        "dropped_orders": dropped_orders,
        "pending_orders": state["pending_orders"],
        "total_costs": state["total_costs"],
        "costs_today": round(cost_total, 2),
        "circuit_breaker": circuit_breaker,
        "drawdown_from_peak_pct": drawdown_from_peak_pct,
        "queue_skipped": queue_skipped_reason,
        "execution_model": EXECUTION_MODEL,
        "risk_events": risk_events,
        "risk_settings": strategy_metadata(),
        "market_regime": regime,
        "rebalance_due": rebalance_due,
        "rebalance_key": rebalance_key,
        "active_profile": ACTIVE_PROFILE,
        "ranked_candidates": [
            {
                "rank": r.get("rank"),
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "ret_1d": r.get("ret_1d"),
                "ret_1w": r.get("ret_1w"),
                "ret_3m": r.get("ret_3m"),
                "score": r.get("score"),
                "signal": r.get("signal"),
            }
            for r in ranked_candidates[:HOLD_UNTIL_RANK]
        ],
        "positions": positions_view,
        "history": state["history"][-120:],
        "strategy_config": strategy_metadata(),
        "strategy": (
            f"Incubation profile {ACTIVE_PROFILE}: rank liquid Top-50 names by "
            f"the active score, buy top {TOP_N}, rebalance {REBALANCE_INTERVAL}, "
            f"and hold existing names until rank > {HOLD_UNTIL_RANK}. "
            f"Market-regime guard {'on' if USE_MARKET_REGIME_GUARD else 'off'}. Risk controls: "
            f"{STOP_LOSS_PCT*100:.0f}% stop-loss, {TRAILING_STOP_PCT*100:.0f}% trailing stop, "
            f"{MAX_POSITION_PCT*100:.0f}% max "
            f"per stock, {MAX_SECTOR_PCT*100:.0f}% max per sector, "
            f"{MAX_DRAWDOWN_PAUSE_PCT}% drawdown circuit-breaker. Orders decided "
            f"at the close fill at the NEXT session's open with {SLIPPAGE_BPS} bps "
            f"slippage and {COST_BPS_PER_SIDE} bps cost per trade leg. Simulation "
            "only — not investment advice."
        ),
    }
    ds.write_json(LATEST_FILE, latest)

    print(f"[{today}] value Rs {end_value:,.0f} | day {day_pnl:+,.0f} "
          f"({day_pnl_pct:+.2f}%) | total {total_pnl:+,.0f} "
          f"({total_pnl_pct:+.2f}%) | NIFTY {benchmark_pct:+.2f}% | "
          f"alpha {alpha_pct:+.2f}% | {len(trades)} fills | "
          f"{len(state['pending_orders'])} queued | "
          f"{len(state['positions'])} holdings")
    return latest


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
