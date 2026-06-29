"""Phase 2: Daily signals.

For each holding in holdings.yaml, compute technical indicators + news
sentiment, then produce a BUY/HOLD/SELL recommendation. Writes
data/signals.json and prints a summary.

Mutual funds (no intraday chart) are reported as HOLD with NAV info only,
since RSI/MA on daily NAV is noisy — they are long-term SIP instruments.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import yaml

from indicators import compute_indicators
from news_sentiment import get_news_sentiment
from strategy import decide

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS_FILE = os.path.join(ROOT, "holdings.yaml")
OUTPUT_FILE = os.path.join(ROOT, "data", "signals.json")

DISCLAIMER = (
    "Rule-based signals from indicators + news. Not investment advice. "
    "Verify before acting."
)


def load_holdings() -> dict:
    with open(HOLDINGS_FILE, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def iter_equities(holdings: dict):
    groups: list[dict] = []
    if holdings.get("stocks"):
        groups.append(holdings["stocks"])
    groups.extend(holdings.get("equity_groups") or [])
    for group in groups:
        broker = group.get("broker", "")
        for h in group.get("holdings") or []:
            if h.get("symbol"):
                yield broker, h


def run() -> dict:
    holdings = load_holdings()
    results: list[dict] = []

    for broker, h in iter_equities(holdings):
        symbol = h["symbol"]
        name = h.get("name", symbol)
        print(f"Analyzing {name} ({symbol}) ...")
        ind = compute_indicators(symbol)
        query = _news_query(name)
        sent = get_news_sentiment(query)
        rec = decide(ind, sent)
        results.append({
            "broker": broker,
            "name": name,
            "symbol": symbol,
            "signal": rec["signal"],
            "score": rec["score"],
            "reasons": rec["reasons"],
            "indicators": ind,
            "sentiment": sent,
        })

    # Mutual funds -> informational HOLD only.
    mfs = holdings.get("mutual_funds") or {}
    for h in mfs.get("holdings") or []:
        results.append({
            "broker": mfs.get("broker", ""),
            "name": h.get("name", h.get("scheme_code")),
            "symbol": f"MF:{h.get('scheme_code')}",
            "signal": "HOLD",
            "score": 0,
            "reasons": ["Mutual fund SIP — continue investing; not a trade signal"],
            "indicators": None,
            "sentiment": None,
        })

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": DISCLAIMER,
        "signals": results,
    }
    _save(snapshot)
    _print(snapshot)
    return snapshot


def _news_query(name: str) -> str:
    # Strip parenthetical tickers and ETF noise for cleaner news search.
    base = name.split("(")[0].strip()
    return f"{base} India stock market"


def _save(snapshot: dict) -> None:
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2)


def _print(snapshot: dict) -> None:
    print("\n=== Daily Signals ===")
    print(f"Generated: {snapshot['generated_at']}")
    print(f"{'Signal':<7}{'Name':<40}{'RSI':>7}{'Score':>7}")
    print("-" * 65)
    order = {"BUY": 0, "SELL": 1, "HOLD": 2}
    for s in sorted(snapshot["signals"], key=lambda x: order.get(x["signal"], 3)):
        rsi = s["indicators"]["rsi14"] if s["indicators"] else None
        rsi_str = "-" if rsi is None else f"{rsi}"
        print(f"{s['signal']:<7}{s['name'][:39]:<40}{rsi_str:>7}{s['score']:>7}")
    print("-" * 65)
    print(f"\n{snapshot['disclaimer']}")
    print(f"Saved -> {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
