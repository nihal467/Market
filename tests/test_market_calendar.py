from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import market_calendar  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


class MarketCalendarTests(unittest.TestCase):
    def test_covered_year_classifies_days(self) -> None:
        # Thursday, not a holiday.
        self.assertTrue(market_calendar.is_trading_day(
            datetime(2026, 7, 2, 12, 0, tzinfo=IST)))
        # Republic Day holiday.
        self.assertFalse(market_calendar.is_trading_day(
            datetime(2026, 1, 26, 12, 0, tzinfo=IST)))
        # Saturday.
        self.assertFalse(market_calendar.is_trading_day(
            datetime(2026, 7, 4, 12, 0, tzinfo=IST)))

    def test_uncovered_future_year_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            market_calendar.is_trading_day(datetime(2027, 1, 5, 12, 0, tzinfo=IST))
        self.assertIn("2027", str(ctx.exception))
        self.assertIn("not updated", str(ctx.exception))

    def test_uncovered_past_year_raises_via_is_market_open(self) -> None:
        with self.assertRaises(RuntimeError):
            market_calendar.is_market_open(datetime(2025, 7, 2, 10, 0, tzinfo=IST))

    def test_uncovered_year_raises_via_session_phase(self) -> None:
        with self.assertRaises(RuntimeError):
            market_calendar.session_phase(datetime(2028, 3, 1, 10, 0, tzinfo=IST))


if __name__ == "__main__":
    unittest.main()
