"""One-month dummy-trading readiness report.

The output is intentionally conservative: it can mark the system eligible for
human review, but it never says "ready for real money" automatically.
"""
from __future__ import annotations

import sys
from datetime import datetime

import datastore as ds
from market_calendar import is_market_open, now_ist
from strategy_config import ACTIVE_PROFILE, MAX_DRAWDOWN_PCT, strategy_metadata

LATEST_FILE = "paper/readiness.json"
MIN_TRADING_DAYS = 20
MAX_DAYS_SINCE_DAILY = 5
MAX_WATCHLIST_AGE_DAYS = 10
MAX_INTRADAY_AGE_MINUTES = 75
MAX_AVG_TRADES_PER_DAY = 4.0
# Walk-forward gate: the backtest's mean out-of-sample alpha must be positive
# AND at least this many validation folds must be individually positive.
WALK_FORWARD_MIN_POSITIVE_FOLDS = 2


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _criterion(key: str, label: str, passed: bool, detail: str, blocking: bool = True) -> dict:
    return {
        "key": key,
        "label": label,
        "passed": bool(passed),
        "blocking": bool(blocking),
        "detail": detail,
    }


def _max_drawdown_pct(history: list[dict]) -> float:
    peak = None
    worst = 0.0
    for row in history:
        value = row.get("value")
        if not value:
            continue
        peak = value if peak is None else max(peak, value)
        if peak:
            worst = min(worst, (value / peak - 1) * 100)
    return round(worst, 3)


def _best_lab_variant(backtest: dict) -> dict | None:
    variants = ((backtest or {}).get("strategy_lab") or {}).get("variants") or []
    if not variants:
        return None
    return max(variants, key=lambda v: v.get("alpha_pct") if v.get("alpha_pct") is not None else -999)


def _active_lab_variant(backtest: dict) -> dict | None:
    variants = ((backtest or {}).get("strategy_lab") or {}).get("variants") or []
    profile_to_variant = {
        "momentum_weekly_churn_control": "momentum_only_weekly",
        "no_regime_filter": "no_regime_filter",
        "aggressive_regime_guarded": "current_daily",
    }
    active_id = profile_to_variant.get(ACTIVE_PROFILE, ACTIVE_PROFILE)
    return next((v for v in variants if v.get("id") == active_id), None)


def _num(value, default: float = 0.0) -> float:
    return default if value is None else float(value)


def _recompute_totals(paper: dict, history: list[dict]) -> dict:
    """Independently recompute total P&L and alpha from the history value series.

    Does not trust the stored totals: derives them from start_capital and the
    last marked value, and uses the last recorded benchmark leg for alpha. Any
    mismatch above 0.1 percentage points versus the stored numbers is flagged
    (a mismatch means the book and its headline claims have diverged).
    """
    start = float(paper.get("start_capital") or 500000.0)
    stored_total = _num(paper.get("total_pnl_pct"))
    stored_alpha = _num(paper.get("alpha_pct"))
    values = [row.get("value") for row in history if row.get("value")]
    if not values or start <= 0:
        return {"available": False, "total_pnl_pct": stored_total,
                "alpha_pct": stored_alpha, "max_mismatch_pct": None,
                "consistent": False}
    total = round((values[-1] / start - 1) * 100, 3)
    bench = None
    for row in reversed(history):
        if row.get("benchmark_pct") is not None:
            bench = float(row["benchmark_pct"])
            break
    if bench is None:
        bench = _num(paper.get("benchmark_pct"))
    alpha = round(total - bench, 3)
    mismatch = round(max(abs(total - stored_total), abs(alpha - stored_alpha)), 3)
    return {"available": True, "total_pnl_pct": total, "alpha_pct": alpha,
            "max_mismatch_pct": mismatch, "consistent": mismatch <= 0.1}


def _oos_validation_alpha(backtest: dict, active_variant: dict | None) -> float | None:
    """Out-of-sample (validation-window) alpha for the active profile's backtest.

    Prefers the headline validation split (the headline now replays
    ACTIVE_PROFILE), falling back to the active lab variant's split.
    """
    headline = (backtest or {}).get("validation") or {}
    if headline.get("validation_alpha_pct") is not None:
        return float(headline["validation_alpha_pct"])
    variant_val = (active_variant or {}).get("validation") or {}
    if variant_val.get("validation_alpha_pct") is not None:
        return float(variant_val["validation_alpha_pct"])
    return None


def _walk_forward_summary(backtest: dict, active_variant: dict | None) -> dict | None:
    """Walk-forward fold summary from the backtest payload, if present.

    Prefers the headline validation (which replays ACTIVE_PROFILE), falling
    back to the active lab variant. Returns None on old payloads that predate
    walk-forward validation so the caller can fall back to the single split.
    """
    for source in ((backtest or {}).get("validation") or {},
                   (active_variant or {}).get("validation") or {}):
        wf = source.get("walk_forward") or {}
        if wf.get("mean_validation_alpha_pct") is not None and \
                wf.get("folds_positive") is not None:
            return wf
    return None


def _drift_status() -> tuple[bool, str]:
    """Advisory backtest-vs-live drift check from drift/latest.json.

    Missing file passes with a note (fresh start — the weekly monitor has not
    run yet). Verdicts 'ok' and 'insufficient_data' pass; 'drift' (or anything
    unknown) fails the advisory criterion.
    """
    drift = ds.read_json("drift/latest.json", default=None)
    if not drift:
        return True, "no drift report yet (fresh start) — advisory pass"
    verdict = drift.get("verdict")
    div = drift.get("final_divergence_pct")
    corr = drift.get("daily_return_correlation")
    detail = (
        f"drift verdict '{verdict}'"
        f" (final divergence {div if div is not None else 'n/a'}%,"
        f" daily-return correlation {corr if corr is not None else 'n/a'})"
    )
    return verdict in ("ok", "insufficient_data"), detail


def _history_by_date(history: list[dict]) -> list[dict]:
    by_date = {
        row.get("date"): row for row in history
        if row.get("date")
    }
    return [by_date[d] for d in sorted(by_date)]


def run() -> dict:
    ist = now_ist()
    paper = ds.read_json("paper/latest.json", default={}) or {}
    live = ds.read_json("paper/live.json", default={}) or {}
    daily = ds.read_json("daily/latest.json", default={}) or {}
    watch = ds.read_json("watchlist/latest.json", default={}) or {}
    backtest = ds.read_json("backtest/latest.json", default={}) or {}

    history = _history_by_date(paper.get("history") or [])
    paper_days = len(history)
    max_dd = _max_drawdown_pct(history)
    # Initial TOP_N portfolio construction is not churn. Measure only activity
    # after inception so day 1 does not fail the readiness warning by design.
    inception = paper.get("inception") or (history[0].get("date") if history else None)
    post_inception = [
        row for row in history
        if row.get("date") and (inception is None or row["date"] > inception)
    ]
    trade_days = sum(1 for row in post_inception if row.get("n_trades", 0) > 0)
    avg_trades_per_day = (
        sum(row.get("n_trades", 0) for row in post_inception) / len(post_inception)
        if post_inception else 0.0
    )

    daily_dt = _parse_date(daily.get("trading_date"))
    daily_age = (ist.date() - daily_dt.date()).days if daily_dt else None

    watch_dt = _parse_dt(watch.get("generated_ist") or watch.get("generated_at") or watch.get("ist"))
    watch_age = (ist - watch_dt.astimezone(ist.tzinfo)).days if watch_dt else None

    live_dt = _parse_dt(live.get("ist"))
    live_age_minutes = None
    if live_dt:
        live_age_minutes = (ist - live_dt.astimezone(ist.tzinfo)).total_seconds() / 60

    best_variant = _best_lab_variant(backtest)
    active_variant = _active_lab_variant(backtest)
    active_is_supported = bool(active_variant) and (
        active_variant.get("alpha_pct", -999) > 0 and
        active_variant.get("total_return_pct", -999) > 0
    )
    recomputed = _recompute_totals(paper, history)
    oos_alpha = _oos_validation_alpha(backtest, active_variant)
    walk_forward = _walk_forward_summary(backtest, active_variant)
    drift_ok, drift_detail = _drift_status()
    paper_exec = paper.get("execution_model")
    backtest_exec = backtest.get("execution_model")

    if walk_forward is not None:
        wf_mean = float(walk_forward["mean_validation_alpha_pct"])
        wf_pos = int(walk_forward["folds_positive"])
        wf_n = int(walk_forward.get("n_folds") or 0)
        oos_passed = wf_mean > 0 and wf_pos >= WALK_FORWARD_MIN_POSITIVE_FOLDS
        oos_detail = (
            f"walk-forward mean validation alpha {wf_mean:.2f}%, "
            f"{wf_pos}/{wf_n} folds positive "
            f"(need mean > 0 and >= {WALK_FORWARD_MIN_POSITIVE_FOLDS} positive folds)"
        )
    else:
        # Old payloads without walk-forward folds: single-split check.
        oos_passed = oos_alpha is not None and oos_alpha > 0
        oos_detail = (
            f"validation-window alpha {oos_alpha:.2f}%"
            if oos_alpha is not None
            else "no train/validation split in backtest output"
        )

    criteria = [
        _criterion(
            "min_trading_days",
            "Run at least one trading month in dummy mode",
            paper_days >= MIN_TRADING_DAYS,
            f"{paper_days}/{MIN_TRADING_DAYS} paper-trading days recorded",
        ),
        _criterion(
            "positive_dummy_pnl",
            "Dummy portfolio is not losing money",
            recomputed["total_pnl_pct"] >= 0,
            f"total P&L {recomputed['total_pnl_pct']:.2f}% (recomputed from history)",
        ),
        _criterion(
            "positive_dummy_alpha",
            "Dummy portfolio beats NIFTY 50",
            recomputed["alpha_pct"] >= 0,
            f"alpha {recomputed['alpha_pct']:.2f}% (recomputed from history)",
        ),
        _criterion(
            "totals_recomputed_consistent",
            "Stored totals match independent recomputation",
            recomputed["consistent"],
            (
                f"recomputed total {recomputed['total_pnl_pct']:.2f}% / alpha "
                f"{recomputed['alpha_pct']:.2f}% vs stored "
                f"{_num(paper.get('total_pnl_pct')):.2f}% / "
                f"{_num(paper.get('alpha_pct')):.2f}% "
                f"(max mismatch {recomputed['max_mismatch_pct']} pct-pts, limit 0.1)"
                if recomputed["available"] else "no history values to recompute from"
            ),
        ),
        _criterion(
            "drawdown_inside_limit",
            "Drawdown stays inside risk limit",
            max_dd >= -(MAX_DRAWDOWN_PCT * 100),
            f"max drawdown {max_dd:.2f}% vs limit {-MAX_DRAWDOWN_PCT * 100:.1f}%",
        ),
        _criterion(
            "daily_data_fresh",
            "Daily analysis is fresh",
            daily_age is not None and daily_age <= MAX_DAYS_SINCE_DAILY,
            "age unavailable" if daily_age is None else f"{daily_age} calendar days old",
        ),
        _criterion(
            "watchlist_fresh",
            "Watchlist is fresh",
            watch_age is not None and watch_age <= MAX_WATCHLIST_AGE_DAYS,
            "age unavailable" if watch_age is None else f"{watch_age} calendar days old",
        ),
        _criterion(
            "intraday_fresh_when_open",
            "Intraday feed is fresh during market hours",
            (not is_market_open(ist)) or (
                live_age_minutes is not None and live_age_minutes <= MAX_INTRADAY_AGE_MINUTES
            ),
            "market closed" if not is_market_open(ist) else (
                "age unavailable" if live_age_minutes is None else f"{live_age_minutes:.0f} minutes old"
            ),
        ),
        _criterion(
            "backtest_supports_profile",
            "Backtest lab supports the active profile",
            active_is_supported,
            (
                f"active variant {active_variant.get('id')} alpha {active_variant.get('alpha_pct'):.2f}%"
                if active_variant and active_variant.get("alpha_pct") is not None
                else f"active profile {ACTIVE_PROFILE} not found in strategy lab"
            ),
        ),
        _criterion(
            "backtest_oos_positive",
            "Active profile backtest is positive out-of-sample",
            oos_passed,
            oos_detail,
        ),
        _criterion(
            "execution_model_next_open",
            "Backtest and paper trader both fill at the next open",
            paper_exec == "next_open" and backtest_exec == "next_open",
            f"paper={paper_exec or 'unknown'}, backtest={backtest_exec or 'unknown'}",
        ),
        _criterion(
            "churn_controlled",
            "Trading frequency is controlled",
            avg_trades_per_day <= MAX_AVG_TRADES_PER_DAY,
            f"{avg_trades_per_day:.2f} average trades/day after inception; {trade_days} trade days",
            blocking=False,
        ),
        _criterion(
            "drift_ok",
            "Backtest replay tracks the live paper book (advisory)",
            drift_ok,
            drift_detail,
            blocking=False,
        ),
    ]

    blocking = [c for c in criteria if c["blocking"]]
    passed_blocking = sum(1 for c in blocking if c["passed"])
    all_blocking_passed = passed_blocking == len(blocking)
    if paper_days < MIN_TRADING_DAYS:
        decision = "incubating"
    elif all_blocking_passed:
        decision = "eligible_for_review"
    else:
        decision = "not_ready"

    report = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "decision": decision,
        "summary": {
            "paper_days": paper_days,
            "min_trading_days": MIN_TRADING_DAYS,
            "passed_blocking": passed_blocking,
            "blocking_criteria": len(blocking),
            "total_pnl_pct": paper.get("total_pnl_pct"),
            "alpha_pct": paper.get("alpha_pct"),
            "recomputed_totals": recomputed,
            "oos_validation_alpha_pct": oos_alpha,
            "walk_forward": walk_forward,
            "execution_model_paper": paper_exec,
            "execution_model_backtest": backtest_exec,
            "max_drawdown_pct": max_dd,
            "trade_days": trade_days,
            "active_profile": ACTIVE_PROFILE,
            "active_lab_variant": active_variant,
            "best_lab_variant": best_variant,
        },
        "criteria": criteria,
        "strategy": strategy_metadata(),
        "note": (
            "Dummy incubation report. This can only make the system eligible for "
            "manual review; it is not permission to trade real money."
        ),
    }
    ds.write_json(LATEST_FILE, report)
    print(
        f"Incubation {decision}: {passed_blocking}/{len(blocking)} blocking "
        f"criteria passed, {paper_days}/{MIN_TRADING_DAYS} days."
    )
    return report


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
