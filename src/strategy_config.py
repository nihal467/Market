"""Shared strategy/risk constants and metadata.

Keep paper trading, backtesting, and dashboard payloads tied to one strategy
version so performance can be interpreted after future changes.
"""
from __future__ import annotations

STRATEGY_VERSION = "2026-07-09.aggressive-dummy-regime-guard-v1"
ACTIVE_PROFILE = "aggressive_regime_guarded"

START_CAPITAL = 500000.0
TOP_N = 10
HOLD_UNTIL_RANK = 10
REBALANCE_INTERVAL = "daily"
USE_MARKET_REGIME_GUARD = True
COST_PER_SIDE = 0.0010

STOP_LOSS_PCT = 0.08
TRAILING_STOP_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.04
MAX_DRAWDOWN_PCT = 0.12
MAX_POSITION_PCT = 0.15
MAX_SECTOR_PCT = 0.40

# Fast intraday dummy-mode guards. These are intentionally tighter than the
# end-of-day stop rules because they react to sudden same-session selloffs.
INTRADAY_SHOCK_BOOK_LOSS_PCT = 0.01
INTRADAY_SHOCK_INDEX_DROP_PCT = 0.0075
INTRADAY_SHOCK_WATCHLIST_DOWN_FRACTION = 0.55
INTRADAY_SHOCK_WATCHLIST_MEDIAN_DROP_PCT = 0.006
INTRADAY_WEAK_HOLDING_DAY_DROP_PCT = 0.015
INTRADAY_WEAK_HOLDING_LOSS_PCT = 0.02

MIN_AVG_DAILY_VALUE = 50_000_000.0  # Rs 5 crore average traded value proxy
MIN_PRICE = 20.0


def strategy_metadata() -> dict:
    return {
        "version": STRATEGY_VERSION,
        "active_profile": ACTIVE_PROFILE,
        "top_n": TOP_N,
        "hold_until_rank": HOLD_UNTIL_RANK,
        "rebalance_interval": REBALANCE_INTERVAL,
        "use_market_regime_guard": USE_MARKET_REGIME_GUARD,
        "cost_per_side_pct": round(COST_PER_SIDE * 100, 3),
        "stop_loss_pct": round(STOP_LOSS_PCT * 100, 1),
        "trailing_stop_pct": round(TRAILING_STOP_PCT * 100, 1),
        "max_daily_loss_pct": round(MAX_DAILY_LOSS_PCT * 100, 1),
        "max_drawdown_pct": round(MAX_DRAWDOWN_PCT * 100, 1),
        "max_position_pct": round(MAX_POSITION_PCT * 100, 1),
        "max_sector_pct": round(MAX_SECTOR_PCT * 100, 1),
        "intraday_shock_book_loss_pct": round(INTRADAY_SHOCK_BOOK_LOSS_PCT * 100, 2),
        "intraday_shock_index_drop_pct": round(INTRADAY_SHOCK_INDEX_DROP_PCT * 100, 2),
        "intraday_weak_holding_day_drop_pct": round(INTRADAY_WEAK_HOLDING_DAY_DROP_PCT * 100, 2),
        "intraday_weak_holding_loss_pct": round(INTRADAY_WEAK_HOLDING_LOSS_PCT * 100, 2),
        "min_avg_daily_value_rs": MIN_AVG_DAILY_VALUE,
        "min_price_rs": MIN_PRICE,
    }
