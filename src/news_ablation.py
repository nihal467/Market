"""Pure helpers for the news-ablation study: does news actually add alpha?

The backtest replays a matched pair of the ACTIVE profile — identical knobs,
one run scoring WITH the archived news sentiment (news/ on the data branch)
and one with the news contribution zeroed — and this module turns the two
result rows into the ``news_ablation`` payload object plus a verdict.

Kept free of pandas/yfinance imports on purpose so the verdict logic is unit
testable without the market data stack (mirrors drift_monitor's split between
pure comparison logic and heavy replay code).
"""
from __future__ import annotations

# The pair's out-of-sample (validation) alpha difference must clear this many
# percentage points before news is called helping/hurting; inside the band it
# is "news_neutral". Deliberately wide: a single replayed history is noisy.
NEWS_ABLATION_NEUTRAL_BAND_PP = 0.5


def ablation_verdict(delta_validation_alpha_pct: float | None,
                     has_walk_forward: bool,
                     news_days_used: int | None = None) -> str:
    """Classify the with-news vs without-news validation-alpha delta.

    Returns "news_helping" / "news_neutral" / "news_hurting", or
    "insufficient_data" when the delta cannot be trusted:
      - walk-forward validation is absent (too little replay history), or
      - the delta itself is None (no benchmark => no alpha), or
      - ``news_days_used`` is 0 (the news archive is still empty, so the two
        runs were identical by construction — a delta of 0.0 would say
        nothing about news). Pass None when the count is unknown.
    """
    if not has_walk_forward or delta_validation_alpha_pct is None:
        return "insufficient_data"
    if news_days_used is not None and news_days_used <= 0:
        return "insufficient_data"
    if delta_validation_alpha_pct > NEWS_ABLATION_NEUTRAL_BAND_PP:
        return "news_helping"
    if delta_validation_alpha_pct < -NEWS_ABLATION_NEUTRAL_BAND_PP:
        return "news_hurting"
    return "news_neutral"


def _walk_forward_summary(validation: dict | None) -> dict | None:
    wf = (validation or {}).get("walk_forward")
    if not wf:
        return None
    return {
        "n_folds": wf.get("n_folds"),
        "mean_validation_alpha_pct": wf.get("mean_validation_alpha_pct"),
        "folds_positive": wf.get("folds_positive"),
    }


def _side_summary(row: dict | None) -> dict:
    row = row or {}
    validation = row.get("validation") or {}
    return {
        "id": row.get("id"),
        "validation_alpha_pct": validation.get("validation_alpha_pct"),
        "total_return_pct": row.get("total_return_pct"),
        "alpha_pct": row.get("alpha_pct"),
        "walk_forward": _walk_forward_summary(validation),
    }


def build_news_ablation(with_row: dict | None, without_row: dict | None) -> dict:
    """Shape the top-level ``news_ablation`` payload from the two variant rows.

    ``with_row`` / ``without_row`` are the strategy-lab result dicts of the
    matched active-profile pair (use_news True / False). Tolerates missing or
    skipped rows — everything degrades to None + "insufficient_data".
    """
    with_side = _side_summary(with_row)
    without_side = _side_summary(without_row)
    wa = with_side["validation_alpha_pct"]
    wo = without_side["validation_alpha_pct"]
    delta = round(wa - wo, 2) if wa is not None and wo is not None else None
    has_wf = bool(with_side["walk_forward"]) and bool(without_side["walk_forward"])
    news_days_used = (with_row or {}).get("news_days_used")
    verdict = ablation_verdict(delta, has_wf, news_days_used)
    return {
        "with_news": with_side,
        "without_news": without_side,
        "delta_validation_alpha_pct": delta,
        "neutral_band_pp": NEWS_ABLATION_NEUTRAL_BAND_PP,
        "news_days_used": news_days_used,
        "verdict": verdict,
        "note": (
            "Matched pair of the active profile replayed on the same history: "
            "identical config except the news term (graded lexicon sentiment "
            "from the dated news archive) is enabled vs zeroed. Delta is "
            "out-of-sample validation alpha, with-news minus without-news; "
            f"verdict needs |delta| > {NEWS_ABLATION_NEUTRAL_BAND_PP} pct-pts. "
            "insufficient_data while walk-forward validation or archived news "
            "days are missing. The archive only covers dates after it was "
            "switched on, so early deltas are naturally tiny."
        ),
    }
