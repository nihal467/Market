#!/usr/bin/env python3
"""
Reviewer agent (independent) + auto-merge.

For each open auto-refinement PR that PASSED the security scan, an independent
reviewer persona re-derives the decision from scratch: it re-reads the issue,
the diff, the tests, and checks the change is small, evidence-backed, paper-only,
touches no protected files, and leaks no sensitive data. It submits a GitHub
review (APPROVE / REQUEST_CHANGES). If approved (and AUTO_MERGE=1), it merges.

Modes:
  --state state.json   pipeline: review every security-passed PR in state
  --pr 42              standalone: run the security scan inline, then review

Env: ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPOSITORY, REVIEWER_MODEL
     (or CLAUDE_MODEL), AUTO_MERGE (default 1), MERGE_METHOD (default squash).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap

import agent_lib as lib
import security_scan

MODEL = os.environ.get("REVIEWER_MODEL") or os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
AUTO_MERGE = os.environ.get("AUTO_MERGE", "1") == "1"
MERGE_METHOD = os.environ.get("MERGE_METHOD", "squash")

# Informational commit status. Not required by default (a required review check
# would deadlock human PRs that never get an AI review); opt in via
# setup_branch_protection.sh if you want it enforced for bot PRs.
REVIEW_CONTEXT = "review/approved"


def publish_review_status(approved: bool, summary: str) -> None:
    sha = lib.run_git(["rev-parse", "HEAD"]).stdout.strip()
    if not sha:
        return
    try:
        lib.set_commit_status(sha, "success" if approved else "failure",
                              REVIEW_CONTEXT, summary)
    except RuntimeError as e:
        print(f"review status publish failed (non-fatal): {e}")

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an INDEPENDENT, skeptical senior reviewer for a personal PAPER-trading
    system. A proposer agent has opened a PR from a daily refinement issue. Do
    NOT assume it is correct — re-derive the decision yourself.

    Approve ONLY if ALL hold:
      1. The change is genuinely justified by evidence in the issue (paper P&L,
         incubation gate, backtest lab, regime). Vague or speculative → reject.
      2. It is small, paper-only, and does not add real-money trading or weaken
         the one-month incubation / readiness gate.
      3. It touches no protected files (.github/, holdings.yaml, secrets) and
         leaks NO sensitive data (rupee amounts, holdings figures, keys, PII).
      4. Tests pass and the code is sound. Verify by reading files and running
         run_command (pytest / py_compile). Do not trust claims — check.
      5. It stays within a small budget and does what its summary says.

    If anything is unclear, unjustified, unsafe, or unverified, REQUEST_CHANGES
    with specific reasons. Prefer rejecting a weak change over approving it.
    Finish by calling submit_review exactly once.
    """)

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit your review decision exactly once.",
    "input_schema": {"type": "object", "properties": {
        "decision": {"type": "string", "enum": ["approve", "request_changes"]},
        "summary": {"type": "string", "description": "1-3 sentence verdict."},
        "concerns": {"type": "string", "description": "Specific issues, or 'none'."},
    }, "required": ["decision", "summary", "concerns"]},
}


def checkout_pr_branch(branch: str) -> None:
    lib.revert_all()
    lib.run_git(["fetch", "origin", branch], check=False)
    lib.run_git(["fetch", "origin", "main"], check=False)
    lib.run_git(["checkout", "-B", branch, f"origin/{branch}"], check=False)


def branch_validation(base: str = "main") -> str:
    """Compile changed-vs-base .py files and run pytest on the branch."""
    log = []
    changed = lib.run_git(["diff", "--name-only", f"origin/{base}...HEAD"]).stdout.split()
    py = [f for f in changed if f.endswith(".py")]
    if py:
        r = subprocess.run([sys.executable, "-m", "py_compile", *py],
                           cwd=lib.REPO_ROOT, capture_output=True, text=True)
        log.append(f"py_compile exit={r.returncode} {r.stderr.strip()}")
    if (lib.REPO_ROOT / "tests").is_dir():
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_*.py"],
            cwd=lib.REPO_ROOT, capture_output=True, text=True)
        log.append(f"unittest exit={r.returncode}\n{((r.stdout or '')+(r.stderr or ''))[-2000:]}")
    return "\n".join(log) or "no python changes / no tests"


def review_first_message(pr: dict, diff: str, val: str, sec_report: str) -> str:
    issue = lib.gh_request("GET", f"/repos/{lib.REPO}/issues/{pr['issue']}")
    return textwrap.dedent(f"""\
        # Original refinement issue #{pr['issue']}: {issue.get('title','')}

        {issue.get('body') or '(no body)'}

        # Security scan result (already run)
        {sec_report}

        # Automated validation on the PR branch
        {val}

        # PR #{pr['number']} diff
        ```diff
        {diff[:15000]}
        ```

        You may read any file or run pytest/py_compile via tools to verify.
        Re-derive the decision independently, then call submit_review.
        """)


def merge_if_ok(pr: dict) -> None:
    if not AUTO_MERGE:
        print(f"PR #{pr['number']} approved; AUTO_MERGE off — leaving open.")
        pr["status"] = "approved"
        return
    try:
        lib.merge_pull(pr["number"], MERGE_METHOD)
        lib.delete_branch(pr["branch"])
        pr["status"] = "merged"
        print(f"PR #{pr['number']} merged.")
    except RuntimeError as e:
        pr["status"] = "merge_failed"
        lib.comment_issue(pr["issue"], f"🤖 Review approved but merge failed: `{e}`.")
        print(f"merge failed: {e}")


def review_pr(client, pr: dict) -> None:
    n = pr["number"]
    checkout_pr_branch(pr["branch"])
    diff = lib.run_git(["diff", "origin/main...HEAD"]).stdout
    val = branch_validation()
    sec_report = pr.get("security_report", "(scan run separately)")

    result = lib.run_agent_loop(
        client, MODEL, SYSTEM_PROMPT,
        review_first_message(pr, diff, val, sec_report),
        tools=lib.READ_TOOLS + [REVIEW_TOOL],
        dispatch=lib.build_dispatch(allow_write=False),
        finish_tool="submit_review")

    if not result:
        lib.submit_review(n, "REQUEST_CHANGES",
                          "🤖 Reviewer reached no conclusion; blocking merge.")
        pr["status"] = "changes_requested"
        return

    body = (f"🤖 **Independent reviewer**\n\n**Verdict:** {result['summary']}\n\n"
            f"**Concerns:** {result['concerns']}")
    approved = result["decision"] == "approve"
    publish_review_status(approved, result["summary"])
    if approved:
        lib.submit_review(n, "APPROVE", body)
        merge_if_ok(pr)
    else:
        lib.submit_review(n, "REQUEST_CHANGES", body)
        pr["status"] = "changes_requested"
        print(f"PR #{n}: changes requested")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--state")
    g.add_argument("--pr", type=int)
    args = ap.parse_args()

    lib.require_env("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITHUB_REPOSITORY")
    client = lib.make_client()

    if args.pr:
        prd = lib.get_pull(args.pr)
        pr = {"number": args.pr, "branch": prd["head"]["ref"],
              "issue": _issue_from_body(prd.get("body", "")), "status": "opened"}
        ok, rep = security_scan.scan_pr(args.pr)
        pr["security_report"] = rep
        if not ok:
            lib.submit_review(args.pr, "REQUEST_CHANGES", rep)
            print("security blocked; not merging")
            return
        review_pr(client, pr)
        return

    state = json.load(open(args.state, encoding="utf-8"))
    for pr in state.get("prs", []):
        if pr.get("status") == "opened" and pr.get("security") == "pass":
            try:
                review_pr(client, pr)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR reviewing #{pr['number']}: {e}")
    json.dump(state, open(args.state, "w", encoding="utf-8"), indent=2)


def _issue_from_body(body: str) -> int:
    import re
    m = re.search(r"Closes #(\d+)", body or "")
    return int(m.group(1)) if m else 0


if __name__ == "__main__":
    main()
