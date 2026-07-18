"""Intraday live paper trader — runs every ~15 minutes during market hours.

This is the "real-time" arm of the dummy ₹5,00,000 bot. It shares the SAME book
(paper/state.json) as the end-of-day trader, but instead of waiting for the
close it manages the portfolio live:

  - Marks every holding to the latest intraday price (from intraday/latest.json,
    produced by intraday.py earlier in the same workflow run) and publishes a
    LIVE profit-and-loss snapshot to paper/live.json roughly every 15 minutes.
  - Executes protective STOP-LOSS sells in real time: any holding down more than
    STOP_LOSS_PCT from its average buy price is sold immediately rather than
    waiting for the close.
  - During the weekly incubation profile, intraday does not deploy idle cash.
    It only marks to market and executes protective exits; allocation decisions
    happen in the end-of-day paper trader.

Granularity note: GitHub Actions cron is best-effort, so "real time" here means
"every ~15 minutes during market hours", not tick-by-tick. True low-latency
trading would need an always-on server.

Writes:
  paper/live.json     -> live P&L view for the dashboard (intraday)
  paper/state.json    -> shared book (positions, cash) — kept continuous
"""
from __future__ import annotations

import sys

import datastore as ds
from market_calendar import is_market_open, now_ist

# Reuse the single source of truth for capital, caps, costs and file paths.
from paper_trader import (
    COST_BPS_PER_SIDE,
    COST_PER_SIDE,
    SLIPPAGE_BPS,
    INTRADAY_SHOCK_BOOK_LOSS_PCT,
    INTRADAY_SHOCK_INDEX_DROP_PCT,
    INTRADAY_SHOCK_WATCHLIST_DOWN_FRACTION,
    INTRADAY_SHOCK_WATCHLIST_MEDIAN_DROP_PCT,
    INTRADAY_WEAK_HOLDING_DAY_DROP_PCT,
    INTRADAY_WEAK_HOLDING_LOSS_PCT,
    MAX_DAILY_LOSS_PCT,
    MAX_DRAWDOWN_PCT,
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    STATE_FILE,
    STOP_LOSS_PCT,
    TOP_N,
    TRAILING_STOP_PCT,
    _load_state,
    _market_value,
    _pick_targets,
    _sector_map,
)
from strategy_config import strategy_metadata
from strategy_config import ACTIVE_PROFILE, REBALANCE_INTERVAL, USE_MARKET_REGIME_GUARD

LIVE_FILE = "paper/live.json"
INDEX_PROXY_SYMBOLS = {"NIFTYBEES.NS", "JUNIORBEES.NS", "MID150BEES.NS"}

# Only deploy idle cash intraday once it exceeds this fraction of the book, so
# we don't churn tiny amounts every 15 minutes.
MIN_DEPLOY_FRACTION = 0.03


def _live_prices() -> dict:
    """Latest intraday prices per symbol from intraday/latest.json."""
    snap = ds.read_json("intraday/latest.json", default={}) or {}
    prices = {}
    for row in snap.get("snapshots", []):
        sym, px = row.get("symbol"), row.get("price")
        if sym and px:
            prices[sym] = float(px)
    return prices


def _price_source() -> dict:
    snap = ds.read_json("intraday/latest.json", default={}) or {}
    return {
        "data_source": snap.get("data_source", "yfinance"),
        "source_label": snap.get("source_label", "Yahoo Finance"),
        "realtime": bool(snap.get("realtime", False)),
        "delay": snap.get("delay", "~15-min delayed"),
    }


def _intraday_snapshot_rows() -> list[dict]:
    snap = ds.read_json("intraday/latest.json", default={}) or {}
    return snap.get("snapshots", [])


def _shock_metrics(rows: list[dict]) -> dict:
    changes = [float(r["chg_pct"]) for r in rows if r.get("chg_pct") is not None]
    if not changes:
        return {
            "index_drop_pct": 0.0,
            "watchlist_down_fraction": 0.0,
            "watchlist_median_chg_pct": 0.0,
        }
    index_changes = [
        float(r["chg_pct"]) for r in rows
        if r.get("symbol") in INDEX_PROXY_SYMBOLS and r.get("chg_pct") is not None
    ]
    watch_changes = [
        float(r["chg_pct"]) for r in rows
        if r.get("symbol") not in INDEX_PROXY_SYMBOLS and r.get("chg_pct") is not None
    ]
    ordered = sorted(watch_changes or changes)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "index_drop_pct": min(index_changes) if index_changes else 0.0,
        "watchlist_down_fraction": round(
            sum(1 for c in watch_changes if c < 0) / len(watch_changes), 3
        ) if watch_changes else 0.0,
        "watchlist_median_chg_pct": round(median, 3),
    }


def _sell_position(state: dict, sym: str, px: float, reason: str, names: dict,
                   ist, trades: list[dict], stops: list[dict], cost_total: float) -> float:
    pos = state["positions"][sym]
    # Same execution realism as the EOD fills: sells give up SLIPPAGE_BPS vs
    # the reference price and pay COST_BPS_PER_SIDE on notional.
    fill_px = px * (1 - SLIPPAGE_BPS / 10000.0)
    proceeds = pos["qty"] * fill_px
    cost = proceeds * COST_PER_SIDE
    cost_total += cost
    state["cash"] += proceeds - cost
    avg = pos.get("avg_price", 0.0)
    pnl_pct = round((fill_px / avg - 1) * 100, 2) if avg else 0.0
    t = {"action": "SELL", "symbol": sym, "name": names.get(sym, sym),
         "qty": pos["qty"], "price": round(fill_px, 2), "cost": round(cost, 2),
         "reason": reason, "ist": ist.isoformat()}
    trades.append(t)
    stops.append({"symbol": sym, "name": names.get(sym, sym),
                  "loss_pct": pnl_pct, "type": reason})
    state["positions"].pop(sym)
    return cost_total


def run() -> dict:
    ist = now_ist()
    today = ist.strftime("%Y-%m-%d")

    if not is_market_open(ist):
        print(f"Market closed (IST {ist.isoformat()}). Intraday trader idle.")
        return {"skipped": True, "reason": "market_closed"}

    state = _load_state()
    if not state["positions"] and state["cash"] >= state["start_capital"]:
        # Nothing has been bought yet (the EOD bot seeds the first positions).
        print("No positions yet — waiting for the first end-of-day allocation.")
        # Still publish a flat live snapshot so the dashboard shows it's tracking.

    snapshot_rows = _intraday_snapshot_rows()
    snapshot_by_symbol = {r.get("symbol"): r for r in snapshot_rows if r.get("symbol")}
    live_prices = {
        sym: float(row["price"])
        for sym, row in snapshot_by_symbol.items()
        if row.get("price")
    }
    if not live_prices:
        print("No intraday prices available yet — run intraday.py first.")
        return {"skipped": True, "reason": "no_intraday_prices"}

    # Fallbacks: if a held symbol is missing from the intraday snapshot, use its
    # average price so marking-to-market never crashes.
    def px_of(sym, default=None):
        return live_prices.get(sym, default)

    # Day baseline = the last official close NAV (yesterday). Live day-P&L is
    # measured against this so it lines up with the EOD bot's day P&L.
    prev_value = state["history"][-1]["value"] if state["history"] else state["start_capital"]
    equity_peak = max([h.get("value", state["start_capital"]) for h in state["history"]] or
                      [state["start_capital"]])

    names = {}
    daily = ds.read_json("daily/latest.json", default={}) or {}
    regime = daily.get("market_regime") or {}
    for a in daily.get("analysis", []):
        names[a["symbol"]] = a.get("name", a["symbol"])

    sectors = _sector_map()
    trades = state.get("intraday_trades_today", [])
    risk_events = []
    # Reset the intraday trade log at the start of a new day.
    if state.get("intraday_date") != today:
        state["intraday_date"] = today
        trades = []
        state["intraday_risk_halt_date"] = None
    cost_total = 0.0
    intraday_deploy_enabled = REBALANCE_INTERVAL != "weekly"

    def mark_value():
        total = state["cash"]
        for s, pos in state["positions"].items():
            total += pos["qty"] * px_of(s, pos.get("avg_price", 0.0))
        return total

    # 1) Protective intraday STOP-LOSS exits.
    stops = []
    for sym in list(state["positions"].keys()):
        pos = state["positions"][sym]
        px = px_of(sym)
        if px is None:
            continue
        avg = pos.get("avg_price", 0.0)
        pos["peak_price"] = round(max(pos.get("peak_price", px), px), 2)
        stop_reason = None
        if avg and px <= avg * (1 - STOP_LOSS_PCT):
            stop_reason = "stop_loss"
        elif px <= pos["peak_price"] * (1 - TRAILING_STOP_PCT):
            stop_reason = "trailing_stop"
        if stop_reason:
            cost_total = _sell_position(state, sym, px, stop_reason, names, ist, trades, stops, cost_total)

    # 2) Fast intraday shock guard. This is separate from the larger daily
    # loss guard: it reacts to same-session broad weakness and exits weak names
    # before a full 8% stop-loss is hit.
    total_value = mark_value()
    live_day_pnl_pct = (total_value / prev_value - 1) if prev_value else 0.0
    shock = _shock_metrics(snapshot_rows)
    watchlist_shock = (
        shock["watchlist_down_fraction"] >= INTRADAY_SHOCK_WATCHLIST_DOWN_FRACTION and
        shock["watchlist_median_chg_pct"] <= -INTRADAY_SHOCK_WATCHLIST_MEDIAN_DROP_PCT * 100
    )
    shock_mode = (
        live_day_pnl_pct <= -INTRADAY_SHOCK_BOOK_LOSS_PCT or
        shock["index_drop_pct"] <= -INTRADAY_SHOCK_INDEX_DROP_PCT * 100 or
        watchlist_shock
    )
    if state.get("intraday_risk_halt_date") == today:
        shock_mode = True
    if shock_mode:
        state["intraday_risk_halt_date"] = today
        risk_events.append({
            "type": "intraday_shock_guard",
            "book_day_pnl_pct": round(live_day_pnl_pct * 100, 3),
            **shock,
        })
        for sym in list(state["positions"].keys()):
            pos = state["positions"][sym]
            px = px_of(sym)
            if px is None:
                continue
            avg = pos.get("avg_price", 0.0)
            pnl_pct = (px / avg - 1) if avg else 0.0
            day_chg_pct = (snapshot_by_symbol.get(sym) or {}).get("chg_pct")
            day_chg = float(day_chg_pct) / 100 if day_chg_pct is not None else 0.0
            if pnl_pct <= -INTRADAY_WEAK_HOLDING_LOSS_PCT:
                cost_total = _sell_position(state, sym, px, "intraday_shock_loss_exit",
                                            names, ist, trades, stops, cost_total)
            elif day_chg <= -INTRADAY_WEAK_HOLDING_DAY_DROP_PCT:
                cost_total = _sell_position(state, sym, px, "intraday_shock_day_exit",
                                            names, ist, trades, stops, cost_total)

    # 3) Deploy idle cash intraday into under-allocated top targets.
    total_value = mark_value()
    idle = state["cash"]
    risk_block_new_buys = bool(shock_mode)
    if prev_value and (total_value / prev_value - 1) <= -MAX_DAILY_LOSS_PCT:
        risk_block_new_buys = True
        risk_events.append({"type": "daily_loss_guard",
                            "loss_pct": round((total_value / prev_value - 1) * 100, 2)})
    if equity_peak and (total_value / equity_peak - 1) <= -MAX_DRAWDOWN_PCT:
        risk_block_new_buys = True
        risk_events.append({"type": "drawdown_guard",
                            "drawdown_pct": round((total_value / equity_peak - 1) * 100, 2)})
    if USE_MARKET_REGIME_GUARD and regime and not regime.get("risk_on", True):
        risk_block_new_buys = True
        risk_events.append({"type": "market_regime_guard", "reason": regime.get("reason")})

    if (intraday_deploy_enabled and total_value > 0 and
            idle > MIN_DEPLOY_FRACTION * total_value and not risk_block_new_buys):
        targets = _pick_targets(daily.get("analysis", []))
        # Block names we just stopped out of (avoid immediate re-entry).
        stopped = {s["symbol"] for s in stops}
        targets = [t for t in targets if t["symbol"] not in stopped]

        base_budget = total_value / max(len(targets), 1) if targets else 0
        pos_cap = MAX_POSITION_PCT * total_value
        sector_cap = MAX_SECTOR_PCT * total_value
        sector_alloc = {}
        for s, pos in state["positions"].items():
            sec = sectors.get(s, "Unknown")
            sector_alloc[sec] = sector_alloc.get(sec, 0.0) + pos["qty"] * px_of(s, pos.get("avg_price", 0.0))

        for t in targets:
            sym = t["symbol"]
            px = px_of(sym, t.get("price"))
            if not px or px <= 0:
                continue
            sec = sectors.get(sym, "Unknown")
            held_qty = state["positions"].get(sym, {}).get("qty", 0)
            held_val = held_qty * px
            target_val = min(base_budget, pos_cap)
            room = sector_cap - sector_alloc.get(sec, 0.0)
            if room <= 0:
                continue
            target_val = min(target_val, held_val + room)
            want = target_val - held_val
            if want <= px:
                continue
            # Buys pay up by SLIPPAGE_BPS vs the reference price, plus costs.
            fill_px = px * (1 + SLIPPAGE_BPS / 10000.0)
            qty = int(want / (fill_px * (1 + COST_PER_SIDE)))
            qty = min(qty, int(state["cash"] / (fill_px * (1 + COST_PER_SIDE))))
            if qty <= 0:
                continue
            spend = qty * fill_px
            cost = spend * COST_PER_SIDE
            cost_total += cost
            state["cash"] -= spend + cost
            sector_alloc[sec] = sector_alloc.get(sec, 0.0) + spend
            old = state["positions"].get(sym)
            if old:
                nq = old["qty"] + qty
                old["avg_price"] = round((old["avg_price"] * old["qty"] + spend) / nq, 2)
                old["qty"] = nq
                old["peak_price"] = round(max(old.get("peak_price", fill_px), fill_px), 2)
            else:
                state["positions"][sym] = {"qty": qty, "avg_price": round(fill_px, 2),
                                           "peak_price": round(fill_px, 2), "name": names.get(sym, sym)}
            trades.append({"action": "BUY", "symbol": sym, "name": names.get(sym, sym),
                           "qty": qty, "price": round(fill_px, 2), "cost": round(cost, 2),
                           "reason": "intraday_deploy", "ist": ist.isoformat()})

    # 4) Mark to market live and publish.
    state["intraday_trades_today"] = trades
    state["total_costs"] = round(float(state.get("total_costs") or 0.0) + cost_total, 2)
    end_value = mark_value()
    day_pnl = round(end_value - prev_value, 2)
    day_pnl_pct = round(day_pnl / prev_value * 100, 3) if prev_value else 0.0
    total_pnl = round(end_value - state["start_capital"], 2)
    total_pnl_pct = round(total_pnl / state["start_capital"] * 100, 3)

    ds.write_json(STATE_FILE, state)

    positions_view = []
    for sym, pos in sorted(state["positions"].items(),
                           key=lambda kv: kv[1]["qty"] * px_of(kv[0], 0) or 0,
                           reverse=True):
        px = px_of(sym, pos["avg_price"])
        positions_view.append({
            "symbol": sym, "name": pos.get("name", sym), "qty": pos["qty"],
            "avg_price": pos["avg_price"], "price": round(px, 2),
            "value": round(pos["qty"] * px, 2),
            "pnl": round((px - pos["avg_price"]) * pos["qty"], 2),
            "peak_price": pos.get("peak_price"),
            "pnl_pct": round((px / pos["avg_price"] - 1) * 100, 2) if pos["avg_price"] else 0.0,
        })

    live = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "market_open": True,
        "as_of_prices": "intraday/latest.json",
        **_price_source(),
        "start_capital": state["start_capital"],
        "value": round(end_value, 2),
        "cash": round(state["cash"], 2),
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "total_costs": state["total_costs"],
        "n_positions": len(state["positions"]),
        "intraday_trades": trades,
        "stops": stops,
        "risk_events": risk_events,
        "intraday_shock_mode": bool(shock_mode),
        "intraday_shock_metrics": shock,
        "risk_block_new_buys": risk_block_new_buys,
        "intraday_deploy_enabled": intraday_deploy_enabled,
        "active_profile": ACTIVE_PROFILE,
        "risk_settings": strategy_metadata(),
        "market_regime": regime,
        "positions": positions_view,
        "note": ("Delayed intraday mark-to-market of the virtual Rs 5L "
                 "book. Stop-loss checks run every ~15 minutes; weekly allocation "
                 "happens in the end-of-day paper trader. Simulation only — not "
                 "investment advice."),
    }
    ds.write_json(LIVE_FILE, live)

    print(f"[LIVE {ist.strftime('%H:%M')}] value Rs {end_value:,.0f} | "
          f"day {day_pnl:+,.0f} ({day_pnl_pct:+.2f}%) | "
          f"{len(stops)} stops | {len([t for t in trades if t['action']=='BUY'])} buys today | "
          f"{len(state['positions'])} holdings")
    return live


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
