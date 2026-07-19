#!/usr/bin/env python3
"""
Security / sensitive-data scanner for the Market repo.

Scans a pull-request diff (added lines only) for leaked secrets, credentials,
and repo-specific private data (portfolio rupee amounts, holdings figures,
1Password refs, protected-file edits). Any HIGH finding fails the check.

This repo is PUBLIC, so the biggest risk is committing personal financial
figures (amounts, units, invested) or a real secret. This is the hard gate:
nothing merges if it trips.

Usage:
  python scripts/security_scan.py --pr 42            # scan one PR (via API)
  python scripts/security_scan.py --state state.json # pipeline mode
  python scripts/security_scan.py --diff-file d.diff # scan a local diff (tests)

Exit code is non-zero when a blocking (HIGH) finding is present in --pr /
--diff-file mode, so it can gate a required check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import agent_lib as lib

FAIL_ON = os.environ.get("FAIL_ON", "HIGH").upper()  # HIGH (default) or MED

# Commit-status context used as the required check on `main`. Published by the
# scanner for BOTH bot PRs (from the pipeline) and human PRs (from the
# pull_request workflow), so branch protection can require this one context.
SECURITY_CONTEXT = "security/sensitive-data"

# --------------------------------------------------------------------------- #
# Detection rules
# --------------------------------------------------------------------------- #
# Tokens that, if present on the line, mean a "secret-looking assignment" is
# actually a safe *reference* (env var / GH secret / placeholder), not a value.
SAFE_REF_TOKENS = (
    "os.environ", "getenv", "environ[", "${{", "secrets.", "vars.",
    "op://", "process.env", "<", "xxxx", "example", "changeme", "your_",
    "***", "redacted", "placeholder", "dummy",
)

# (name, severity, compiled_regex)
VALUE_RULES = [
    ("anthropic_api_key", "HIGH", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_api_key", "HIGH", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("github_pat_classic", "HIGH", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_pat_fine", "HIGH", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("github_token", "HIGH", re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{36}\b")),
    ("aws_access_key", "HIGH", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_key", "HIGH",
     re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}")),
    ("google_api_key", "HIGH", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", "HIGH", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key_block", "HIGH", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", "HIGH",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}")),
    # PII (India)
    ("pan_number", "HIGH", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
]

# Secret-looking assignment: key = "literal". Exempted by SAFE_REF_TOKENS.
ASSIGN_RULE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|client[_-]?secret|"
    r"dashboard_password)\b\s*[:=]\s*['\"][^'\"]{6,}['\"]")

# Repo-specific private financial data.
RUPEE_RULE = re.compile(r"₹\s?\d{4,}")                     # portfolio-scale amounts
INR_NUM_RULE = re.compile(r"\b\d{4,}(?:\.\d+)?\s*(?:INR|rupees)\b", re.I)
HOLDINGS_RULE = re.compile(
    r"(?i)\b(invested|units|quantity|avg_price|buy_price|cost_basis)\s*[:=]\s*[\d.]+")
AADHAAR_RULE = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")

# Files that must never appear in an automated refinement PR.
PROTECTED_FILE_RULES = [
    ("edits_holdings", "HIGH", re.compile(r"(^|/)holdings\.ya?ml$")),
    ("edits_workflow", "HIGH", re.compile(r"^\.github/workflows/.+")),
    ("edits_scanner", "HIGH",
     re.compile(r"^scripts/(security_scan|agent_lib|ops_alert|ensure_labels)\.py$")),
    ("dotenv_file", "HIGH", re.compile(r"(^|/)\.env(\.|$)")),
    ("key_file", "HIGH", re.compile(r"\.(pem|key|p12|pfx)$|(^|/)id_rsa$")),
]

# Rules downgraded to advisory (MED) when the PR author is the repo owner:
# the owner can already push to main directly, so blocking their maintenance
# PRs adds friction, not security. Bot/stranger PRs stay hard-blocked.
OWNER_ADVISORY_RULES = {"edits_workflow", "edits_scanner"}


def _mask(s: str) -> str:
    s = s.strip()
    if len(s) <= 8:
        return s[0] + "***" if s else "***"
    return f"{s[:4]}…{s[-2:]} ({len(s)} chars)"


def _safe_ref(line: str) -> bool:
    low = line.lower()
    return any(tok in low for tok in SAFE_REF_TOKENS)


# --------------------------------------------------------------------------- #
# Diff parsing
# --------------------------------------------------------------------------- #
def parse_diff(diff: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (added_lines[(file, text)], changed_files)."""
    added: list[tuple[str, str]] = []
    files: list[str] = []
    cur = "?"
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            path = ln[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            cur = path
            if path not in files and path != "/dev/null":
                files.append(path)
        elif ln.startswith("diff --git"):
            m = re.search(r" b/(\S+)$", ln)
            if m and m.group(1) not in files:
                files.append(m.group(1))
        elif ln.startswith("+") and not ln.startswith("+++"):
            added.append((cur, ln[1:]))
    return added, files


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
def scan_diff(diff: str) -> list[dict]:
    added, files = parse_diff(diff)
    findings: list[dict] = []

    def add(sev, rule, path, snippet):
        findings.append({"severity": sev, "rule": rule, "file": path,
                         "snippet": snippet})

    # File-level rules
    for f in files:
        for rule, sev, rx in PROTECTED_FILE_RULES:
            if rx.search(f):
                add(sev, rule, f, "(protected file added/modified)")

    # Line-level rules
    for path, text in added:
        is_doc = path.endswith((".md", ".rst", ".txt"))
        for name, sev, rx in VALUE_RULES:
            m = rx.search(text)
            if m:
                add(sev, name, path, _mask(m.group(0)))
        if ASSIGN_RULE.search(text) and not _safe_ref(text):
            add("HIGH", "hardcoded_secret_assignment", path, _mask(text))
        if RUPEE_RULE.search(text) or INR_NUM_RULE.search(text):
            add("MED" if is_doc else "HIGH", "portfolio_amount", path, _mask(text))
        if HOLDINGS_RULE.search(text):
            add("HIGH", "holdings_figure", path, _mask(text))
        if AADHAAR_RULE.search(text) and not re.search(r"\.\d", text):
            add("MED", "possible_aadhaar", path, _mask(text))
    return findings


def is_blocking(findings: list[dict]) -> bool:
    order = {"HIGH": 2, "MED": 1, "LOW": 0}
    threshold = order.get(FAIL_ON, 2)
    return any(order.get(f["severity"], 0) >= threshold for f in findings)


def report(findings: list[dict]) -> str:
    if not findings:
        return "✅ **Security scan clean** — no secrets or sensitive data in the diff."
    lines = ["🔒 **Security scan findings**\n",
             "| Severity | Rule | File | Match |",
             "|---|---|---|---|"]
    for f in sorted(findings, key=lambda x: x["severity"]):
        snip = f["snippet"].replace("|", "\\|")
        lines.append(f"| {f['severity']} | `{f['rule']}` | `{f['file']}` | `{snip}` |")
    verdict = ("\n**Result: BLOCKED** — HIGH-severity findings must be resolved "
               "before merge." if is_blocking(findings) else
               "\n**Result: warnings only** — no blocking findings.")
    return "\n".join(lines) + "\n" + verdict


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def scan_pr(number: int) -> tuple[bool, str]:
    """Scan a PR, publish the required commit status on its head sha."""
    pull = lib.get_pull(number)
    sha = pull.get("head", {}).get("sha")
    findings = scan_diff(lib.pr_diff_via_api(number))
    # Owner-authored PRs: protected-infra edits become advisory, not blocking.
    author = (pull.get("user") or {}).get("login", "")
    owner = lib.REPO.split("/")[0] if "/" in lib.REPO else ""
    if author and author == owner:
        for f in findings:
            if f["rule"] in OWNER_ADVISORY_RULES and f["severity"] == "HIGH":
                f["severity"] = "MED"
                f["snippet"] += " — owner-authored PR, advisory only"
    ok = not is_blocking(findings)
    rep = report(findings)
    if sha:
        n_high = sum(1 for f in findings if f["severity"] == "HIGH")
        desc = "No sensitive data detected" if ok else f"{n_high} HIGH finding(s)"
        try:
            lib.set_commit_status(sha, "success" if ok else "failure",
                                  SECURITY_CONTEXT, desc)
        except RuntimeError as e:
            print(f"status publish failed (non-fatal): {e}")
    return ok, rep


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int)
    g.add_argument("--state")
    g.add_argument("--diff-file")
    args = ap.parse_args()

    if args.diff_file:
        diff = open(args.diff_file, encoding="utf-8").read()
        findings = scan_diff(diff)
        print(report(findings))
        sys.exit(1 if is_blocking(findings) else 0)

    lib.require_env("GITHUB_TOKEN", "GITHUB_REPOSITORY")

    if args.pr:
        ok, rep = scan_pr(args.pr)
        print(rep)
        if not ok:
            lib.submit_review(args.pr, "REQUEST_CHANGES", rep)
        sys.exit(0 if ok else 1)

    # pipeline mode
    state = json.load(open(args.state, encoding="utf-8"))
    for pr in state.get("prs", []):
        if pr.get("status") != "opened":
            continue
        ok, rep = scan_pr(pr["number"])
        pr["security"] = "pass" if ok else "fail"
        pr["security_report"] = rep
        if not ok:
            lib.submit_review(pr["number"], "REQUEST_CHANGES", rep)
            lib.add_labels(pr["number"], ["security-failed"])
            print(f"PR #{pr['number']}: security FAIL")
        else:
            print(f"PR #{pr['number']}: security pass")
    json.dump(state, open(args.state, "w", encoding="utf-8"), indent=2)


if __name__ == "__main__":
    main()
