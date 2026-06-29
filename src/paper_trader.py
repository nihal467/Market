"""Paper-trading bot: simulate investing a virtual Rs 5,00,000 using the daily
signals, mark to market after each market close, log daily P&L, and persist
everything to the data store (data branch).

Strategy (transparent, rule-based — NOT advice):
  - Start with Rs 5,00,000 in virtual cash on inception.
  - Each trading day, after close, read the latest end-of-day analysis
    (data/daily/latest.json) which holds a BUY/HOLD/SELL signal + score +
    close price for every Top-50 watchlist stock.
  - Target portfolio = the TOP_N highest-scoring BUY-signal stocks, equally
    weighted. Anything currently held that is no longer a BUY is SOLD; new
    BUYs are bought with the freed cash (integer share quantities).
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

import datastore as ds
from market_calendar import now_ist

START_CAPITAL = 500000.0
TOP_N = 10               # equal-weight this many top BUY-ranked stocks
COST_PER_SIDE = 0.0010   # 0.10% per buy/sell leg (brokerage+slippage+taxes proxy)

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
    }


def _load_state() -> dict:
    st = ds.read_json(STATE_FILE, default=None)
    if not st:
        return _empty_state()
    # Backfill any missing keys for forward-compatibility.
    base = _empty_state()
    base.update(st)
    return base


def _market_value(positions: dict, prices: dict) -> float:
    total = 0.0
    for sym, pos in positions.items():
        px = prices.get(sym, pos.get("avg_price", 0.0))
        total += pos["qty"] * px
    return total


def _pick_targets(analysis: list[dict]) -> list[dict]:
    buys = [a for a in analysis if a.get("signal") == "BUY" and a.get("price")]
    buys.sort(key=lambda a: (a.get("score", 0), -(a.get("pct_from_high") or 0)),
              reverse=True)
    return buys[:TOP_N]


def run() -> dict:
    ist = now_ist()
    today = ist.strftime("%Y-%m-%d")

    daily = ds.read_json("daily/latest.json", default=None)
    if not daily or not daily.get("analysis"):
        print("No daily analysis available yet — run daily_analysis first.")
        return {"skipped": True, "reason": "no_daily"}

    analysis = daily["analysis"]
    prices = {a["symbol"]: a["price"] for a in analysis if a.get("price")}
    names = {a["symbol"]: a.get("name", a["symbol"]) for a in analysis}

    state = _load_state()
    if state["last_date"] == today:
        print(f"Paper trade already run for {today}. Skipping (idempotent).")
        return {"skipped": True, "reason": "already_done", "date": today}

    if state["inception"] is None:
        state["inception"] = today

    # Day P&L is measured as today's closing NAV minus the PREVIOUS trading
    # day's stored closing NAV (true day-over-day mark-to-market). On the first
    # ever run there is no prior snapshot, so we anchor to the start capital.
    prev_value = state["history"][-1]["value"] if state["history"] else state["start_capital"]

    targets = _pick_targets(analysis)
    target_syms = {t["symbol"] for t in targets}
    trades: list[dict] = []
    cost_total = 0.0

    # 1) SELL everything not in the new target set (signal no longer BUY).
    for sym in list(state["positions"].keys()):
        if sym not in target_syms:
            pos = state["positions"].pop(sym)
            px = prices.get(sym, pos.get("avg_price", 0.0))
            proceeds = pos["qty"] * px
            cost = proceeds * COST_PER_SIDE
            cost_total += cost
            state["cash"] += proceeds - cost
            trades.append({"action": "SELL", "symbol": sym,
                           "name": names.get(sym, sym), "qty": pos["qty"],
                           "price": round(px, 2)})

    # 2) Determine equal-weight budget per target from total investable value.
    total_value = state["cash"] + _market_value(state["positions"], prices)
    if targets:
        budget = total_value / len(targets)
        # 3) BUY/top-up each target toward the equal-weight budget.
        for t in targets:
            sym = t["symbol"]
            px = prices[sym]
            held_qty = state["positions"].get(sym, {}).get("qty", 0)
            held_val = held_qty * px
            want_val = budget - held_val
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
            old = state["positions"].get(sym)
            if old:
                new_qty = old["qty"] + buy_qty
                old["avg_price"] = round(
                    (old["avg_price"] * old["qty"] + spend) / new_qty, 2)
                old["qty"] = new_qty
            else:
                state["positions"][sym] = {
                    "qty": buy_qty, "avg_price": round(px, 2),
                    "name": names.get(sym, sym)}
            trades.append({"action": "BUY", "symbol": sym,
                           "name": names.get(sym, sym), "qty": buy_qty,
                           "price": round(px, 2)})

    # 4) Mark to market at today's close.
    end_value = state["cash"] + _market_value(state["positions"], prices)
    day_pnl = round(end_value - prev_value, 2)
    day_pnl_pct = round(day_pnl / prev_value * 100, 3) if prev_value else 0.0
    total_pnl = round(end_value - state["start_capital"], 2)
    total_pnl_pct = round(total_pnl / state["start_capital"] * 100, 3)

    snapshot = {
        "date": today,
        "value": round(end_value, 2),
        "cash": round(state["cash"], 2),
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "n_positions": len(state["positions"]),
        "n_trades": len(trades),
        "costs": round(cost_total, 2),
    }

    state["last_date"] = today
    state["history"].append(snapshot)
    state["history"] = state["history"][-400:]  # keep ~1.5 yrs of trading days

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
        "n_positions": len(state["positions"]),
        "today_trades": trades,
        "positions": positions_view,
        "history": state["history"][-120:],
        "strategy": (
            f"Equal-weight top {TOP_N} BUY-ranked stocks from the daily Top-50 "
            f"analysis, rebalanced each close. {COST_PER_SIDE*100:.2f}% cost per "
            "trade leg. Simulation only — not investment advice."
        ),
    }
    ds.write_json(LATEST_FILE, latest)

    print(f"[{today}] value Rs {end_value:,.0f} | day {day_pnl:+,.0f} "
          f"({day_pnl_pct:+.2f}%) | total {total_pnl:+,.0f} "
          f"({total_pnl_pct:+.2f}%) | {len(trades)} trades | "
          f"{len(state['positions'])} holdings")
    return latest


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
