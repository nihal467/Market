"""NSE trading-session helpers (IST), with no external dependencies.

GitHub Actions cron runs in UTC and is not precise, so we gate work in Python:
intraday jobs no-op outside market hours / on weekends, and we annotate every
snapshot with the IST timestamp and session phase.

The holiday list is intentionally static so CI does not depend on NSE's website
(which often blocks datacenter IPs). Refresh it yearly from the NSE/Nifty
Indices trading-holiday calendar.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Calendar years the static holiday table below actually covers. Any query
# outside these years raises loudly (see is_trading_day) instead of silently
# treating unknown holidays as trading days. Extend this set together with
# nse_holidays when refreshing the table each year.
HOLIDAY_YEARS_COVERED: set[int] = {2026}

# NSE equity trading holidays for calendar year 2026 (YYYY-MM-DD).
nse_holidays: set[str] = {
    "2026-01-15",  # Makar Sankranti / Pongal
    "2026-01-26",  # Republic Day
    "2026-02-15",  # Maha Shivaratri
    "2026-03-04",  # Holi
    "2026-03-21",  # Id-Ul-Fitr / Ramzan Id
    "2026-03-26",  # Ram Navami
    "2026-03-31",  # Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-27",  # Bakri Id
    "2026-08-15",  # Independence Day
    "2026-08-28",  # Ganesh Chaturthi
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Diwali Laxmi Pujan
    "2026-10-21",  # Diwali Balipratipada
    "2026-11-24",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(dt: datetime | None = None) -> bool:
    dt = dt or now_ist()
    if dt.year not in HOLIDAY_YEARS_COVERED:
        # Fail loudly rather than silently trading through unknown holidays.
        raise RuntimeError(f"NSE holiday calendar not updated for {dt.year}")
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
