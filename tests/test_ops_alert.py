from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ops_alert  # noqa: E402


class FakeGh:
    """Records gh_request calls and serves canned open-issue listings."""

    def __init__(self, open_issues: list[dict]):
        self.open_issues = open_issues
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, payload: dict | None = None,
                 accept: str = "application/vnd.github+json"):
        self.calls.append((method, path, payload))
        if method == "GET" and "/issues?" in path:
            return self.open_issues
        if method == "POST" and path.endswith("/labels"):
            return {}
        if method == "POST" and path.endswith("/issues"):
            return {"number": 42, "title": (payload or {}).get("title")}
        if method == "POST" and "/comments" in path:
            return {"id": 1}
        raise AssertionError(f"unexpected gh_request: {method} {path}")


class OpsAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_gh = ops_alert.lib.gh_request

    def tearDown(self) -> None:
        ops_alert.lib.gh_request = self.old_gh

    def test_creates_new_issue_when_no_open_alert_exists(self) -> None:
        fake = FakeGh(open_issues=[])
        ops_alert.lib.gh_request = fake

        result = ops_alert.alert("Daily Analysis", "run failed: http://example")

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["number"], 42)
        created = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/issues")]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2]["title"], "Ops alert: Daily Analysis")
        self.assertIn("ops-alert", created[0][2]["labels"])

    def test_comments_on_existing_open_alert_instead_of_duplicating(self) -> None:
        fake = FakeGh(open_issues=[
            {"number": 7, "title": "Ops alert: Daily Analysis"},
            {"number": 8, "title": "Ops alert: Weekly Watchlist"},
        ])
        ops_alert.lib.gh_request = fake

        result = ops_alert.alert("Daily Analysis", "failed again")

        self.assertEqual(result["action"], "commented")
        self.assertEqual(result["number"], 7)
        comments = [c for c in fake.calls if "/comments" in c[1]]
        self.assertEqual(len(comments), 1)
        self.assertIn("/issues/7/comments", comments[0][1])
        self.assertIn("failed again", comments[0][2]["body"])
        # No new issue was opened.
        self.assertFalse(
            [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/issues")])

    def test_pull_requests_and_other_titles_do_not_match(self) -> None:
        fake = FakeGh(open_issues=[
            {"number": 5, "title": "Ops alert: Daily Analysis",
             "pull_request": {"url": "x"}},   # a PR must never count
        ])
        ops_alert.lib.gh_request = fake

        result = ops_alert.alert("Daily Analysis", "detail")

        self.assertEqual(result["action"], "created")


if __name__ == "__main__":
    unittest.main()
