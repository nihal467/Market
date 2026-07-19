#!/usr/bin/env python3
"""Monthly meta-review issue: did the self-refinement loop actually help?

Runs on the 1st of each month (10:00 IST). Gathers, for the PREVIOUS calendar
month:
  - merged PRs labeled `auto-refinement` (the daily refinement pipeline's output)
  - `daily-refinement` issues closed in the month
  - the paper book's performance for the month (from the data branch)

and opens ONE issue titled "Meta-review: YYYY-MM" (skipped if it already
exists) with a checklist for the human: the point is to audit the automated
changes with hindsight, not to celebrate them. stdlib only + agent_lib.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta, timezone
from datetime import datetime as _datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_lib as lib  # noqa: E402

META_LABEL = "meta-review"
RAW_BASE = "https://raw.githubusercontent.com"

IST = timezone(timedelta(hours=5, minutes=30))


def _repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "") or lib.REPO


def previous_month(today: date) -> tuple[str, date, date]:
    """(label 'YYYY-MM', first day, last day) of the month before `today`."""
    first_of_this = today.replace(day=1)
    last_prev = first_of_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return f"{first_prev.year:04d}-{first_prev.month:02d}", first_prev, last_prev


def issue_title(month_label: str) -> str:
    return f"Meta-review: {month_label}"


def find_existing_issue(title: str) -> int | None:
    """Number of an issue (any state) with EXACTLY this title, else None."""
    query = urllib.parse.quote(f'repo:{_repo()} in:title "{title}"')
    result = lib.gh_request("GET", f"/search/issues?q={query}&per_page=50")
    for item in (result or {}).get("items", []):
        if item.get("title") == title and "pull_request" not in item:
            return item.get("number")
    return None


def merged_refinement_prs(start: date, end: date) -> list[dict]:
    query = urllib.parse.quote(
        f"repo:{_repo()} is:pr label:{lib.PR_LABEL} "
        f"merged:{start.isoformat()}..{end.isoformat()}")
    result = lib.gh_request("GET", f"/search/issues?q={query}&per_page=100")
    prs = []
    for item in (result or {}).get("items", []):
        merged_at = (item.get("pull_request") or {}).get("merged_at") \
            or item.get("closed_at") or ""
        prs.append({
            "number": item.get("number"),
            "title": item.get("title") or "",
            "merged_at": merged_at[:10],
        })
    prs.sort(key=lambda p: (p["merged_at"], p["number"] or 0))
    return prs


def closed_refinement_issues(start: date, end: date) -> list[dict]:
    query = urllib.parse.quote(
        f"repo:{_repo()} is:issue label:{lib.ISSUE_LABEL} "
        f"closed:{start.isoformat()}..{end.isoformat()}")
    result = lib.gh_request("GET", f"/search/issues?q={query}&per_page=100")
    return [{"number": i.get("number"), "title": i.get("title") or ""}
            for i in (result or {}).get("items", [])]


def fetch_paper_latest() -> dict | None:
    """paper/latest.json from the data branch; None when absent (404 etc.)."""
    url = f"{RAW_BASE}/{_repo()}/data/paper/latest.json"
    req = urllib.request.Request(url, headers={"User-Agent": "market-meta-review"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"paper data unavailable ({exc}) — continuing without it")
        return None


def month_performance(paper: dict | None, start: date, end: date) -> dict:
    """Paper P&L / alpha over the month, from the recorded history rows.

    Anchors to the last row BEFORE the month when one exists so the month
    return is a true window return, not since-inception.
    """
    if not paper:
        return {"available": False}
    rows = sorted(
        (r for r in (paper.get("history") or [])
         if r.get("date") and r.get("value")),
        key=lambda r: r["date"])
    in_month = [r for r in rows
                if start.isoformat() <= r["date"] <= end.isoformat()]
    if not in_month:
        return {"available": False}
    before = [r for r in rows if r["date"] < start.isoformat()]
    anchor = before[-1] if before else in_month[0]
    last = in_month[-1]
    ret = round((last["value"] / anchor["value"] - 1) * 100, 2) \
        if anchor["value"] else None
    b0, b1 = anchor.get("benchmark_value"), last.get("benchmark_value")
    bench = round((b1 / b0 - 1) * 100, 2) if b0 and b1 else None
    return {
        "available": True,
        "trading_days": len(in_month),
        "month_return_pct": ret,
        "month_benchmark_return_pct": bench,
        "month_alpha_pct": round(ret - bench, 2)
        if ret is not None and bench is not None else None,
        "end_total_pnl_pct": last.get("total_pnl_pct"),
        "end_alpha_pct": last.get("alpha_pct"),
    }


def build_body(month_label: str, prs: list[dict], issues: list[dict],
               perf: dict) -> str:
    lines = [
        f"Automated monthly meta-review for **{month_label}** — audit the "
        "self-refinement loop with hindsight. Paper trading only; virtual "
        "book, no real money.",
        "",
        "## Merged auto-refinement PRs",
    ]
    if prs:
        lines += ["| PR | Title | Merged |", "| --- | --- | --- |"]
        lines += [f"| #{p['number']} | {p['title']} | {p['merged_at'] or '?'} |"
                  for p in prs]
    else:
        lines.append("_None merged this month._")
    lines += ["", "## Refinement issues closed",
              f"{len(issues)} `daily-refinement` issue(s) closed this month."
              if issues else "_No refinement issues closed this month._"]
    lines += ["", "## Paper performance for the month"]
    if perf.get("available"):
        lines += [
            f"- Month return: {perf['month_return_pct']}% over "
            f"{perf['trading_days']} trading day(s)",
            f"- NIFTY 50 over the same window: {perf['month_benchmark_return_pct']}%",
            f"- Month alpha: {perf['month_alpha_pct']}%",
            f"- Since inception at month end: total P&L "
            f"{perf['end_total_pnl_pct']}%, alpha {perf['end_alpha_pct']}%",
        ]
    else:
        lines.append("_No paper data for this month (data branch empty or "
                     "history does not cover it)._")
    lines += [
        "",
        "## Human checklist",
        "- [ ] Would I have made these changes without seeing the results?",
        "- [ ] Any parameter drift (thresholds/knobs creeping toward whatever "
        "recently worked)?",
        "- [ ] Revert anything?",
        "- [ ] Is the incubation/readiness gate still honest?",
    ]
    return "\n".join(lines)


def run() -> dict:
    lib.require_env("GITHUB_TOKEN", "GITHUB_REPOSITORY")
    today = _datetime.now(IST).date()
    month_label, start, end = previous_month(today)
    title = issue_title(month_label)

    existing = find_existing_issue(title)
    if existing is not None:
        print(f"Issue '{title}' already exists: #{existing} — nothing to do.")
        return {"skipped": True, "reason": "already_exists", "number": existing}

    prs = merged_refinement_prs(start, end)
    issues = closed_refinement_issues(start, end)
    perf = month_performance(fetch_paper_latest(), start, end)
    body = build_body(month_label, prs, issues, perf)

    lib.ensure_label(META_LABEL, "5319e7", "Monthly audit of the auto-refinement loop")
    created = lib.gh_request(
        "POST", f"/repos/{_repo()}/issues",
        {"title": title, "body": body, "labels": [META_LABEL]})
    print(f"Created meta-review issue #{created.get('number')}: {title} "
          f"({len(prs)} merged PRs, {len(issues)} closed issues)")
    return {"skipped": False, "number": created.get("number")}


if __name__ == "__main__":
    run()
