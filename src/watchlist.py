"""Weekly Top-50 watchlist builder.

Ranks the NSE universe by a transparent composite score combining technical
momentum/structure with recent news sentiment, and writes the top 50 names to
the data store. Runs once a week (heavy job).

Efficiency: indicators are computed from a SINGLE batched ``yf.download`` call
for the whole universe instead of one request per symbol — this is far less
likely to be rate-limited/blocked on GitHub Actions IPs. News sentiment (one
RSS request per name) is only fetched for the top technical candidates to keep
the run fast and gentle on the source.
"""
from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

import datastore as ds
from market_calendar import now_ist
from news_sentiment import get_news_sentiment
from universe import load_universe

TOP_N = 50
NEWS_REFINE_N = 70  # fetch news only for this many top technical candidates
BATCH = 50          # tickers per yf.download batch


def _sma(close: pd.Series, window: int):
    if close.dropna().shape[0] < window:
        return None
    return round(float(close.rolling(window).mean().iloc[-1]), 2)


def _rsi(close: pd.Series, period: int = 14):
    close = close.dropna()
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    lg, ll = float(gain.iloc[-1]), float(loss.iloc[-1])
    if ll == 0:
        return 100.0 if lg > 0 else 50.0
    return round(100 - (100 / (1 + lg / ll)), 2)


def _download(symbols: list[str]) -> dict[str, pd.Series]:
    """Return {symbol: close-series} using batched downloads."""
    closes: dict[str, pd.Series] = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        print(f"  downloading {i + 1}-{i + len(chunk)} of {len(symbols)} ...")
        try:
            data = yf.download(
                chunk, period="1y", interval="1d",
                auto_adjust=True, progress=False, group_by="ticker", threads=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! batch download failed: {exc}")
            continue
        for sym in chunk:
            try:
                if len(chunk) == 1:
                    series = data["Close"]
                else:
                    series = data[sym]["Close"]
                series = series.dropna()
                if len(series) >= 30:
                    closes[sym] = series
            except (KeyError, TypeError):
                continue
    return closes


def _tech_score(close: pd.Series) -> dict:
    last = float(close.iloc[-1])
    sma50 = _sma(close, 50)
    sma200 = _sma(close, 200)
    rsi = _rsi(close, 14)
    high52 = round(float(close.max()), 2)
    low52 = round(float(close.min()), 2)
    ret_1m = _pct_return(close, 21)
    ret_3m = _pct_return(close, 63)

    score = 0.0
    reasons: list[str] = []

    # Momentum: positive medium-term returns.
    if ret_3m is not None:
        score += max(min(ret_3m / 5.0, 4), -4)  # cap contribution
        reasons.append(f"3M return {ret_3m}%")
    if ret_1m is not None and ret_1m > 0:
        score += 1
        reasons.append(f"1M return {ret_1m}%")

    # Trend structure.
    if sma50 and last > sma50:
        score += 1.5
        reasons.append("Above SMA50")
    if sma50 and sma200 and sma50 > sma200:
        score += 1.5
        reasons.append("Golden cross (SMA50>SMA200)")

    # RSI: reward healthy momentum, penalise overbought / reward oversold bounce.
    if rsi is not None:
        if rsi < 30:
            score += 1.5
            reasons.append(f"RSI {rsi} oversold")
        elif rsi > 75:
            score -= 1.5
            reasons.append(f"RSI {rsi} overbought")
        elif 45 <= rsi <= 65:
            score += 1
            reasons.append(f"RSI {rsi} healthy")

    pct_from_high = round((last - high52) / high52 * 100, 2)
    if pct_from_high >= -5:
        score += 1
        reasons.append("Near 52w high (strength)")

    return {
        "price": round(last, 2),
        "rsi14": rsi,
        "sma50": sma50,
        "sma200": sma200,
        "week52_high": high52,
        "week52_low": low52,
        "ret_1m": ret_1m,
        "ret_3m": ret_3m,
        "pct_from_high": pct_from_high,
        "tech_score": round(score, 2),
        "reasons": reasons,
    }


def _pct_return(close: pd.Series, lookback: int):
    if len(close) <= lookback:
        return None
    past = float(close.iloc[-1 - lookback])
    if past == 0:
        return None
    return round((float(close.iloc[-1]) - past) / past * 100, 2)


def build() -> dict:
    universe = load_universe()
    by_symbol = {u["symbol"]: u for u in universe}
    symbols = list(by_symbol)
    print(f"Building watchlist from {len(symbols)} universe symbols ...")

    closes = _download(symbols)
    print(f"  got history for {len(closes)} symbols")

    scored = []
    for sym, series in closes.items():
        t = _tech_score(series)
        meta = by_symbol[sym]
        scored.append({
            "symbol": sym,
            "name": meta["name"],
            "sector": meta["sector"],
            **t,
            "composite": t["tech_score"],
        })

    # Refine the strongest technical candidates with news sentiment.
    scored.sort(key=lambda x: x["tech_score"], reverse=True)
    for row in scored[:NEWS_REFINE_N]:
        sent = get_news_sentiment(f"{row['name']} share price NSE")
        row["news"] = sent
        s = sent.get("score", 0) or 0
        row["composite"] = round(row["tech_score"] + s * 2, 2)  # news nudges rank
        if sent.get("count"):
            row["reasons"] = row["reasons"] + [f"News sentiment {s}"]

    scored.sort(key=lambda x: x["composite"], reverse=True)
    top = scored[:TOP_N]
    for i, row in enumerate(top, 1):
        row["rank"] = i

    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "generated_ist": now_ist().isoformat(),
        "universe_size": len(symbols),
        "evaluated": len(scored),
        "top_n": TOP_N,
        "watchlist": top,
        "disclaimer": (
            "Ranked by transparent technical + news rules. Not investment "
            "advice. Verify before acting."
        ),
    }
    ds.write_json(ds.weekly_path(), payload)
    ds.write_json("watchlist/latest.json", payload)
    print(f"Top {len(top)} watchlist written. #1: {top[0]['name']} ({top[0]['symbol']})"
          if top else "No watchlist produced.")
    return payload


if __name__ == "__main__":
    result = build()
    if not result.get("watchlist"):
        sys.exit(1)
