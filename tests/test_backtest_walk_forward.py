from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# backtest.py imports yfinance at module level; skip cleanly where the market
# data stack is not installed (CI installs requirements.txt, so it runs there).
try:
    import backtest  # noqa: E402
    import pandas as pd  # noqa: E402
    HAVE_STACK = True
except Exception:  # noqa: BLE001 — any import failure means "not available"
    HAVE_STACK = False


@unittest.skipUnless(HAVE_STACK, "yfinance/pandas stack not installed")
class WalkForwardTests(unittest.TestCase):
    def make_series(self, n: int, daily_ret: float):
        dates = pd.bdate_range("2025-01-01", periods=n)
        eq_dates = [d.strftime("%Y-%m-%d") for d in dates]
        equity = [backtest.START_CAPITAL * (1 + daily_ret) ** i for i in range(n)]
        return equity, eq_dates, dates

    def test_short_history_returns_none(self) -> None:
        equity, eq_dates, _ = self.make_series(15, 0.001)
        bclose = pd.Series(dtype=float)
        self.assertIsNone(backtest._walk_forward(equity, eq_dates, bclose))

    def test_three_expanding_folds_with_positive_alpha(self) -> None:
        n = 40
        equity, eq_dates, dates = self.make_series(n, 0.002)  # strategy outruns...
        bclose = pd.Series([100 * (1 + 0.001) ** i for i in range(n)],
                           index=dates)                       # ...the benchmark

        wf = backtest._walk_forward(equity, eq_dates, bclose)

        self.assertEqual(wf["n_folds"], backtest.WALK_FORWARD_FOLDS)
        self.assertTrue(wf["expanding_window"])
        self.assertEqual(len(wf["folds"]), 3)
        # Folds tile the post-train window sequentially with expanding trains.
        prev_end = None
        prev_train = 0
        for fold in wf["folds"]:
            self.assertGreater(fold["train_days"], prev_train)
            prev_train = fold["train_days"]
            if prev_end is not None:
                self.assertGreater(fold["validation_start"], prev_end)
            prev_end = fold["validation_end"]
            self.assertGreater(fold["validation_alpha_pct"], 0)
        self.assertEqual(wf["folds_positive"], 3)
        self.assertGreater(wf["mean_validation_alpha_pct"], 0)
        self.assertEqual(wf["folds"][-1]["validation_end"], eq_dates[-1])

    def test_missing_benchmark_yields_none_alphas(self) -> None:
        equity, eq_dates, _ = self.make_series(40, 0.002)
        bclose = pd.Series(dtype=float)

        wf = backtest._walk_forward(equity, eq_dates, bclose)

        self.assertEqual(wf["folds_positive"], 0)
        self.assertIsNone(wf["mean_validation_alpha_pct"])
        for fold in wf["folds"]:
            self.assertIsNone(fold["validation_alpha_pct"])
            self.assertIsNotNone(fold["validation_return_pct"])

    def test_validation_split_carries_walk_forward(self) -> None:
        n = 40
        equity, eq_dates, dates = self.make_series(n, 0.002)
        bclose = pd.Series([100 * (1 + 0.001) ** i for i in range(n)], index=dates)

        split = backtest._validation_split(equity, eq_dates, bclose)

        # Old single-split fields are preserved for backward compatibility...
        self.assertIn("validation_alpha_pct", split)
        self.assertIn("train_return_pct", split)
        # ...and the new walk-forward summary rides alongside them.
        self.assertIsNotNone(split["walk_forward"])
        self.assertEqual(split["walk_forward"]["n_folds"], 3)


if __name__ == "__main__":
    unittest.main()
