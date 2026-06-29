"""Fetch live/EOD stock prices from Yahoo Finance (NSE/BSE supported)."""
from __future__ import annotations

import yfinance as yf


def get_stock_prices(symbols: list[str]) -> dict[str, float | None]:
    """Return {symbol: last_price}. Price is None if it could not be fetched."""
    prices: dict[str, float | None] = {}
    if not symbols:
        return prices
    for symbol in symbols:
        prices[symbol] = _last_price(symbol)
    return prices


def _last_price(symbol: str) -> float | None:
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            return None
        return round(float(hist["Close"].dropna().iloc[-1]), 2)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! price fetch failed for {symbol}: {exc}")
        return None


if __name__ == "__main__":
    print(get_stock_prices(["RELIANCE.NS", "INFY.NS"]))
