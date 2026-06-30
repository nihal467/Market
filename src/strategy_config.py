"""Shared strategy/risk constants and metadata.

Keep paper trading, backtesting, and dashboard payloads tied to one strategy
version so performance can be interpreted after future changes.
"""
from __future__ import annotations

STRATEGY_VERSION = "2026-06-30.regime-liquidity-risk-v1"

START_CAPITAL = 500000.0
TOP_N = 10
COST_PER_SIDE = 0.0010

STOP_LOSS_PCT = 0.08
TRAILING_STOP_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.04
MAX_DRAWDOWN_PCT = 0.12
MAX_POSITION_PCT = 0.15
MAX_SECTOR_PCT = 0.40

MIN_AVG_DAILY_VALUE = 50_000_000.0  # Rs 5 crore average traded value proxy
MIN_PRICE = 20.0


def strategy_metadata() -> dict:
    return {
        "version": STRATEGY_VERSION,
        "top_n": TOP_N,
        "cost_per_side_pct": round(COST_PER_SIDE * 100, 3),
        "stop_loss_pct": round(STOP_LOSS_PCT * 100, 1),
        "trailing_stop_pct": round(TRAILING_STOP_PCT * 100, 1),
        "max_daily_loss_pct": round(MAX_DAILY_LOSS_PCT * 100, 1),
        "max_drawdown_pct": round(MAX_DRAWDOWN_PCT * 100, 1),
        "max_position_pct": round(MAX_POSITION_PCT * 100, 1),
        "max_sector_pct": round(MAX_SECTOR_PCT * 100, 1),
        "min_avg_daily_value_rs": MIN_AVG_DAILY_VALUE,
        "min_price_rs": MIN_PRICE,
    }
