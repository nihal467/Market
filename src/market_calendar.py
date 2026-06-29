"""NSE trading-session helpers (IST), with no external dependencies.

GitHub Actions cron runs in UTC and is not precise, so we gate work in Python:
intraday jobs no-op outside market hours / on weekends, and we annotate every
snapshot with the IST timestamp and session phase.

NOTE: this does not account for NSE trading holidays (no free reliable feed
without hitting NSE, which blocks CI IPs). Holiday runs simply produce a
snapshot with little price movement — harmless. A holiday list can be added to
``nse_holidays`` below if desired.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Optional manual holiday list (YYYY-MM-DD). Extend as needed each year.
nse_holidays: set[str] = set()


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    if dt.weekday() >= 5:  # Sat/Sun
        return False
    if dt.strftime("%Y-%m-%d") in nse_holidays:
        return False
    return True


def is_market_open(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    if not is_trading_day(dt):
        return False
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE


def session_phase(dt: datetime | None = None) -> str:
    """Return 'pre', 'open', 'post', or 'closed' for the given IST time."""
    dt = dt or now_ist()
    if not is_trading_day(dt):
        return "closed"
    t = dt.time()
    if t < MARKET_OPEN:
        return "pre"
    if t > MARKET_CLOSE:
        return "post"
    return "open"
