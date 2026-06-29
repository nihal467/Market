"""Strategy engine: blend technical indicators + news sentiment into a signal.

Produces BUY / HOLD / SELL plus a numeric ``score`` (higher = more bullish) and
a short list of human-readable reasons. This is a transparent, *weighted*
rule-based scorer — each input contributes a graded amount rather than a flat
±1, so a strong uptrend with strongly positive news ranks above a borderline
one. The paper-trading bot uses ``score`` to rank which BUYs to actually hold.

Weights (tunable in one place):
  - Trend (price vs SMA50)        : W_TREND
  - Golden/Death cross (50 vs 200): W_CROSS
  - RSI regime (graded)           : up to W_RSI
  - 52-week position              : W_52W
  - News sentiment (graded x conf): up to W_NEWS

Thresholds BUY_AT / SELL_AT convert the composite into a discrete signal.
NOT investment advice.
"""
from __future__ import annotations

# --- Tunable weights -------------------------------------------------------
W_TREND = 1.0   # price above/below SMA50
W_CROSS = 1.0   # golden vs death cross
W_RSI = 1.5     # max magnitude from RSI regime (graded)
W_52W = 0.5     # near 52w low (value) vs near 52w high (stretched)
W_NEWS = 2.0    # max magnitude from news (graded by sentiment & confidence)
W_MOM = 2.0     # max magnitude from medium-term (3M) momentum

BUY_AT = 2.0
SELL_AT = -2.0

# News confidence saturates at this many headlines (more headlines => we trust
# the average sentiment more, up to a cap).
NEWS_CONF_FULL = 6

# 3-month return at/above this (%) earns the full momentum weight.
MOM_FULL = 20.0


def _momentum_component(ret_3m: float | None) -> tuple[float, str | None]:
    """Graded 3-month momentum contribution in [-W_MOM, +W_MOM].

    Momentum is a well-documented equity factor: recent winners tend to keep
    winning over the medium term. Scales linearly and saturates at +/-MOM_FULL.
    """
    if ret_3m is None:
        return 0.0, None
    frac = max(-1.0, min(1.0, ret_3m / MOM_FULL))
    contrib = round(W_MOM * frac, 3)
    if contrib >= 0.25:
        return contrib, f"Strong 3M momentum ({ret_3m}%)"
    if contrib <= -0.25:
        return contrib, f"Weak 3M momentum ({ret_3m}%)"
    return contrib, f"Flat 3M momentum ({ret_3m}%)"


def _rsi_component(rsi: float) -> tuple[float, str]:
    """Graded RSI contribution in roughly [-W_RSI, +W_RSI].

    Oversold (<30) is bullish, overbought (>70) is bearish, with the magnitude
    scaling by how deep into the extreme we are. 30-70 contributes ~0.
    """
    if rsi <= 30:
        frac = min(1.0, (30 - rsi) / 30.0)   # rsi 30 -> 0, rsi 0 -> 1
        return W_RSI * frac, f"RSI {rsi} oversold — bullish"
    if rsi >= 70:
        frac = min(1.0, (rsi - 70) / 30.0)   # rsi 70 -> 0, rsi 100 -> 1
        return -W_RSI * frac, f"RSI {rsi} overbought — bearish"
    return 0.0, f"RSI {rsi} neutral"


def _news_component(sentiment: dict | None) -> tuple[float, str | None]:
    """Graded news contribution in [-W_NEWS, +W_NEWS].

    Scales by both the sentiment magnitude (how positive/negative) and a
    confidence factor (how many headlines backed it). A single mildly positive
    headline barely moves the needle; many strongly positive ones move it a lot.
    """
    if not sentiment or not sentiment.get("count"):
        return 0.0, None
    s = float(sentiment.get("score") or 0.0)   # in [-1, 1]
    count = int(sentiment.get("count") or 0)
    conf = min(1.0, count / NEWS_CONF_FULL)
    contrib = round(W_NEWS * s * conf, 3)
    if contrib >= 0.25:
        return contrib, f"Positive news (score {s}, {count} headlines)"
    if contrib <= -0.25:
        return contrib, f"Negative news (score {s}, {count} headlines)"
    return contrib, f"Neutral/weak news (score {s}, {count} headlines)"


def decide(indicators: dict | None, sentiment: dict | None) -> dict:
    """Return {'signal', 'score', 'reasons': [...], 'components': {...}}."""
    reasons: list[str] = []
    if not indicators:
        return {"signal": "HOLD", "score": 0, "reasons": ["No price history available"],
                "components": {}}

    price = indicators["price"]
    rsi = indicators.get("rsi14")
    sma50 = indicators.get("sma50")
    sma200 = indicators.get("sma200")
    components: dict[str, float] = {}
    score = 0.0

    # --- Trend: price vs SMA50 ---
    if sma50 is not None:
        c = W_TREND if price > sma50 else -W_TREND
        components["trend"] = c
        score += c
        reasons.append(f"Price {'above' if c > 0 else 'below'} SMA50 ({sma50})")

    # --- Golden/Death cross: SMA50 vs SMA200 ---
    if sma50 is not None and sma200 is not None:
        c = W_CROSS if sma50 > sma200 else -W_CROSS
        components["cross"] = c
        score += c
        reasons.append("Golden cross (SMA50>SMA200)" if c > 0
                       else "Death cross (SMA50<SMA200)")

    # --- RSI regime (graded) ---
    if rsi is not None:
        c, why = _rsi_component(rsi)
        components["rsi"] = round(c, 3)
        score += c
        reasons.append(why)

    # --- 52-week position (light tilt) ---
    pct_high = indicators.get("pct_from_high")
    pct_low = indicators.get("pct_from_low")
    if pct_high is not None and pct_high >= -2:
        components["pos52w"] = -W_52W
        score -= W_52W
        reasons.append("Near 52-week high")
    elif pct_low is not None and pct_low <= 5:
        components["pos52w"] = W_52W
        score += W_52W
        reasons.append("Near 52-week low — value zone")

    # --- Medium-term momentum (3M) ---
    c, why = _momentum_component(indicators.get("ret_3m"))
    if why is not None:
        components["momentum"] = c
        score += c
        reasons.append(why)

    # --- News sentiment (graded by magnitude x confidence) ---
    c, why = _news_component(sentiment)
    if why is not None:
        components["news"] = c
        score += c
        reasons.append(why)

    score = round(score, 3)
    if score >= BUY_AT:
        signal = "BUY"
    elif score <= SELL_AT:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {"signal": signal, "score": score, "reasons": reasons,
            "components": components}
