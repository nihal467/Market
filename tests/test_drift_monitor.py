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

import drift_monitor  # noqa: E402


def rows(values: list[float], start_day: int = 1) -> list[dict]:
    return [{"date": f"2026-07-{start_day + i:02d}", "value": v}
            for i, v in enumerate(values)]


class CompareTests(unittest.TestCase):
    def test_too_few_common_days_returns_none(self) -> None:
        paper = rows([500000 + i for i in range(5)])
        sim = {r["date"]: r["value"] for r in paper}
        self.assertIsNone(drift_monitor._compare(sim, paper))

    def test_identical_curves_are_ok(self) -> None:
        values = [500000 * (1 + 0.001 * i + 0.002 * (i % 3)) for i in range(12)]
        paper = rows(values)
        sim = {r["date"]: r["value"] for r in paper}

        metrics = drift_monitor._compare(sim, paper)

        self.assertEqual(metrics["n_common_days"], 12)
        self.assertAlmostEqual(metrics["final_divergence_pct"], 0.0, places=3)
        self.assertAlmostEqual(metrics["daily_return_correlation"], 1.0, places=3)
        self.assertAlmostEqual(metrics["max_single_day_gap_pct"], 0.0, places=3)
        self.assertEqual(drift_monitor._verdict(metrics), "ok")

    def test_diverged_final_value_is_drift(self) -> None:
        values = [500000 * (1 + 0.001 * i + 0.002 * (i % 3)) for i in range(12)]
        paper = rows(values)
        # Same shape but scaled 5% higher -> perfectly correlated returns,
        # final divergence well past the 2% limit.
        sim = {r["date"]: r["value"] * 1.05 for r in paper}

        metrics = drift_monitor._compare(sim, paper)

        self.assertGreater(abs(metrics["final_divergence_pct"]), 2.0)
        self.assertEqual(drift_monitor._verdict(metrics), "drift")

    def test_uncorrelated_returns_are_drift(self) -> None:
        paper = rows([500000, 501000, 499500, 502000, 500500, 503000,
                      501500, 504000, 502500, 505000, 503500, 506000])
        # Mirror-image path: anti-correlated daily returns.
        sim_vals = [500000, 499000, 500500, 498000, 499500, 497000,
                    498500, 496000, 497500, 495000, 496500, 500000]
        sim = {r["date"]: v for r, v in zip(paper, sim_vals)}

        metrics = drift_monitor._compare(sim, paper)

        self.assertLess(metrics["daily_return_correlation"], 0.9)
        self.assertEqual(drift_monitor._verdict(metrics), "drift")

    def test_flat_series_has_no_measurable_correlation(self) -> None:
        paper = rows([500000.0] * 12)
        sim = {r["date"]: 500000.0 for r in paper}

        metrics = drift_monitor._compare(sim, paper)

        self.assertIsNone(metrics["daily_return_correlation"])
        self.assertEqual(drift_monitor._verdict(metrics), "insufficient_data")

    def test_verdict_of_none_metrics_is_insufficient_data(self) -> None:
        self.assertEqual(drift_monitor._verdict(None), "insufficient_data")


class ShortHistoryTests(unittest.TestCase):
    """run() with a short paper history must write insufficient_data, no crash."""

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

    def test_empty_datastore_writes_insufficient_data(self) -> None:
        payload = drift_monitor.run()

        self.assertEqual(payload["verdict"], "insufficient_data")
        self.assertEqual(payload["n_paper_days"], 0)
        written = json.loads(
            (self.root / "drift" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(written["verdict"], "insufficient_data")
        self.assertIn("note", written)

    def test_short_history_writes_insufficient_data(self) -> None:
        state = {"history": [{"date": f"2026-07-{d:02d}", "value": 500000 + d}
                             for d in range(1, 6)]}
        path = self.root / "paper" / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")

        payload = drift_monitor.run()

        self.assertEqual(payload["verdict"], "insufficient_data")
        self.assertEqual(payload["n_paper_days"], 5)
        self.assertEqual(payload["paper_start"], "2026-07-01")
        self.assertEqual(payload["paper_end"], "2026-07-05")


if __name__ == "__main__":
    unittest.main()
