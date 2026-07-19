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

# backtest.py imports yfinance at module level; skip cleanly where the market
# data stack is not installed (CI installs requirements.txt, so it runs there).
try:
    import backtest  # noqa: E402
    import pandas as pd  # noqa: E402
    HAVE_STACK = True
except Exception:  # noqa: BLE001 — any import failure means "not available"
    HAVE_STACK = False


@unittest.skipUnless(HAVE_STACK, "yfinance/pandas stack not installed")
class NewsAblationPairTests(unittest.TestCase):
    def test_pair_is_matched_to_the_active_profile(self) -> None:
        base = backtest._active_variant()
        with_v, without_v = backtest._news_ablation_pair()

        self.assertTrue(with_v["use_news"])
        self.assertFalse(without_v["use_news"])
        self.assertEqual(with_v["id"], f"{base['id']}__news_on")
        self.assertEqual(without_v["id"], f"{base['id']}__news_off")
        # Identical config apart from the news toggle — that is the ablation.
        for key in ("score", "rebalance_every", "use_regime", "hold_until_rank"):
            self.assertEqual(with_v[key], base[key], key)
            self.assertEqual(without_v[key], base[key], key)


@unittest.skipUnless(HAVE_STACK, "yfinance/pandas stack not installed")
class VariantScoreNewsTests(unittest.TestCase):
    IND = {"price": 100.0, "sma50": 90.0, "sma200": 80.0, "rsi14": 55.0,
           "pct_from_high": -8.0, "pct_from_low": 20.0, "ret_3m": 10.0}

    def test_no_sentiment_matches_previous_behaviour(self) -> None:
        self.assertEqual(backtest._variant_score(self.IND, "momentum"), 10.0)

    def test_momentum_mode_adds_graded_news_term(self) -> None:
        base = backtest._variant_score(self.IND, "momentum")
        # Fully confident (count >= NEWS_CONF_FULL), maximally positive news
        # adds exactly W_NEWS; half confidence adds half.
        full = backtest._variant_score(self.IND, "momentum",
                                       {"score": 1.0, "count": 6})
        half = backtest._variant_score(self.IND, "momentum",
                                       {"score": 1.0, "count": 3})
        self.assertAlmostEqual(full - base, backtest.strategy.W_NEWS)
        self.assertAlmostEqual(half - base, backtest.strategy.W_NEWS / 2)

    def test_zero_count_sentiment_changes_nothing(self) -> None:
        base = backtest._variant_score(self.IND, "momentum")
        self.assertEqual(
            backtest._variant_score(self.IND, "momentum",
                                    {"score": 1.0, "count": 0}), base)

    def test_news_never_rescues_the_no_data_sentinel(self) -> None:
        no_mom = {**self.IND, "ret_3m": None}
        self.assertEqual(
            backtest._variant_score(no_mom, "momentum",
                                    {"score": 1.0, "count": 6}), -999.0)

    def test_full_mode_lets_news_flip_hold_to_buy(self) -> None:
        # Crafted borderline: trend +1, death cross -1, RSI neutral, no 52w
        # tilt, no momentum -> composite 0 (HOLD -> sentinel). Strong confident
        # news adds +W_NEWS = +2.0, exactly the BUY threshold.
        ind = {"price": 100.0, "sma50": 90.0, "sma200": 95.0, "rsi14": 50.0,
               "pct_from_high": -50.0, "pct_from_low": 100.0, "ret_3m": None}
        self.assertEqual(backtest._variant_score(ind, "full"), -999.0)
        self.assertEqual(
            backtest._variant_score(ind, "full", {"score": 1.0, "count": 6}),
            2.0)


@unittest.skipUnless(HAVE_STACK, "yfinance/pandas stack not installed")
class LoadArchivedNewsTests(unittest.TestCase):
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

    def test_empty_data_branch_yields_empty_lookup(self) -> None:
        dates = [pd.Timestamp("2026-07-16"), pd.Timestamp("2026-07-17")]
        self.assertEqual(backtest._load_archived_news(dates), {})

    def test_archived_day_maps_symbols_to_score_and_count(self) -> None:
        path = self.root / "news" / "2026" / "07" / "17.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "lexicon_version": "v1",
            "symbols": [
                {"symbol": "AAA.NS", "score": 0.5, "n_headlines": 4},
                {"symbol": "BBB.NS", "score": -0.25, "n_headlines": 0},
                {"score": 1.0, "n_headlines": 2},   # no symbol -> ignored
            ],
        }), encoding="utf-8")

        out = backtest._load_archived_news(
            [pd.Timestamp("2026-07-16"), pd.Timestamp("2026-07-17")])

        self.assertEqual(out, {"2026-07-17": {
            "AAA.NS": {"score": 0.5, "count": 4},
            "BBB.NS": {"score": -0.25, "count": 0},
        }})


if __name__ == "__main__":
    unittest.main()
