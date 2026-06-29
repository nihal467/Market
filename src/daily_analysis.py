"""Daily 24h analysis — full end-of-day pass over the watchlist.

Runs once a day (after close, and again overnight if desired). For each Top-50
watchlist symbol it computes full indicators + news sentiment + a BUY/HOLD/SELL
signal, then DIFFS against yesterday's analysis to surface what changed
(signal flips, RSI regime changes, new 52-week highs/lows). Writes a
date-partitioned daily file plus latest.json.
"""
from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

import datastore as ds
from market_calendar import now_ist
from news_sentiment import get_news_sentiment
from strategy import decide

BATCH = 50


def _indicators_from_close(close: pd.Series) -> dict | None:
    close = close.dropna()
    if len(close) < 30:
        return None
    last = float(close.iloc[-1])

    def sma(w):
        return round(float(close.rolling(w).mean().iloc[-1]), 2) if len(close) >= w else None

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    lg, ll = float(gain.iloc[-1]), float(loss.iloc[-1])
    rsi = 100.0 if (ll == 0 and lg > 0) else (50.0 if ll == 0 else round(100 - 100 / (1 + lg / ll), 2))

    high52 = round(float(close.max()), 2)
    low52 = round(float(close.min()), 2)
    ret_3m = None
    if len(close) > 63:
        past = float(close.iloc[-63])
        if past:
            ret_3m = round((last / past - 1) * 100, 2)
    return {
        "price": round(last, 2),
        "sma50": sma(50),
        "sma200": sma(200),
        "rsi14": rsi,
        "week52_high": high52,
        "week52_low": low52,
        "pct_from_high": round((last - high52) / high52 * 100, 2),
        "pct_from_low": round((last - low52) / low52 * 100, 2),
        "ret_3m": ret_3m,
    }


def _download(symbols: list[str]) -> dict[str, pd.Series]:
    closes: dict[str, pd.Series] = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        try:
            data = yf.download(chunk, period="1y", interval="1d", auto_adjust=True,
                               progress=False, group_by="ticker", threads=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! download failed: {exc}")
            continue
        for sym in chunk:
            try:
                s = (data["Close"] if len(chunk) == 1 else data[sym]["Close"]).dropna()
                if len(s) >= 30:
                    closes[sym] = s
            except (KeyError, TypeError):
                continue
    return closes


def _watchlist() -> list[dict]:
    wl = ds.read_json("watchlist/latest.json", default={}) or {}
    return [r for r in wl.get("watchlist", []) if r.get("symbol")]


def run() -> dict:
    ist = now_ist()
    watch = _watchlist()
    if not watch:
        print("No watchlist yet — run watchlist.py first.")
        return {"skipped": True}

    symbols = [w["symbol"] for w in watch]
    name_by = {w["symbol"]: w["name"] for w in watch}
    print(f"Daily analysis for {len(symbols)} watchlist symbols ...")

    closes = _download(symbols)
    prev = (ds.read_json("daily/latest.json", default={}) or {})
    prev_by = {r["symbol"]: r for r in prev.get("analysis", [])}

    results = []
    changes = []
    for sym in symbols:
        series = closes.get(sym)
        if series is None:
            continue
        ind = _indicators_from_close(series)
        sent = get_news_sentiment(f"{name_by[sym]} share price NSE")
        rec = decide(ind, sent)
        row = {
            "symbol": sym,
            "name": name_by[sym],
            "signal": rec["signal"],
            "score": rec["score"],
            "rsi14": ind["rsi14"] if ind else None,
            "price": ind["price"] if ind else None,
            "pct_from_high": ind["pct_from_high"] if ind else None,
            "reasons": rec["reasons"],
            "news_score": sent.get("score"),
            "components": rec.get("components", {}),
        }
        results.append(row)

        # Diff vs yesterday.
        old = prev_by.get(sym)
        if old:
            if old.get("signal") != row["signal"]:
                changes.append({
                    "symbol": sym, "name": name_by[sym], "type": "signal_flip",
                    "from": old.get("signal"), "to": row["signal"],
                })
            old_rsi, new_rsi = old.get("rsi14"), row["rsi14"]
            if old_rsi is not None and new_rsi is not None:
                if old_rsi >= 30 > new_rsi:
                    changes.append({"symbol": sym, "name": name_by[sym],
                                    "type": "rsi_entered_oversold", "rsi": new_rsi})
                elif old_rsi <= 70 < new_rsi:
                    changes.append({"symbol": sym, "name": name_by[sym],
                                    "type": "rsi_entered_overbought", "rsi": new_rsi})

    counts = {"BUY": 0, "HOLD": 0, "SELL": 0}
    for r in results:
        if r["signal"] in counts:
            counts[r["signal"]] += 1

    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "watchlist_size": len(symbols),
        "analyzed": len(results),
        "signal_counts": counts,
        "changes_since_prev": changes,
        "analysis": results,
        "disclaimer": "Rule-based EOD analysis. Not investment advice.",
    }
    ds.write_json(ds.daily_path(ist), payload)
    ds.write_json("daily/latest.json", payload)
    print(f"  analyzed {len(results)} | signals {counts} | {len(changes)} changes")
    return payload


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
