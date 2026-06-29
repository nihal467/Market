"""Load the NSE stock universe from ``universe.csv`` at the repo root.

This is a curated Nifty-200-style list of liquid NSE names in Yahoo Finance
format (SYMBOL.NS). It is shipped in-repo so the pipeline never depends on
NSE's website (which blocks GitHub Actions IP ranges). Edit ``universe.csv``
to add/remove names.
"""
from __future__ import annotations

import csv
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIVERSE_FILE = os.path.join(ROOT, "universe.csv")


def load_universe() -> list[dict]:
    rows: list[dict] = []
    with open(UNIVERSE_FILE, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip()
            if sym:
                rows.append({
                    "symbol": sym,
                    "name": (row.get("name") or sym).strip(),
                    "sector": (row.get("sector") or "").strip(),
                })
    return rows


def symbols() -> list[str]:
    return [r["symbol"] for r in load_universe()]


if __name__ == "__main__":
    u = load_universe()
    print(f"{len(u)} symbols loaded; first 5: {[r['symbol'] for r in u[:5]]}")
