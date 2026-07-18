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
import time
from typing import Optional

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
DATA_SOURCE = "yfinance"
SOURCE_LABEL = "Yahoo Finance"
REALTIME = False
DELAY_NOTE = "~15-min delayed"
CHUNK_SIZE = 25
DOWNLOAD_RETRIES = 3


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


def _download(symbols: list[str], *, period: str, interval: str) -> Optional[pd.DataFrame]:
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            # auto_adjust=True so intraday marks/stops use the SAME adjusted
            # price basis as the EOD book and backtest (one price basis).
            data = yf.download(
                symbols, period=period, interval=interval,
                auto_adjust=True, progress=False, group_by="ticker", threads=True,
            )
            if data is not None and not data.empty:
                return data
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {interval} download attempt {attempt}/{DOWNLOAD_RETRIES} failed: {exc}")
        time.sleep(attempt * 2)
    return None


def _extract_close(data: Optional[pd.DataFrame], sym: str, *, single: bool) -> pd.Series:
    if data is None or data.empty:
        return pd.Series(dtype="float64")
    try:
        close = data["Close"] if single else data[sym]["Close"]
        return close.dropna()
    except (KeyError, TypeError):
        return pd.Series(dtype="float64")


def _snapshot_chunk(symbols: list[str]) -> dict[str, dict]:
    """Batched intraday + prev-close fetch for one small chunk."""
    out: dict[str, dict] = {}
    intraday = _download(symbols, period="1d", interval="15m")
    daily = _download(symbols, period="5d", interval="1d")
    single = len(symbols) == 1
    for sym in symbols:
        try:
            intr = _extract_close(intraday, sym, single=single)
            day = _extract_close(daily, sym, single=single)
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


def _snapshot(symbols: list[str]) -> dict[str, dict]:
    """Fetch in chunks to reduce Yahoo rate-limit and partial-failure pain."""
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), CHUNK_SIZE):
        chunk = symbols[i:i + CHUNK_SIZE]
        print(f"  fetching {len(chunk)} symbols from {SOURCE_LABEL}...")
        out.update(_snapshot_chunk(chunk))
        time.sleep(1)
    missing = [sym for sym in symbols if sym not in out]
    if missing:
        print(f"  retrying {len(missing)} missing symbols individually...")
        for sym in missing:
            out.update(_snapshot_chunk([sym]))
            time.sleep(1)
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
    missing_symbols = [sym for sym in symbols if sym not in snaps]
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
    if not rows:
        print(f"  ! no prices captured from {SOURCE_LABEL}; leaving previous snapshot untouched.")
        return {"skipped": True, "reason": "no_prices", "source": DATA_SOURCE}

    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "phase": phase,
        "count": len(rows),
        "expected_count": len(symbols),
        "coverage_pct": round(len(rows) / len(symbols) * 100, 2) if symbols else 0.0,
        "quality": "ok" if symbols and len(rows) / len(symbols) >= 0.8 else "partial",
        "data_source": DATA_SOURCE,
        "source_label": SOURCE_LABEL,
        "realtime": REALTIME,
        "delay": DELAY_NOTE,
        "movers": movers,
        "snapshots": rows,
        "missing_symbols": missing_symbols,
    }
    # Compact line for the day's intraday log + full latest snapshot.
    ds.append_jsonl(ds.intraday_path(ist), {
        "ist": ist.isoformat(),
        "phase": phase,
        "data_source": DATA_SOURCE,
        "realtime": REALTIME,
        "movers": [
            {"symbol": m["symbol"], "chg_pct": m["chg_pct"], "rsi": m["rsi"],
             "flags": m["flags"]}
            for m in movers
        ],
        "n": len(rows),
        "missing_symbols": missing_symbols,
    })
    ds.write_json("intraday/latest.json", payload)
    if missing_symbols:
        print(f"  ! missing {len(missing_symbols)} symbols: {', '.join(missing_symbols[:20])}")
    print(f"  captured {len(rows)} snapshots, {len(movers)} movers flagged.")
    return payload


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
