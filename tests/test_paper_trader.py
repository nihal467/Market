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

import paper_trader  # noqa: E402
from paper_trader import _normalize_history  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


class PaperTraderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_dir = os.environ.get("MARKET_DATA_DIR")
        os.environ["MARKET_DATA_DIR"] = str(self.root)
        self.old_benchmark_level = paper_trader._benchmark_level
        paper_trader._benchmark_level = lambda: None

    def tearDown(self) -> None:
        paper_trader._benchmark_level = self.old_benchmark_level
        if self.old_data_dir is None:
            os.environ.pop("MARKET_DATA_DIR", None)
        else:
            os.environ["MARKET_DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def write_json(self, rel: str, payload: dict) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_normalize_history_sorts_and_dedupes_by_date(self) -> None:
        rows = [
            {"date": "2026-07-02", "value": 502},
            {"date": "2026-07-01", "value": 501},
            {"date": "2026-07-02", "value": 503},
            {"value": 999},
        ]

        normalized = _normalize_history(rows)

        self.assertEqual(
            normalized,
            [
                {"date": "2026-07-01", "value": 501},
                {"date": "2026-07-02", "value": 503},
            ],
        )

    def test_normalize_history_drops_rows_before_inception(self) -> None:
        rows = [
            {"date": "2026-06-29", "value": 499},
            {"date": "2026-06-30", "value": 500},
            {"date": "2026-07-01", "value": 501},
        ]

        normalized = _normalize_history(rows, inception="2026-06-30")

        self.assertEqual(
            normalized,
            [
                {"date": "2026-06-30", "value": 500},
                {"date": "2026-07-01", "value": 501},
            ],
        )

    def test_stale_daily_analysis_does_not_rewind_paper_state(self) -> None:
        self.write_json("daily/latest.json", {
            "trading_date": "2026-06-30",
            "analysis": [{"symbol": "AAA.NS", "name": "AAA", "price": 100.0}],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-06-30",
            "start_capital": 500000.0,
            "cash": 100000.0,
            "positions": {"AAA.NS": {"qty": 10, "avg_price": 90.0, "name": "AAA"}},
            "last_date": "2026-07-01",
            "history": [
                {"date": "2026-06-30", "value": 499000.0, "total_pnl": -1000.0},
                {"date": "2026-07-01", "value": 501000.0, "total_pnl": 1000.0},
            ],
        })
        self.write_json("paper/latest.json", {
            "inception": "2026-06-30",
            "start_capital": 500000.0,
            "value": 499000.0,
            "cash": 100000.0,
            "total_pnl": -1000.0,
            "history": [],
        })

        result = paper_trader.run()

        self.assertEqual(result["reason"], "stale_trading_date")
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        latest = json.loads((self.root / "paper/latest.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_date"], "2026-07-01")
        self.assertEqual([row["date"] for row in state["history"]], ["2026-06-30", "2026-07-01"])
        self.assertEqual(latest["value"], 501000.0)
        self.assertEqual(latest["total_pnl"], 1000.0)

    def test_same_day_paper_trade_skips_before_market_close(self) -> None:
        old_now = paper_trader.now_ist
        paper_trader.now_ist = lambda: datetime(2026, 7, 2, 14, 56, tzinfo=IST)
        self.addCleanup(lambda: setattr(paper_trader, "now_ist", old_now))
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "analysis": [{"symbol": "AAA.NS", "name": "AAA", "price": 120.0}],
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 499000.0,
            "positions": {"AAA.NS": {"qty": 10, "avg_price": 90.0, "name": "AAA"}},
            "last_date": "2026-07-01",
            "history": [{"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0}],
        })

        result = paper_trader.run()

        self.assertEqual(result["reason"], "market_not_closed")
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_date"], "2026-07-01")
        self.assertEqual([row["date"] for row in state["history"]], ["2026-07-01"])

    def test_post_close_run_replaces_preliminary_same_day_snapshot(self) -> None:
        old_now = paper_trader.now_ist
        paper_trader.now_ist = lambda: datetime(2026, 7, 2, 16, 40, tzinfo=IST)
        self.addCleanup(lambda: setattr(paper_trader, "now_ist", old_now))
        self.write_json("daily/latest.json", {
            "trading_date": "2026-07-02",
            "analysis": [{
                "symbol": "AAA.NS",
                "name": "AAA",
                "price": 120.0,
                "signal": "BUY",
                "score": 4.0,
                "ret_3m": 10.0,
            }],
            "market_regime": {"risk_on": False, "reason": "test"},
        })
        self.write_json("paper/state.json", {
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "cash": 499000.0,
            "positions": {"AAA.NS": {"qty": 10, "avg_price": 90.0, "name": "AAA"}},
            "last_date": "2026-07-02",
            "last_rebalance_key": "2026-W27",
            "history": [
                {"date": "2026-07-01", "value": 500000.0, "total_pnl": 0.0},
                {"date": "2026-07-02", "value": 500100.0, "day_pnl": 100.0, "total_pnl": 100.0},
            ],
        })
        self.write_json("paper/latest.json", {
            "ist": "2026-07-02T14:56:00+05:30",
            "inception": "2026-07-01",
            "start_capital": 500000.0,
            "value": 500100.0,
            "history": [{"date": "2026-07-02", "value": 500100.0}],
        })

        result = paper_trader.run()

        self.assertFalse(result.get("skipped", False))
        state = json.loads((self.root / "paper/state.json").read_text(encoding="utf-8"))
        latest = json.loads((self.root / "paper/latest.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_date"], "2026-07-02")
        self.assertEqual([row["date"] for row in state["history"]], ["2026-07-01", "2026-07-02"])
        self.assertNotEqual(state["history"][-1]["value"], 500100.0)
        self.assertTrue(state["history"][-1]["final"])
        self.assertEqual(latest["value"], state["history"][-1]["value"])
        self.assertEqual(latest["day_pnl"], state["history"][-1]["day_pnl"])


if __name__ == "__main__":
    unittest.main()
