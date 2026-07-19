"""Node smoke test for the dashboard script (docs/index.html).

Extracts the inline <script>, runs it under node with a stubbed DOM + fetch,
and checks that the news-ablation section renders from a backtest fixture and
that absence of data degrades to a message instead of a crash. Skips cleanly
where node is not installed (CI's ubuntu runner ships node).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

INDEX = ROOT / "docs" / "index.html"
NODE = shutil.which("node")

# Minimal DOM/fetch stand-ins. Elements record innerHTML/textContent so the
# test can assert on what the dashboard rendered. Unknown fetch paths return
# ok:false, which jget() maps to null — the "no data yet" path.
HARNESS_PREFIX = """
const __els = {};
function __el(id){
  if(!__els[id]) __els[id] = { id, innerHTML:"", textContent:"", outerHTML:"",
    style:{}, onclick:null, setAttribute(){} };
  return __els[id];
}
globalThis.document = { getElementById: __el };
const __fixtures = __FIXTURES_JSON__;
globalThis.fetch = async (url) => {
  const path = String(url).split("?")[0];
  for (const key of Object.keys(__fixtures)) {
    if (path.endsWith(key)) return { ok:true, json: async () => __fixtures[key] };
  }
  return { ok:false };
};
"""

HARNESS_SUFFIX = """
setTimeout(() => {
  const out = {};
  for (const id of Object.keys(__els)) {
    const el = __els[id];
    out[id] = String(el.innerHTML || el.textContent || el.outerHTML || "");
  }
  process.stdout.write("__RESULT__" + JSON.stringify(out));
}, 150);
"""

ABLATION_FIXTURE = {
    "generated_at": "2026-07-19T05:00:00+00:00",
    "news_ablation": {
        "with_news": {
            "id": "momentum_weekly_churn_control__news_on",
            "validation_alpha_pct": 3.1, "total_return_pct": 12.5, "alpha_pct": 4.0,
            "walk_forward": {"n_folds": 3, "mean_validation_alpha_pct": 2.5,
                             "folds_positive": 3},
        },
        "without_news": {
            "id": "momentum_weekly_churn_control__news_off",
            "validation_alpha_pct": 1.9, "total_return_pct": 10.1, "alpha_pct": 1.6,
            "walk_forward": {"n_folds": 3, "mean_validation_alpha_pct": 1.2,
                             "folds_positive": 2},
        },
        "delta_validation_alpha_pct": 1.2,
        "neutral_band_pp": 0.5,
        "news_days_used": 14,
        "verdict": "news_helping",
        "note": "test fixture",
    },
}


@unittest.skipUnless(NODE, "node not installed")
class DashboardNewsAblationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        html = INDEX.read_text(encoding="utf-8")
        match = re.search(r"<script>(.*?)</script>", html, re.S)
        assert match, "docs/index.html must contain one inline <script> block"
        cls.script = match.group(1)

    def run_dashboard(self, fixtures: dict) -> dict:
        js = (HARNESS_PREFIX.replace("__FIXTURES_JSON__", json.dumps(fixtures))
              + self.script + HARNESS_SUFFIX)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(js)
            path = fh.name
        try:
            proc = subprocess.run([NODE, path], capture_output=True, text=True,
                                  timeout=60)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 0,
                         f"dashboard script crashed:\n{proc.stderr}")
        self.assertIn("__RESULT__", proc.stdout, proc.stderr)
        return json.loads(proc.stdout.split("__RESULT__", 1)[1])

    def test_news_ablation_section_renders_from_fixture(self) -> None:
        out = self.run_dashboard({"backtest/latest.json": ABLATION_FIXTURE})

        section = out.get("newsablation", "")
        self.assertIn("News is helping", section)
        self.assertIn("With news", section)
        self.assertIn("Without news", section)
        self.assertIn("+1.20%", section)          # the OOS alpha delta
        self.assertIn("14 replay days", section)
        # The rest of the dashboard took its graceful no-data paths.
        self.assertIn("No data yet", out.get("updated", ""))

    def test_insufficient_data_verdict_renders(self) -> None:
        fixture = json.loads(json.dumps(ABLATION_FIXTURE))
        fixture["news_ablation"].update({
            "verdict": "insufficient_data",
            "delta_validation_alpha_pct": None,
            "news_days_used": 0,
        })

        out = self.run_dashboard({"backtest/latest.json": fixture})

        self.assertIn("Not enough data yet", out.get("newsablation", ""))

    def test_backtest_without_ablation_shows_message(self) -> None:
        out = self.run_dashboard(
            {"backtest/latest.json": {"generated_at": "2026-07-19"}})

        self.assertIn("No news study", out.get("newsablation", ""))

    def test_completely_empty_datastore_does_not_crash(self) -> None:
        out = self.run_dashboard({})

        self.assertIn("No backtest yet", out.get("newsablation", ""))
        self.assertIn("No data yet", out.get("updated", ""))


if __name__ == "__main__":
    unittest.main()
