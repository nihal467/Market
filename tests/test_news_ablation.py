from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Pure module by design — importable without pandas/yfinance (unlike backtest).
import news_ablation  # noqa: E402
from news_ablation import (  # noqa: E402
    NEWS_ABLATION_NEUTRAL_BAND_PP,
    ablation_verdict,
    build_news_ablation,
)


def make_row(vid: str, val_alpha, *, walk_forward: bool = True,
             news_days: int = 0, total: float = 10.0, alpha: float = 2.0) -> dict:
    """A strategy-lab result row shaped like backtest._simulate_variant's."""
    validation: dict = {"validation_alpha_pct": val_alpha}
    if walk_forward:
        validation["walk_forward"] = {
            "n_folds": 3,
            "expanding_window": True,
            "folds": [],
            "mean_validation_alpha_pct": 1.5,
            "folds_positive": 2,
            "note": "test",
        }
    return {
        "id": vid,
        "total_return_pct": total,
        "alpha_pct": alpha,
        "validation": validation,
        "use_news": vid.endswith("news_on"),
        "news_days_used": news_days,
    }


class VerdictTests(unittest.TestCase):
    def test_band_is_half_a_percentage_point(self) -> None:
        self.assertEqual(NEWS_ABLATION_NEUTRAL_BAND_PP, 0.5)

    def test_delta_above_band_is_helping(self) -> None:
        self.assertEqual(ablation_verdict(0.51, True, 5), "news_helping")
        self.assertEqual(ablation_verdict(4.0, True, 5), "news_helping")

    def test_delta_below_band_is_hurting(self) -> None:
        self.assertEqual(ablation_verdict(-0.51, True, 5), "news_hurting")
        self.assertEqual(ablation_verdict(-3.2, True, 5), "news_hurting")

    def test_inside_band_is_neutral_including_boundaries(self) -> None:
        for delta in (0.0, 0.5, -0.5, 0.25, -0.49):
            self.assertEqual(ablation_verdict(delta, True, 5), "news_neutral",
                             f"delta {delta} should be neutral")

    def test_missing_walk_forward_is_insufficient(self) -> None:
        self.assertEqual(ablation_verdict(4.0, False, 5), "insufficient_data")

    def test_missing_delta_is_insufficient(self) -> None:
        self.assertEqual(ablation_verdict(None, True, 5), "insufficient_data")

    def test_zero_archived_news_days_is_insufficient(self) -> None:
        # Empty archive => the pair was identical by construction; a delta of
        # 0.0 says nothing about news, so the verdict must not claim neutral.
        self.assertEqual(ablation_verdict(0.0, True, 0), "insufficient_data")
        self.assertEqual(ablation_verdict(2.0, True, 0), "insufficient_data")

    def test_unknown_news_days_falls_back_to_delta(self) -> None:
        self.assertEqual(ablation_verdict(2.0, True, None), "news_helping")


class BuildNewsAblationTests(unittest.TestCase):
    def test_full_pair_shapes_payload_and_delta(self) -> None:
        with_row = make_row("p__news_on", 3.1, news_days=14)
        without_row = make_row("p__news_off", 1.9)

        out = build_news_ablation(with_row, without_row)

        self.assertEqual(out["with_news"]["id"], "p__news_on")
        self.assertEqual(out["without_news"]["id"], "p__news_off")
        self.assertEqual(out["with_news"]["validation_alpha_pct"], 3.1)
        self.assertEqual(out["without_news"]["validation_alpha_pct"], 1.9)
        self.assertAlmostEqual(out["delta_validation_alpha_pct"], 1.2)
        self.assertEqual(out["news_days_used"], 14)
        self.assertEqual(out["verdict"], "news_helping")
        self.assertIn("note", out)
        # Walk-forward summary is compact — folds themselves are not copied.
        wf = out["with_news"]["walk_forward"]
        self.assertEqual(set(wf), {"n_folds", "mean_validation_alpha_pct",
                                   "folds_positive"})

    def test_small_delta_with_news_days_is_neutral(self) -> None:
        out = build_news_ablation(make_row("a__news_on", 2.0, news_days=30),
                                  make_row("a__news_off", 1.8))
        self.assertAlmostEqual(out["delta_validation_alpha_pct"], 0.2)
        self.assertEqual(out["verdict"], "news_neutral")

    def test_negative_delta_is_hurting(self) -> None:
        out = build_news_ablation(make_row("a__news_on", 0.5, news_days=30),
                                  make_row("a__news_off", 2.0))
        self.assertAlmostEqual(out["delta_validation_alpha_pct"], -1.5)
        self.assertEqual(out["verdict"], "news_hurting")

    def test_zero_news_days_yields_insufficient_data(self) -> None:
        out = build_news_ablation(make_row("a__news_on", 2.0, news_days=0),
                                  make_row("a__news_off", 2.0))
        self.assertEqual(out["verdict"], "insufficient_data")

    def test_missing_walk_forward_yields_insufficient_data(self) -> None:
        out = build_news_ablation(
            make_row("a__news_on", 2.0, walk_forward=False, news_days=9),
            make_row("a__news_off", 1.0))
        self.assertIsNone(out["with_news"]["walk_forward"])
        self.assertEqual(out["verdict"], "insufficient_data")

    def test_missing_validation_alpha_yields_none_delta(self) -> None:
        out = build_news_ablation(make_row("a__news_on", None, news_days=9),
                                  make_row("a__news_off", 1.0))
        self.assertIsNone(out["delta_validation_alpha_pct"])
        self.assertEqual(out["verdict"], "insufficient_data")

    def test_none_rows_are_tolerated(self) -> None:
        out = build_news_ablation(None, None)
        self.assertIsNone(out["delta_validation_alpha_pct"])
        self.assertIsNone(out["news_days_used"])
        self.assertEqual(out["verdict"], "insufficient_data")

    def test_module_stays_light(self) -> None:
        # The whole point of the module: usable where pandas/yfinance are not.
        for heavy in ("pandas", "numpy", "yfinance"):
            self.assertNotIn(heavy, news_ablation.__dict__)


if __name__ == "__main__":
    unittest.main()
