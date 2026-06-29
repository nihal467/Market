"""Phase 1: Portfolio tracker.

Reads holdings.yaml, fetches current stock prices and MF NAVs, and computes
current value, invested amount, profit/loss and allocation. Writes a snapshot
to data/portfolio.json and prints a summary.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import yaml

from fetch_nav import get_mf_navs
from fetch_prices import get_stock_prices

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS_FILE = os.path.join(ROOT, "holdings.yaml")
OUTPUT_FILE = os.path.join(ROOT, "data", "portfolio.json")


def load_holdings(path: str = HOLDINGS_FILE) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build_snapshot(holdings: dict) -> dict:
    positions: list[dict] = []

    # Equity groups: "stocks" (Groww SIP) plus any extra groups under
    # "equity_groups" (e.g. the 5paisa lumpsum ETF basket).
    equity_groups: list[dict] = []
    if holdings.get("stocks"):
        equity_groups.append(holdings["stocks"])
    equity_groups.extend(holdings.get("equity_groups") or [])

    all_symbols = [
        h["symbol"]
        for group in equity_groups
        for h in (group.get("holdings") or [])
        if h.get("symbol")
    ]
    prices = get_stock_prices(all_symbols)

    for group in equity_groups:
        default_broker = group.get("broker", "Groww")
        kind = group.get("type", "stock")
        for h in group.get("holdings") or []:
            qty = float(h.get("quantity") or 0)
            invested = float(h.get("invested") or 0)
            price = prices.get(h.get("symbol"))
            positions.append(_position(
                kind=kind,
                broker=default_broker,
                name=h.get("name", h.get("symbol")),
                identifier=h.get("symbol"),
                units=qty,
                invested=invested,
                unit_price=price,
            ))

    mfs = holdings.get("mutual_funds") or {}
    mf_holdings = mfs.get("holdings") or []
    codes = [str(h["scheme_code"]) for h in mf_holdings if h.get("scheme_code")]
    navs = get_mf_navs(codes)

    for h in mf_holdings:
        units = float(h.get("units") or 0)
        invested = float(h.get("invested") or 0)
        nav = navs.get(str(h.get("scheme_code")))
        positions.append(_position(
            kind="mutual_fund",
            broker=mfs.get("broker", "5paisa"),
            name=h.get("name", h.get("scheme_code")),
            identifier=str(h.get("scheme_code")),
            units=units,
            invested=invested,
            unit_price=nav,
        ))

    total_value = sum(p["current_value"] or 0 for p in positions)
    total_invested = sum(p["invested"] for p in positions)
    total_pnl = total_value - total_invested

    for p in positions:
        p["allocation_pct"] = (
            round((p["current_value"] or 0) / total_value * 100, 2)
            if total_value else 0.0
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "invested": round(total_invested, 2),
            "current_value": round(total_value, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested else 0.0,
        },
        "positions": positions,
    }


def _position(kind, broker, name, identifier, units, invested, unit_price) -> dict:
    current_value = round(units * unit_price, 2) if unit_price is not None else None
    pnl = round(current_value - invested, 2) if current_value is not None else None
    pnl_pct = (
        round(pnl / invested * 100, 2)
        if (pnl is not None and invested) else None
    )
    return {
        "type": kind,
        "broker": broker,
        "name": name,
        "identifier": identifier,
        "units": units,
        "invested": round(invested, 2),
        "unit_price": unit_price,
        "current_value": current_value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
    }


def save_snapshot(snapshot: dict, path: str = OUTPUT_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)


def print_summary(snapshot: dict) -> None:
    t = snapshot["totals"]
    print("\n=== Portfolio Snapshot ===")
    print(f"Generated: {snapshot['generated_at']}")
    print(f"{'Name':<28}{'Broker':<10}{'Value':>12}{'P&L':>12}{'Alloc%':>8}")
    print("-" * 70)
    for p in snapshot["positions"]:
        value = "n/a" if p["current_value"] is None else f"{p['current_value']:,.0f}"
        pnl = "n/a" if p["pnl"] is None else f"{p['pnl']:,.0f}"
        print(f"{p['name'][:27]:<28}{p['broker']:<10}{value:>12}{pnl:>12}{p['allocation_pct']:>8}")
    print("-" * 70)
    print(f"{'TOTAL':<38}{t['current_value']:>12,.0f}{t['pnl']:>12,.0f}")
    print(f"Invested: {t['invested']:,.0f}   P&L: {t['pnl']:,.0f} ({t['pnl_pct']}%)\n")


def main() -> None:
    holdings = load_holdings()
    snapshot = build_snapshot(holdings)
    save_snapshot(snapshot)
    print_summary(snapshot)
    print(f"Saved snapshot -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
