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


def _num(value, default: float = 0.0) -> float:
    return default if value is None else float(value)


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
    active_is_supported = False
    if best_variant:
        active_is_supported = (
            best_variant.get("alpha_pct", -999) > 0 and
            best_variant.get("total_return_pct", -999) > 0
        )
    variants = ((backtest or {}).get("strategy_lab") or {}).get("variants") or []
    active_family = [
        v for v in variants
        if v.get("id") in {"momentum_only_weekly", "weekly_churn_control"}
    ]
    active_family_ok = any((v.get("alpha_pct") or -999) > 0 for v in active_family)

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
            _num(paper.get("total_pnl_pct")) >= 0,
            f"total P&L {_num(paper.get('total_pnl_pct')):.2f}%",
        ),
        _criterion(
            "positive_dummy_alpha",
            "Dummy portfolio beats NIFTY 50",
            _num(paper.get("alpha_pct")) >= 0,
            f"alpha {_num(paper.get('alpha_pct')):.2f}%",
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
            "Backtest lab supports the active profile family",
            active_is_supported and active_family_ok,
            (
                f"best variant {best_variant.get('id')} alpha {best_variant.get('alpha_pct'):.2f}%"
                if best_variant and best_variant.get("alpha_pct") is not None
                else "no strategy lab result available"
            ),
        ),
        _criterion(
            "churn_controlled",
            "Trading frequency is controlled",
            avg_trades_per_day <= MAX_AVG_TRADES_PER_DAY,
            f"{avg_trades_per_day:.2f} average trades/day after inception; {trade_days} trade days",
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
            "max_drawdown_pct": max_dd,
            "trade_days": trade_days,
            "active_profile": ACTIVE_PROFILE,
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
