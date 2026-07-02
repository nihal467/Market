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
  - A small round-trip cost (COST_PER_SIDE) approximates brokerage + slippage
    + taxes so returns are not overstated.
  - Mark the book to market at the day's close, record the day's P&L, and
    append a dated history row.

State files (on the data branch):
  paper/state.json    -> full state: cash, positions, history, inception
  paper/latest.json   -> compact view for the dashboard (today + equity curve)
  paper/history.jsonl -> one appended line per trading day

Idempotent per date: a second run on the same trading date is a no-op, so the
twice-daily Daily Analysis workflow won't double-trade.
"""
from __future__ import annotations

import sys
from datetime import date

import datastore as ds
from market_calendar import now_ist
from strategy_config import (
    ACTIVE_PROFILE,
    COST_PER_SIDE,
    HOLD_UNTIL_RANK,
    MAX_DAILY_LOSS_PCT,
    MAX_DRAWDOWN_PCT,
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    REBALANCE_INTERVAL,
    START_CAPITAL,
    STOP_LOSS_PCT,
    TOP_N,
    TRAILING_STOP_PCT,
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
    for key in (
        "value",
        "day_pnl",
        "day_pnl_pct",
        "total_pnl",
        "total_pnl_pct",
        "benchmark_pct",
        "benchmark_value",
        "alpha_pct",
    ):
        if key in last_snapshot:
            latest[key] = last_snapshot[key]
    latest["history"] = state.get("history", [])[-120:]
    ds.write_json(LATEST_FILE, latest)


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
    if state["last_date"] == today:
        _persist_idempotent_cleanup(state)
        print(f"Paper trade already run for {today}. Skipping (idempotent).")
        return {"skipped": True, "reason": "already_done", "date": today}

    if state["inception"] is None:
        state["inception"] = today

    # Day P&L is measured as today's closing NAV minus the PREVIOUS trading
    # day's stored closing NAV (true day-over-day mark-to-market). On the first
    # ever run there is no prior snapshot, so we anchor to the start capital.
    prev_value = state["history"][-1]["value"] if state["history"] else state["start_capital"]
    equity_peak = max([h.get("value", state["start_capital"]) for h in state["history"]] or
                      [state["start_capital"]])

    ranked_candidates = _rank_candidates(analysis)
    candidate_rank = {r["symbol"]: r["rank"] for r in ranked_candidates}
    targets = ranked_candidates[:TOP_N]
    rebalance_key = _rebalance_key(today)
    rebalance_due = (
        REBALANCE_INTERVAL != "weekly" or
        state.get("last_rebalance_key") != rebalance_key
    )
    regime = daily.get("market_regime") or {}
    sectors = _sector_map()
    trades: list[dict] = []
    risk_events: list[dict] = []
    cost_total = 0.0
    risk_block_new_buys = False

    # 1a) STOP-LOSS: force-exit any holding down more than STOP_LOSS_PCT from its
    # average buy price, regardless of signal. These symbols are blocked from
    # being re-bought today so we don't immediately churn back in.
    blocked: set[str] = set()
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
            proceeds = pos["qty"] * px
            cost = proceeds * COST_PER_SIDE
            cost_total += cost
            state["cash"] += proceeds - cost
            loss_pct = round((px / avg - 1) * 100, 2)
            trades.append({"action": "SELL", "symbol": sym,
                           "name": names.get(sym, sym), "qty": pos["qty"],
                           "price": round(px, 2), "reason": stop_reason,
                           "date": today, "ist": ist.isoformat(), "phase": "eod"})
            risk_events.append({"type": stop_reason, "symbol": sym,
                                "name": names.get(sym, sym), "loss_pct": loss_pct,
                                "peak_price": pos.get("peak_price")})
            blocked.add(sym)
            state["positions"].pop(sym)

    # Drop blocked names from today's target set.
    targets = [t for t in targets if t["symbol"] not in blocked]
    target_syms = {t["symbol"] for t in targets}

    # 1b) On weekly rebalance only: trim holdings that have fallen outside the
    # hold band. On non-rebalance days, only stops can change the book.
    if rebalance_due:
        keep_syms = {
            sym for sym in state["positions"]
            if candidate_rank.get(sym, 10**9) <= HOLD_UNTIL_RANK
        }
        target_syms |= keep_syms
    else:
        target_syms = set(state["positions"].keys())
        risk_events.append({
            "type": "rebalance_skip",
            "reason": f"{REBALANCE_INTERVAL} cadence",
            "next_rebalance_key": state.get("last_rebalance_key"),
        })

    for sym in list(state["positions"].keys()):
        if rebalance_due and sym not in target_syms:
            pos = state["positions"].pop(sym)
            px = prices.get(sym, pos.get("avg_price", 0.0))
            proceeds = pos["qty"] * px
            cost = proceeds * COST_PER_SIDE
            cost_total += cost
            state["cash"] += proceeds - cost
            trades.append({"action": "SELL", "symbol": sym,
                           "name": names.get(sym, sym), "qty": pos["qty"],
                           "price": round(px, 2), "reason": "exit_signal",
                           "date": today, "ist": ist.isoformat(), "phase": "eod"})

    # 2) Determine equal-weight budget per target, then apply position & sector
    # caps so a single name or sector can't dominate the book.
    total_value = state["cash"] + _market_value(state["positions"], prices)
    if prev_value and (total_value / prev_value - 1) <= -MAX_DAILY_LOSS_PCT:
        risk_block_new_buys = True
        risk_events.append({"type": "daily_loss_guard", "loss_pct": round((total_value / prev_value - 1) * 100, 2)})
    if equity_peak and (total_value / equity_peak - 1) <= -MAX_DRAWDOWN_PCT:
        risk_block_new_buys = True
        risk_events.append({"type": "drawdown_guard", "drawdown_pct": round((total_value / equity_peak - 1) * 100, 2)})
    if regime and not regime.get("risk_on", True):
        risk_block_new_buys = True
        risk_events.append({"type": "market_regime_guard", "reason": regime.get("reason")})

    if rebalance_due and targets and not risk_block_new_buys:
        base_budget = total_value / len(targets)
        pos_cap = MAX_POSITION_PCT * total_value
        sector_cap = MAX_SECTOR_PCT * total_value
        # Running sector exposure (value already held this run).
        sector_alloc: dict[str, float] = {}
        for sym, pos in state["positions"].items():
            sec = sectors.get(sym, "Unknown")
            sector_alloc[sec] = sector_alloc.get(sec, 0.0) + pos["qty"] * prices.get(sym, 0)

        # 3) BUY/top-up each target toward its capped budget (highest score first).
        for t in targets:
            sym = t["symbol"]
            px = prices[sym]
            sec = sectors.get(sym, "Unknown")
            held_qty = state["positions"].get(sym, {}).get("qty", 0)
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

            want_val = target_val - held_val
            if want_val <= px:  # nothing meaningful to add
                continue
            # Reserve for cost so we don't overspend cash.
            buy_qty = int(want_val / (px * (1 + COST_PER_SIDE)))
            buy_qty = min(buy_qty, int(state["cash"] / (px * (1 + COST_PER_SIDE))))
            if buy_qty <= 0:
                continue
            spend = buy_qty * px
            cost = spend * COST_PER_SIDE
            cost_total += cost
            state["cash"] -= spend + cost
            sector_alloc[sec] = sector_alloc.get(sec, 0.0) + spend
            old = state["positions"].get(sym)
            if old:
                new_qty = old["qty"] + buy_qty
                old["avg_price"] = round(
                    (old["avg_price"] * old["qty"] + spend) / new_qty, 2)
                old["qty"] = new_qty
                old["peak_price"] = round(max(old.get("peak_price", px), px), 2)
            else:
                state["positions"][sym] = {
                    "qty": buy_qty, "avg_price": round(px, 2),
                    "peak_price": round(px, 2), "name": names.get(sym, sym)}
            trades.append({"action": "BUY", "symbol": sym,
                           "name": names.get(sym, sym), "qty": buy_qty,
                           "price": round(px, 2), "reason": "weekly_rebalance",
                           "date": today, "ist": ist.isoformat(), "phase": "eod"})
    elif rebalance_due and targets:
        print("Risk guard active — new buys skipped today.")

    if rebalance_due:
        state["last_rebalance_key"] = rebalance_key

    # 4) Mark to market at today's close.
    end_value = state["cash"] + _market_value(state["positions"], prices)
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
    }

    state["last_date"] = today
    state["history"].append(snapshot)
    state["history"] = _normalize_history(state["history"], state.get("inception"))[-400:]  # keep ~1.5 yrs
    if state["history"]:
        state["last_date"] = state["history"][-1].get("date")

    # Persist full state + compact dashboard view + history line.
    ds.write_json(STATE_FILE, state)
    ds.append_jsonl(HISTORY_FILE, snapshot)

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
            f"positive 3-month momentum, buy top {TOP_N}, rebalance {REBALANCE_INTERVAL}, "
            f"and hold existing names until rank > {HOLD_UNTIL_RANK}. Risk controls: "
            f"{STOP_LOSS_PCT*100:.0f}% stop-loss, {TRAILING_STOP_PCT*100:.0f}% trailing stop, "
            f"{MAX_POSITION_PCT*100:.0f}% max "
            f"per stock, {MAX_SECTOR_PCT*100:.0f}% max per sector. "
            f"{COST_PER_SIDE*100:.2f}% cost per trade leg. Simulation only — not "
            "investment advice."
        ),
    }
    ds.write_json(LATEST_FILE, latest)

    print(f"[{today}] value Rs {end_value:,.0f} | day {day_pnl:+,.0f} "
          f"({day_pnl_pct:+.2f}%) | total {total_pnl:+,.0f} "
          f"({total_pnl_pct:+.2f}%) | NIFTY {benchmark_pct:+.2f}% | "
          f"alpha {alpha_pct:+.2f}% | {len(trades)} trades | "
          f"{len(state['positions'])} holdings")
    return latest


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
