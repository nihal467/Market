from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import monthly_review  # noqa: E402


class PreviousMonthTests(unittest.TestCase):
    def test_mid_month_maps_to_previous_calendar_month(self) -> None:
        label, start, end = monthly_review.previous_month(date(2026, 7, 19))
        self.assertEqual(label, "2026-06")
        self.assertEqual(start, date(2026, 6, 1))
        self.assertEqual(end, date(2026, 6, 30))

    def test_first_of_month_still_reviews_the_month_that_just_ended(self) -> None:
        label, start, end = monthly_review.previous_month(date(2026, 8, 1))
        self.assertEqual(label, "2026-07")
        self.assertEqual(start, date(2026, 7, 1))
        self.assertEqual(end, date(2026, 7, 31))

    def test_january_rolls_back_to_december_of_prior_year(self) -> None:
        label, start, end = monthly_review.previous_month(date(2026, 1, 1))
        self.assertEqual(label, "2025-12")
        self.assertEqual(start, date(2025, 12, 1))
        self.assertEqual(end, date(2025, 12, 31))

    def test_march_window_covers_leap_february(self) -> None:
        label, start, end = monthly_review.previous_month(date(2028, 3, 15))
        self.assertEqual(label, "2028-02")
        self.assertEqual(end, date(2028, 2, 29))


class DedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_gh = monthly_review.lib.gh_request

    def tearDown(self) -> None:
        monthly_review.lib.gh_request = self.old_gh

    def test_exact_title_match_is_found(self) -> None:
        def fake_gh(method, path, payload=None, accept=""):
            self.assertEqual(method, "GET")
            self.assertIn("/search/issues", path)
            return {"items": [
                {"number": 3, "title": "Meta-review: 2026-06 (draft)"},
                {"number": 4, "title": "Meta-review: 2026-06"},
            ]}
        monthly_review.lib.gh_request = fake_gh

        self.assertEqual(
            monthly_review.find_existing_issue("Meta-review: 2026-06"), 4)

    def test_near_miss_titles_and_prs_do_not_dedupe(self) -> None:
        def fake_gh(method, path, payload=None, accept=""):
            return {"items": [
                {"number": 5, "title": "Meta-review: 2026-05"},
                {"number": 6, "title": "Meta-review: 2026-06",
                 "pull_request": {"url": "x"}},
            ]}
        monthly_review.lib.gh_request = fake_gh

        self.assertIsNone(
            monthly_review.find_existing_issue("Meta-review: 2026-06"))

    def test_empty_search_result_returns_none(self) -> None:
        monthly_review.lib.gh_request = lambda *a, **k: {"items": []}
        self.assertIsNone(
            monthly_review.find_existing_issue("Meta-review: 2026-06"))


class MonthPerformanceTests(unittest.TestCase):
    def test_no_paper_data_is_tolerated(self) -> None:
        perf = monthly_review.month_performance(None, date(2026, 6, 1), date(2026, 6, 30))
        self.assertFalse(perf["available"])

    def test_month_return_anchors_to_last_row_before_the_month(self) -> None:
        paper = {"history": [
            {"date": "2026-05-29", "value": 500000.0, "benchmark_value": 500000.0},
            {"date": "2026-06-10", "value": 505000.0, "benchmark_value": 501000.0,
             "total_pnl_pct": 1.0, "alpha_pct": 0.8},
            {"date": "2026-06-30", "value": 510000.0, "benchmark_value": 502000.0,
             "total_pnl_pct": 2.0, "alpha_pct": 1.6},
        ]}
        perf = monthly_review.month_performance(paper, date(2026, 6, 1), date(2026, 6, 30))
        self.assertTrue(perf["available"])
        self.assertEqual(perf["trading_days"], 2)
        self.assertAlmostEqual(perf["month_return_pct"], 2.0, places=2)
        self.assertAlmostEqual(perf["month_benchmark_return_pct"], 0.4, places=2)
        self.assertAlmostEqual(perf["month_alpha_pct"], 1.6, places=2)


if __name__ == "__main__":
    unittest.main()
