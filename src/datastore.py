"""Helpers for writing the time-series market data store.

All market data (watchlists, intraday snapshots, daily analysis) is written
under a single output root so it can be published to a dedicated ``data``
branch (or a separate data repo) without bloating the code branch history.

Layout (date-partitioned to keep files small and appendable):

    <root>/watchlist/YYYY-WW.json          # weekly Top-50
    <root>/watchlist/latest.json           # newest weekly watchlist
    <root>/intraday/YYYY/MM/DD.jsonl       # one JSON line per 15-min run
    <root>/intraday/latest.json            # newest intraday snapshot
    <root>/daily/YYYY/MM/DD.json           # end-of-day analysis
    <root>/daily/latest.json               # newest daily analysis

The output root is controlled by the ``MARKET_DATA_DIR`` env var so that CI
can point it at a checkout of the ``data`` branch. Defaults to ``data_out``
in the repo root for local runs.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_root() -> str:
    root = os.environ.get("MARKET_DATA_DIR") or os.path.join(ROOT, "data_out")
    os.makedirs(root, exist_ok=True)
    return root


def _path(*parts: str) -> str:
    p = os.path.join(data_root(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def write_json(rel_path: str, obj) -> str:
    """Write a JSON file (pretty) under the data root. Returns the full path."""
    full = _path(*rel_path.split("/"))
    with open(full, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
    return full


def append_jsonl(rel_path: str, obj) -> str:
    """Append one compact JSON line under the data root. Returns the full path."""
    full = _path(*rel_path.split("/"))
    with open(full, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, separators=(",", ":"), default=str) + "\n")
    return full


def read_json(rel_path: str, default=None):
    full = os.path.join(data_root(), *rel_path.split("/"))
    if not os.path.exists(full):
        return default
    try:
        with open(full, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


# --- path builders -----------------------------------------------------------

def weekly_path(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    iso = dt.isocalendar()
    return f"watchlist/{iso.year}-W{iso.week:02d}.json"


def intraday_path(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    return f"intraday/{dt.year}/{dt.month:02d}/{dt.day:02d}.jsonl"


def daily_path(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    return f"daily/{dt.year}/{dt.month:02d}/{dt.day:02d}.json"
