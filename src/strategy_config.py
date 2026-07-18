"""Shared strategy/risk constants and metadata.

Keep paper trading, backtesting, and dashboard payloads tied to one strategy
version so performance can be interpreted after future changes.
"""
from __future__ import annotations

STRATEGY_VERSION = "2026-07-13.momentum-weekly-churn-control-v1"
ACTIVE_PROFILE = "momentum_weekly_churn_control"

START_CAPITAL = 500000.0
TOP_N = 10
HOLD_UNTIL_RANK = 20
REBALANCE_INTERVAL = "weekly"
USE_MARKET_REGIME_GUARD = True

# --- Execution realism (applied to every simulated fill, both sides) --------
# All-in transaction cost per side in basis points for NSE delivery trades on
# a discount broker: STT + exchange txn charges + stamp duty + SEBI fee + GST.
# 12 bps per side is a fair approximation (brokerage itself is zero).
COST_BPS_PER_SIDE = 12
# Market-order slippage vs the reference price in basis points: buys fill a
# touch above the open, sells a touch below. 5 bps suits liquid Top-50 names.
SLIPPAGE_BPS = 5
# Fractional per-side cost derived from the bps knob; kept for call sites that
# work with fractions (notional * COST_PER_SIDE == notional * bps / 10000).
COST_PER_SIDE = COST_BPS_PER_SIDE / 10000.0
# All simulated fills happen at the NEXT session's open (signals from day T's
# close fill at day T+1's open) — no same-bar look-ahead.
EXECUTION_MODEL = "next_open"

STOP_LOSS_PCT = 0.08
TRAILING_STOP_PCT = 0.10
MAX_DAILY_LOSS_PCT = 0.04
MAX_DRAWDOWN_PCT = 0.12
MAX_POSITION_PCT = 0.15
MAX_SECTOR_PCT = 0.40
# Circuit-breaker: while the book is down this many percent from its all-time
# peak, queue NO new buys (protective sells/stops still run). Resets naturally
# once the book value recovers inside the threshold.
MAX_DRAWDOWN_PAUSE_PCT = 15

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
        "cost_bps_per_side": COST_BPS_PER_SIDE,
        "slippage_bps": SLIPPAGE_BPS,
        "execution_model": EXECUTION_MODEL,
        "stop_loss_pct": round(STOP_LOSS_PCT * 100, 1),
        "trailing_stop_pct": round(TRAILING_STOP_PCT * 100, 1),
        "max_daily_loss_pct": round(MAX_DAILY_LOSS_PCT * 100, 1),
        "max_drawdown_pct": round(MAX_DRAWDOWN_PCT * 100, 1),
        "max_drawdown_pause_pct": MAX_DRAWDOWN_PAUSE_PCT,
        "max_position_pct": round(MAX_POSITION_PCT * 100, 1),
        "max_sector_pct": round(MAX_SECTOR_PCT * 100, 1),
        "intraday_shock_book_loss_pct": round(INTRADAY_SHOCK_BOOK_LOSS_PCT * 100, 2),
        "intraday_shock_index_drop_pct": round(INTRADAY_SHOCK_INDEX_DROP_PCT * 100, 2),
        "intraday_weak_holding_day_drop_pct": round(INTRADAY_WEAK_HOLDING_DAY_DROP_PCT * 100, 2),
        "intraday_weak_holding_loss_pct": round(INTRADAY_WEAK_HOLDING_LOSS_PCT * 100, 2),
        "min_avg_daily_value_rs": MIN_AVG_DAILY_VALUE,
        "min_price_rs": MIN_PRICE,
    }
