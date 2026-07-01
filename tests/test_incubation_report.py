from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import incubation_report  # noqa: E402


IST = timezone(timedelta(hours=5, minutes=30))


def write_json(root: Path, rel: str, payload: dict) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class IncubationReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_dir = os.environ.get("MARKET_DATA_DIR")
        os.environ["MARKET_DATA_DIR"] = str(self.root)

        fixed_now = datetime(2026, 7, 1, 18, 0, tzinfo=IST)
        self.old_now_ist = incubation_report.now_ist
        self.old_is_market_open = incubation_report.is_market_open
        incubation_report.now_ist = lambda: fixed_now
        incubation_report.is_market_open = lambda _dt=None: False

    def tearDown(self) -> None:
        incubation_report.now_ist = self.old_now_ist
        incubation_report.is_market_open = self.old_is_market_open
        if self.old_data_dir is None:
            os.environ.pop("MARKET_DATA_DIR", None)
        else:
            os.environ["MARKET_DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def seed_common_files(self, history: list[dict]) -> None:
        write_json(self.root, "paper/latest.json", {
            "ist": "2026-07-01T16:30:00+05:30",
            "inception": "2026-07-01",
            "value": 500000,
            "total_pnl_pct": 0.0,
            "alpha_pct": 0.0,
            "history": history,
        })
        write_json(self.root, "paper/live.json", {
            "ist": "2026-07-01T15:20:00+05:30",
            "market_open": False,
            "intraday_trades": [],
            "stops": [],
        })
        write_json(self.root, "daily/latest.json", {
            "trading_date": "2026-07-01",
            "market_regime": {"risk_on": True, "reason": "test"},
        })
        write_json(self.root, "watchlist/latest.json", {
            "generated_ist": "2026-07-01T08:00:00+05:30",
        })
        write_json(self.root, "backtest/latest.json", {
            "strategy_lab": {
                "variants": [
                    {
                        "id": "momentum_only_weekly",
                        "total_return_pct": 10.0,
                        "alpha_pct": 8.0,
                    }
                ]
            }
        })

    def churn_criterion(self) -> dict:
        report = incubation_report.run()
        return next(c for c in report["criteria"] if c["key"] == "churn_controlled")

    def test_inception_buys_do_not_count_as_churn(self) -> None:
        self.seed_common_files([
            {
                "date": "2026-07-01",
                "value": 500000,
                "n_trades": 10,
            }
        ])

        criterion = self.churn_criterion()

        self.assertTrue(criterion["passed"])
        self.assertIn("0.00 average trades/day after inception", criterion["detail"])
        self.assertIn("0 trade days", criterion["detail"])

    def test_post_inception_trades_are_still_measured(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
            {"date": "2026-07-02", "value": 501000, "n_trades": 6},
            {"date": "2026-07-03", "value": 502000, "n_trades": 4},
        ])

        criterion = self.churn_criterion()

        self.assertFalse(criterion["passed"])
        self.assertIn("5.00 average trades/day after inception", criterion["detail"])
        self.assertIn("2 trade days", criterion["detail"])

    def test_history_is_sorted_before_churn_window(self) -> None:
        self.seed_common_files([
            {"date": "2026-07-02", "value": 501000, "n_trades": 6},
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
            {"date": "2026-07-03", "value": 502000, "n_trades": 0},
        ])

        criterion = self.churn_criterion()

        self.assertTrue(criterion["passed"])
        self.assertIn("3.00 average trades/day after inception", criterion["detail"])
        self.assertIn("1 trade days", criterion["detail"])

    def test_pre_inception_rows_do_not_count_as_churn(self) -> None:
        self.seed_common_files([
            {"date": "2026-06-30", "value": 499000, "n_trades": 7},
            {"date": "2026-07-01", "value": 500000, "n_trades": 10},
            {"date": "2026-07-02", "value": 501000, "n_trades": 0},
        ])

        criterion = self.churn_criterion()

        self.assertTrue(criterion["passed"])
        self.assertIn("0.00 average trades/day after inception", criterion["detail"])
        self.assertIn("0 trade days", criterion["detail"])


if __name__ == "__main__":
    unittest.main()
