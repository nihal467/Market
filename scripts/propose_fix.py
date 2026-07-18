#!/usr/bin/env python3
"""
Proposer agent: for each open `daily-refinement` issue, decide whether the
day's evidence justifies a small, paper-only change. If yes, make it on a new
branch and open a PR (Closes #<issue>). If no, comment and close the issue.

Never pushes to main directly. Writes a pipeline state file describing the PRs
it opened, for the security + review steps that follow.

Env: ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPOSITORY, PROPOSER_MODEL
     (or CLAUDE_MODEL), ISSUE_LABEL, MAX_ISSUES.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import textwrap

import agent_lib as lib

MODEL = os.environ.get("PROPOSER_MODEL") or os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
MAX_ISSUES = int(os.environ.get("MAX_ISSUES", "3"))

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a careful senior engineer doing a DAILY post-market refinement of a
    personal PAPER-trading (dummy money) system. You are NOT a "fix everything"
    bot — your job is disciplined restraint.

    Hard rules (a violation is a failure):
      1. Evidence-backed only. Cite specific data from the issue (paper P&L,
         incubation gate, backtest lab, signal counts, regime). If the data
         does not clearly justify a change, make NO change and report
         changed=false. "No change" is the correct answer on most days.
      2. Keep any change SMALL, testable, PAPER-ONLY. Do not add strategy
         parameters at random. Smallest edit that addresses a repeated/material
         failure pattern.
      3. Preserve the one-month incubation rule and readiness gate. Never
         enable/recommend real-money trading. Never touch secrets, workflows
         (.github/), or holdings.yaml.
      4. NEVER put personal/sensitive data in code: no rupee amounts, no
         holdings figures (units/invested/quantity), no secrets/keys.
      5. Do not break tests. After editing, run pytest / py_compile via
         run_command before reporting.
      6. Stay within a small budget: a few files, a few hundred lines. If a
         proper fix would be bigger, report changed=false and describe it.

    Explore first (list_dir/read_file), decide, make the minimal edit(s) with
    write_file, validate with run_command, then call report_result exactly once.
    """)

REPORT_TOOL = {
    "name": "report_result",
    "description": ("Finish. changed=true only if you edited files with an "
                    "evidence-backed, paper-only improvement; else changed=false."),
    "input_schema": {"type": "object", "properties": {
        "changed": {"type": "boolean"},
        "summary": {"type": "string"},
        "evidence": {"type": "string"},
        "validation": {"type": "string"},
    }, "required": ["changed", "summary", "evidence", "validation"]},
}


def first_message(issue: dict) -> str:
    return textwrap.dedent(f"""\
        # Repository map
        {lib.repo_map()}

        # Refinement issue #{issue['number']}: {issue['title']}

        {issue.get('body') or '(no body)'}

        Begin. No change is the right answer unless the evidence clearly
        justifies a small, safe, paper-only edit. Finish with report_result.
        """)


def pr_body(issue_num: int, r: dict) -> str:
    return textwrap.dedent(f"""\
        Automated post-market refinement for #{issue_num}.

        **Summary:** {r.get('summary','').strip()}

        **Evidence:** {r.get('evidence','').strip()}

        **Validation & gate effect:** {r.get('validation','').strip()}

        ---
        Proposed by the Claude proposer agent. Subject to the security scan and
        an independent review before merge.

        Closes #{issue_num}
        """)


def process_issue(client, issue: dict) -> dict | None:
    n = issue["number"]
    date = datetime.date.today().isoformat()
    branch = f"auto-refine/issue-{n}-{date}"
    print(f"\n=== Issue #{n}: {issue['title']} ===")

    lib.revert_all()
    lib.run_git(["fetch", "origin", "main"], check=False)
    lib.checkout_new_branch(branch, "main")

    result = lib.run_agent_loop(
        client, MODEL, SYSTEM_PROMPT, first_message(issue),
        tools=lib.READ_TOOLS + [lib.WRITE_TOOL, REPORT_TOOL],
        dispatch=lib.build_dispatch(allow_write=True),
        finish_tool="report_result")

    if not result or not result.get("changed") or not lib.working_tree_dirty():
        lib.revert_all()
        lib.comment_issue(n,
            "🤖 **Proposer — no change today.** Evidence did not justify a code "
            f"change.\n\n{(result or {}).get('evidence') or 'No conclusion reached.'}")
        lib.close_issue(n)
        print("no change; closed")
        return None

    files, lines = lib.diff_stats()
    if files > lib.MAX_CHANGED_FILES or lines > lib.MAX_CHANGED_LINES:
        lib.revert_all()
        lib.comment_issue(n, f"🤖 **Proposer — change too large** ({files} files / "
                             f"{lines} lines > budget); left open.\n\n{result['summary']}")
        print("too large; discarded")
        return None

    ok, val_log = lib.validate_changes()
    if not ok:
        lib.revert_all()
        lib.comment_issue(n, "🤖 **Proposer — validation failed; nothing opened.**\n\n"
                             f"```\n{val_log[:3500]}\n```")
        print("validation failed; discarded")
        return None

    if not lib.commit_all(f"fix(paper-trading): refine per #{n}\n\n{result['summary']}"):
        lib.revert_all()
        return None
    if not lib.push_branch(branch):
        lib.revert_all()
        lib.comment_issue(n, "🤖 **Proposer — push failed; nothing opened.**")
        return None

    pr = lib.create_pull(title=f"Refine paper-trading (#{n}): {result['summary'][:60]}",
                         head=branch, base="main", body=pr_body(n, result))
    lib.add_labels(pr["number"], [lib.PR_LABEL])
    lib.comment_issue(n, f"🤖 **Proposer opened #{pr['number']}** for review.")
    print(f"opened PR #{pr['number']}")
    return {"number": pr["number"], "branch": branch, "issue": n,
            "status": "opened", "summary": result["summary"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="pipeline_state.json")
    args = ap.parse_args()

    lib.require_env("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITHUB_REPOSITORY")
    client = lib.make_client()
    issues = lib.list_target_issues()
    if not issues:
        print(f"No open '{lib.ISSUE_LABEL}' issues.")
        json.dump({"prs": []}, open(args.state, "w"))
        return

    prs = []
    for issue in issues[:MAX_ISSUES]:
        try:
            pr = process_issue(client, issue)
            if pr:
                prs.append(pr)
        except Exception as e:  # noqa: BLE001
            lib.revert_all()
            try:
                lib.comment_issue(issue["number"],
                                  f"🤖 Proposer errored: `{e}`. Nothing opened.")
            except Exception:
                pass
            print(f"ERROR on #{issue['number']}: {e}")
    json.dump({"prs": prs}, open(args.state, "w"), indent=2)
    print(f"\nOpened {len(prs)} PR(s).")


if __name__ == "__main__":
    main()
