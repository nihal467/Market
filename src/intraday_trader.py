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
  - Deploys idle cash intraday into the day's top BUY-ranked targets (from
    daily/latest.json) that it is under-allocated to, respecting the same
    position and sector caps. It does NOT trim winners intraday — full
    rebalancing happens once at the close (paper_trader.py), which keeps
    turnover (and costs) sane.

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
    COST_PER_SIDE,
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    STATE_FILE,
    STOP_LOSS_PCT,
    TOP_N,
    _load_state,
    _market_value,
    _pick_targets,
    _sector_map,
)

LIVE_FILE = "paper/live.json"

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

    live_prices = _live_prices()
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

    names = {}
    daily = ds.read_json("daily/latest.json", default={}) or {}
    for a in daily.get("analysis", []):
        names[a["symbol"]] = a.get("name", a["symbol"])

    sectors = _sector_map()
    trades = state.get("intraday_trades_today", [])
    # Reset the intraday trade log at the start of a new day.
    if state.get("intraday_date") != today:
        state["intraday_date"] = today
        trades = []
    cost_total = 0.0

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
        if avg and px <= avg * (1 - STOP_LOSS_PCT):
            proceeds = pos["qty"] * px
            cost = proceeds * COST_PER_SIDE
            cost_total += cost
            state["cash"] += proceeds - cost
            loss_pct = round((px / avg - 1) * 100, 2)
            t = {"action": "SELL", "symbol": sym, "name": names.get(sym, sym),
                 "qty": pos["qty"], "price": round(px, 2), "reason": "stop_loss",
                 "ist": ist.isoformat()}
            trades.append(t)
            stops.append({"symbol": sym, "name": names.get(sym, sym), "loss_pct": loss_pct})
            state["positions"].pop(sym)

    # 2) Deploy idle cash intraday into under-allocated top targets.
    total_value = mark_value()
    idle = state["cash"]
    if total_value > 0 and idle > MIN_DEPLOY_FRACTION * total_value:
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
            qty = int(want / (px * (1 + COST_PER_SIDE)))
            qty = min(qty, int(state["cash"] / (px * (1 + COST_PER_SIDE))))
            if qty <= 0:
                continue
            spend = qty * px
            cost = spend * COST_PER_SIDE
            cost_total += cost
            state["cash"] -= spend + cost
            sector_alloc[sec] = sector_alloc.get(sec, 0.0) + spend
            old = state["positions"].get(sym)
            if old:
                nq = old["qty"] + qty
                old["avg_price"] = round((old["avg_price"] * old["qty"] + spend) / nq, 2)
                old["qty"] = nq
            else:
                state["positions"][sym] = {"qty": qty, "avg_price": round(px, 2),
                                           "name": names.get(sym, sym)}
            trades.append({"action": "BUY", "symbol": sym, "name": names.get(sym, sym),
                           "qty": qty, "price": round(px, 2), "reason": "intraday_deploy",
                           "ist": ist.isoformat()})

    # 3) Mark to market live and publish.
    state["intraday_trades_today"] = trades
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
            "pnl_pct": round((px / pos["avg_price"] - 1) * 100, 2) if pos["avg_price"] else 0.0,
        })

    live = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "market_open": True,
        "as_of_prices": "intraday/latest.json",
        "start_capital": state["start_capital"],
        "value": round(end_value, 2),
        "cash": round(state["cash"], 2),
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "n_positions": len(state["positions"]),
        "intraday_trades": trades,
        "stops": stops,
        "positions": positions_view,
        "note": ("Live (~15-min) intraday mark-to-market of the virtual Rs 5L "
                 "book. Stop-losses execute in real time; full rebalance is at "
                 "the close. Simulation only — not investment advice."),
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
