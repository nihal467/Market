"""Market regime filter for the paper strategy.

The bot should not force new risk when the broad market trend is weak. This is
intentionally simple and explainable: NIFTY must be above its SMA50, and SMA50
must be at/above SMA200. Existing holdings can still be managed by stops.
"""
from __future__ import annotations

import yfinance as yf

BENCHMARK = "^NSEI"


def current_regime() -> dict:
    try:
        hist = yf.download(BENCHMARK, period="1y", interval="1d",
                           auto_adjust=True, progress=False)
    except Exception as exc:  # noqa: BLE001
        return {"risk_on": True, "reason": f"regime fetch failed: {exc}", "fallback": True}

    if hist is None or hist.empty:
        return {"risk_on": True, "reason": "regime unavailable", "fallback": True}

    close = hist["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close = close.dropna()
    if len(close) < 200:
        return {"risk_on": True, "reason": "insufficient regime history", "fallback": True}

    price = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    ret_1m = float(close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
    risk_on = price >= sma50 and sma50 >= sma200
    return {
        "benchmark": "NIFTY 50",
        "symbol": BENCHMARK,
        "risk_on": risk_on,
        "price": round(price, 2),
        "sma50": round(sma50, 2),
        "sma200": round(sma200, 2),
        "ret_1m_pct": round(ret_1m, 2) if ret_1m is not None else None,
        "reason": "NIFTY above SMA50 and SMA50>=SMA200" if risk_on
                  else "NIFTY trend filter is risk-off",
    }
