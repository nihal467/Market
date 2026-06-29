"""Technical indicators (SMA, RSI, 52-week range) from Yahoo Finance history."""
from __future__ import annotations

import pandas as pd
import yfinance as yf


def compute_indicators(symbol: str) -> dict | None:
    """Return indicator dict for a symbol, or None if history is unavailable."""
    try:
        hist = yf.Ticker(symbol).history(period="1y")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! history fetch failed for {symbol}: {exc}")
        return None

    close = hist["Close"].dropna() if not hist.empty else pd.Series(dtype=float)
    if len(close) < 20:
        return None

    last = float(close.iloc[-1])
    sma50 = _sma(close, 50)
    sma200 = _sma(close, 200)
    rsi14 = _rsi(close, 14)
    week52_high = round(float(close.max()), 2)
    week52_low = round(float(close.min()), 2)

    return {
        "price": round(last, 2),
        "sma50": sma50,
        "sma200": sma200,
        "rsi14": rsi14,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "pct_from_high": round((last - week52_high) / week52_high * 100, 2),
        "pct_from_low": round((last - week52_low) / week52_low * 100, 2),
    }


def _sma(close: pd.Series, window: int) -> float | None:
    if len(close) < window:
        return None
    return round(float(close.rolling(window).mean().iloc[-1]), 2)


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_loss = float(loss.iloc[-1])
    last_gain = float(gain.iloc[-1])
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return round(100 - (100 / (1 + rs)), 2)


if __name__ == "__main__":
    print(compute_indicators("NIFTYBEES.NS"))
