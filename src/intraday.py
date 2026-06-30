"""Intraday monitor — runs every 15 minutes during market hours.

Watches ONLY the current Top-50 weekly watchlist (plus a couple of index
proxies) to stay light and avoid rate limits. For each symbol it captures the
last price, % change vs previous close, and a short-interval RSI, then flags
notable movers (large moves or RSI extremes). One compact JSON line is
appended per run to the date-partitioned intraday log, and a `latest.json`
snapshot is written for the dashboard.

IMPORTANT: GitHub Actions cron is not precise (runs can be 5-20+ min late or
skipped). Treat these snapshots as near-real-time monitoring, NOT as a
low-latency trading trigger. The job no-ops outside market hours.
"""
from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

import datastore as ds
from market_calendar import is_market_open, now_ist, session_phase

INDEX_PROXIES = [
    {"symbol": "NIFTYBEES.NS", "name": "Nifty 50 (proxy)"},
    {"symbol": "JUNIORBEES.NS", "name": "Nifty Next 50 (proxy)"},
    {"symbol": "MID150BEES.NS", "name": "Nifty Midcap 150 (proxy)"},
]

MOVE_THRESHOLD = 2.0  # abs % change vs prev close to flag as a mover


def _watchlist_symbols() -> list[dict]:
    wl = ds.read_json("watchlist/latest.json", default={}) or {}
    items = [
        {"symbol": r["symbol"], "name": r["name"]}
        for r in wl.get("watchlist", [])
        if r.get("symbol")
    ]
    return INDEX_PROXIES + items


def _intraday_rsi(series: pd.Series, period: int = 14):
    series = series.dropna()
    if len(series) < period + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    lg, ll = float(gain.iloc[-1]), float(loss.iloc[-1])
    if ll == 0:
        return 100.0 if lg > 0 else 50.0
    return round(100 - (100 / (1 + lg / ll)), 2)


def _snapshot(symbols: list[str]) -> dict[str, dict]:
    """Batched intraday + prev-close fetch for all symbols."""
    out: dict[str, dict] = {}
    try:
        intraday = yf.download(
            symbols, period="1d", interval="15m",
            auto_adjust=False, progress=False, group_by="ticker", threads=True,
        )
        daily = yf.download(
            symbols, period="5d", interval="1d",
            auto_adjust=False, progress=False, group_by="ticker", threads=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ! intraday download failed: {exc}")
        return out

    single = len(symbols) == 1
    for sym in symbols:
        try:
            intr = intraday["Close"] if single else intraday[sym]["Close"]
            day = daily["Close"] if single else daily[sym]["Close"]
            intr = intr.dropna()
            day = day.dropna()
            if intr.empty or len(day) < 2:
                continue
            last = round(float(intr.iloc[-1]), 2)
            prev_close = float(day.iloc[-2])
            chg = round((last - prev_close) / prev_close * 100, 2) if prev_close else None
            out[sym] = {
                "price": last,
                "prev_close": round(prev_close, 2),
                "chg_pct": chg,
                "rsi": _intraday_rsi(intr),
            }
        except (KeyError, TypeError, IndexError):
            continue

    return out


def run() -> dict:
    ist = now_ist()
    phase = session_phase(ist)
    if not is_market_open(ist):
        print(f"Market not open (phase={phase}, IST={ist.isoformat()}). Skipping.")
        return {"skipped": True, "phase": phase, "ist": ist.isoformat()}

    watch = _watchlist_symbols()
    symbols = [w["symbol"] for w in watch]
    name_by = {w["symbol"]: w["name"] for w in watch}
    print(f"Intraday snapshot for {len(symbols)} symbols at {ist.isoformat()} ...")

    snaps = _snapshot(symbols)
    rows = []
    movers = []
    for sym in symbols:
        s = snaps.get(sym)
        if not s:
            continue
        row = {"symbol": sym, "name": name_by.get(sym, sym), **s}
        flags = []
        if s["chg_pct"] is not None and abs(s["chg_pct"]) >= MOVE_THRESHOLD:
            flags.append("big_move_up" if s["chg_pct"] > 0 else "big_move_down")
        if s["rsi"] is not None and s["rsi"] <= 30:
            flags.append("rsi_oversold")
        if s["rsi"] is not None and s["rsi"] >= 70:
            flags.append("rsi_overbought")
        row["flags"] = flags
        rows.append(row)
        if flags:
            movers.append(row)

    movers.sort(key=lambda r: abs(r.get("chg_pct") or 0), reverse=True)

    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "phase": phase,
        "count": len(rows),
        "movers": movers,
        "snapshots": rows,
    }
    # Compact line for the day's intraday log + full latest snapshot.
    ds.append_jsonl(ds.intraday_path(ist), {
        "ist": ist.isoformat(),
        "phase": phase,
        "movers": [
            {"symbol": m["symbol"], "chg_pct": m["chg_pct"], "rsi": m["rsi"],
             "flags": m["flags"]}
            for m in movers
        ],
        "n": len(rows),
    })
    ds.write_json("intraday/latest.json", payload)
    print(f"  captured {len(rows)} snapshots, {len(movers)} movers flagged.")
    return payload


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
