"""Strategy engine: combine technical indicators + news sentiment into a signal.

Produces BUY / HOLD / SELL with a short list of human-readable reasons.
This is a transparent rule-based scorer — not a guarantee of returns.
"""
from __future__ import annotations


def decide(indicators: dict | None, sentiment: dict | None) -> dict:
    """Return {'signal', 'score', 'reasons': [...]} from inputs."""
    reasons: list[str] = []
    score = 0  # positive = bullish, negative = bearish

    if not indicators:
        return {"signal": "HOLD", "score": 0, "reasons": ["No price history available"]}

    price = indicators["price"]
    rsi = indicators.get("rsi14")
    sma50 = indicators.get("sma50")
    sma200 = indicators.get("sma200")

    # --- RSI ---
    if rsi is not None:
        if rsi < 30:
            score += 2
            reasons.append(f"RSI {rsi} oversold (<30) — potential buy")
        elif rsi > 70:
            score -= 2
            reasons.append(f"RSI {rsi} overbought (>70) — potential sell")
        else:
            reasons.append(f"RSI {rsi} neutral")

    # --- Trend: price vs SMA50 ---
    if sma50 is not None:
        if price > sma50:
            score += 1
            reasons.append(f"Price above SMA50 ({sma50}) — uptrend")
        else:
            score -= 1
            reasons.append(f"Price below SMA50 ({sma50}) — downtrend")

    # --- Golden/Death cross: SMA50 vs SMA200 ---
    if sma50 is not None and sma200 is not None:
        if sma50 > sma200:
            score += 1
            reasons.append("SMA50 > SMA200 (golden cross) — bullish")
        else:
            score -= 1
            reasons.append("SMA50 < SMA200 (death cross) — bearish")

    # --- 52-week position ---
    pct_high = indicators.get("pct_from_high")
    pct_low = indicators.get("pct_from_low")
    if pct_high is not None and pct_high >= -2:
        score -= 1
        reasons.append("Near 52-week high — limited upside / book profit")
    elif pct_low is not None and pct_low <= 5:
        score += 1
        reasons.append("Near 52-week low — value zone")

    # --- News sentiment ---
    if sentiment and sentiment.get("count"):
        s = sentiment["score"]
        if s >= 0.25:
            score += 1
            reasons.append(f"Positive news sentiment ({s})")
        elif s <= -0.25:
            score -= 1
            reasons.append(f"Negative news sentiment ({s})")
        else:
            reasons.append(f"Neutral news sentiment ({s})")

    if score >= 2:
        signal = "BUY"
    elif score <= -2:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {"signal": signal, "score": score, "reasons": reasons}
