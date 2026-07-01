from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_trader import _normalize_history  # noqa: E402


class PaperTraderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
