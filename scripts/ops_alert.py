#!/usr/bin/env python3
"""Failure alert helper: one open GitHub issue per alert source.

Usage:
    python scripts/ops_alert.py --source <name> --detail <text>

Creates an open issue titled "Ops alert: <source>" labeled `ops-alert`.
If an OPEN issue with that exact title already exists, a timestamped comment
is added instead of a duplicate issue — repeated failures of the same
workflow pile up in one place. stdlib only + agent_lib.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_lib as lib  # noqa: E402

ALERT_LABEL = "ops-alert"
ALERT_COLOR = "b60205"  # red


def _repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "") or lib.REPO


def alert_title(source: str) -> str:
    return f"Ops alert: {source}"


def find_open_alert(title: str) -> int | None:
    """Number of an OPEN issue with exactly this title, else None."""
    issues = lib.gh_request(
        "GET",
        f"/repos/{_repo()}/issues?state=open&labels={ALERT_LABEL}&per_page=100")
    for issue in issues or []:
        if "pull_request" in issue:
            continue
        if issue.get("title") == title:
            return issue.get("number")
    return None


def alert(source: str, detail: str) -> dict:
    """Create the alert issue, or comment on the existing open one."""
    lib.ensure_label(ALERT_LABEL, ALERT_COLOR, "Automated workflow failure alert")
    title = alert_title(source)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message = f"**{stamp}** — {detail}"
    number = find_open_alert(title)
    if number is not None:
        lib.gh_request("POST", f"/repos/{_repo()}/issues/{number}/comments",
                       {"body": message})
        print(f"Updated existing ops alert #{number} ({title})")
        return {"action": "commented", "number": number}
    created = lib.gh_request(
        "POST", f"/repos/{_repo()}/issues",
        {"title": title,
         "body": (f"Automated alert for source `{source}`.\n\n{message}\n\n"
                  "Close this issue once the underlying problem is fixed; "
                  "repeat failures will re-open the conversation as comments "
                  "on a fresh issue."),
         "labels": [ALERT_LABEL]})
    print(f"Created ops alert #{created.get('number')} ({title})")
    return {"action": "created", "number": created.get("number")}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create/update an ops alert issue.")
    parser.add_argument("--source", required=True, help="Alert source (e.g. workflow name)")
    parser.add_argument("--detail", required=True, help="Detail text (e.g. run URL)")
    args = parser.parse_args()
    lib.require_env("GITHUB_TOKEN", "GITHUB_REPOSITORY")
    alert(args.source, args.detail)


if __name__ == "__main__":
    main()
