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

import news_archive  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
FIXED_NOW = datetime(2026, 7, 17, 18, 0, tzinfo=IST)
DATED_REL = "news/2026/07/17.json"


def fake_item(title: str, score: float, published: str = "2026-07-17T05:00:00+00:00") -> dict:
    return {"title": title, "clean_title": title, "source": "Test Wire",
            "published_at": published, "score": score}


class NewsArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_dir = os.environ.get("MARKET_DATA_DIR")
        os.environ["MARKET_DATA_DIR"] = str(self.root)
        self.old_now = news_archive.now_ist
        news_archive.now_ist = lambda: FIXED_NOW
        self.old_score_headlines = news_archive.score_headlines
        self.old_load_universe = news_archive.load_universe

    def tearDown(self) -> None:
        news_archive.now_ist = self.old_now
        news_archive.score_headlines = self.old_score_headlines
        news_archive.load_universe = self.old_load_universe
        if self.old_data_dir is None:
            os.environ.pop("MARKET_DATA_DIR", None)
        else:
            os.environ["MARKET_DATA_DIR"] = self.old_data_dir
        self.tmp.cleanup()

    def write_watchlist(self, symbols: list[tuple[str, str]]) -> None:
        payload = {"watchlist": [{"symbol": s, "name": n, "rank": i + 1}
                                 for i, (s, n) in enumerate(symbols)]}
        path = self.root / "watchlist" / "latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def read(self, rel: str) -> dict:
        return json.loads((self.root / rel).read_text(encoding="utf-8"))

    # ---- payload shaping -----------------------------------------------

    def test_payload_shape_truncation_and_aggregate(self) -> None:
        self.write_watchlist([("AAA.NS", "Alpha Ltd")])
        # 12 items (only 10 must be kept) with one over-long title.
        items = [fake_item("T" * 250, 1.0)] + \
                [fake_item(f"headline {i}", 1.0 if i % 2 else -1.0) for i in range(11)]
        news_archive.score_headlines = lambda query, max_items=10: list(items)

        payload = news_archive.run()

        self.assertEqual(payload["lexicon_version"], "v1")
        self.assertEqual(payload["source"], "watchlist")
        self.assertEqual(payload["n_symbols"], 1)
        row = payload["symbols"][0]
        self.assertEqual(row["symbol"], "AAA.NS")
        self.assertEqual(row["n_headlines"], 10)          # capped at 10
        self.assertEqual(len(row["headlines"]), 10)
        for h in row["headlines"]:
            self.assertLessEqual(len(h["title"]), 200)    # titles truncated
            self.assertEqual(set(h), {"title", "published", "score"})
        # Aggregate score is the mean of the ARCHIVED headline scores.
        kept_scores = [h["score"] for h in row["headlines"]]
        self.assertAlmostEqual(row["score"],
                               round(sum(kept_scores) / len(kept_scores), 3))
        self.assertEqual(row["confidence"], 1.0)          # 10 headlines >= conf cap
        # Both the dated file and latest.json are written and identical.
        self.assertEqual(self.read(DATED_REL), self.read("news/latest.json"))
        self.assertEqual(self.read(DATED_REL)["symbols"][0]["symbol"], "AAA.NS")

    def test_per_symbol_failure_is_skipped_with_note(self) -> None:
        self.write_watchlist([("AAA.NS", "Alpha"), ("BBB.NS", "Beta"),
                              ("CCC.NS", "Gamma")])

        def flaky(query, max_items=10):
            if "Beta" in query:
                raise RuntimeError("rss exploded")
            return [fake_item("fine", 0.5)]

        news_archive.score_headlines = flaky

        payload = news_archive.run()

        self.assertEqual(payload["n_symbols"], 2)
        self.assertEqual(payload["n_failed"], 1)
        self.assertEqual(payload["failures"][0]["symbol"], "BBB.NS")
        self.assertIn("rss exploded", payload["failures"][0]["error"])
        self.assertIn("skipped", payload["note"])
        self.assertEqual([r["symbol"] for r in payload["symbols"]],
                         ["AAA.NS", "CCC.NS"])
        # The file still landed despite the failure.
        self.assertEqual(self.read(DATED_REL)["n_failed"], 1)

    def test_no_headlines_still_writes_valid_file(self) -> None:
        self.write_watchlist([("AAA.NS", "Alpha")])
        news_archive.score_headlines = lambda query, max_items=10: []

        payload = news_archive.run()

        row = payload["symbols"][0]
        self.assertEqual(row["n_headlines"], 0)
        self.assertEqual(row["score"], 0.0)
        self.assertEqual(row["confidence"], 0.0)
        self.assertEqual(row["headlines"], [])
        self.assertTrue((self.root / DATED_REL).exists())

    # ---- symbol sources -------------------------------------------------

    def test_missing_watchlist_falls_back_to_universe_capped_at_50(self) -> None:
        # Data branch starts empty: no watchlist file at all.
        news_archive.score_headlines = lambda query, max_items=10: [fake_item("x", 0.0)]

        payload = news_archive.run()

        self.assertEqual(payload["source"], "universe_fallback")
        self.assertEqual(payload["n_symbols"], 50)        # capped
        self.assertEqual(len({r["symbol"] for r in payload["symbols"]}), 50)

    def test_no_watchlist_and_broken_universe_writes_empty_valid_file(self) -> None:
        def boom():
            raise OSError("universe.csv unreadable")

        news_archive.load_universe = boom
        news_archive.score_headlines = lambda query, max_items=10: [fake_item("x", 0.0)]

        payload = news_archive.run()

        self.assertEqual(payload["source"], "none")
        self.assertEqual(payload["symbols"], [])
        self.assertEqual(payload["n_symbols"], 0)
        self.assertIn("nothing to archive", payload["note"])
        self.assertEqual(self.read("news/latest.json")["symbols"], [])

    # ---- idempotency ----------------------------------------------------

    def test_same_date_rerun_overwrites_deterministically(self) -> None:
        self.write_watchlist([("AAA.NS", "Alpha")])
        news_archive.score_headlines = lambda query, max_items=10: [fake_item("old", 1.0)]
        news_archive.run()
        news_archive.score_headlines = lambda query, max_items=10: [fake_item("new", -1.0)]

        news_archive.run()

        dated = self.read(DATED_REL)
        self.assertEqual(dated["symbols"][0]["headlines"][0]["title"], "new")
        self.assertEqual(dated["symbols"][0]["score"], -1.0)
        # Exactly one dated file for the day, mirrored to latest.
        day_dir = self.root / "news" / "2026" / "07"
        self.assertEqual([p.name for p in sorted(day_dir.iterdir())], ["17.json"])
        self.assertEqual(dated, self.read("news/latest.json"))


if __name__ == "__main__":
    unittest.main()
