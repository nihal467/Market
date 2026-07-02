from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import paper_trader  # noqa: E402
from paper_trader import _normalize_history  # noqa: E402


class PaperTraderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_dir = os.environ.get("MARKET_DATA_DIR")
        os.environ["MARKET_DATA_DIR"] = str(self.root)

    def tearDown(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
