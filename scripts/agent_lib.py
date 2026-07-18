#!/usr/bin/env python3
"""
Shared library for the Market daily-refinement PR pipeline.

Used by:
  propose_fix.py    - proposer agent: opens a PR per daily-refinement issue
  security_scan.py  - scans a PR diff for leaked secrets / sensitive data
  review_pr.py      - reviewer agent: approves/blocks, then auto-merges

Design goals: no third-party deps except the `anthropic` SDK; everything else
is stdlib + git. All file writes and shell commands the agents run are scoped
and guarded here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import urllib.request
import urllib.error

# --------------------------------------------------------------------------- #
# Config / constants
# --------------------------------------------------------------------------- #
REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_API = "https://api.github.com"

REPO_ROOT = Path(__file__).resolve().parent.parent

ISSUE_LABEL = os.environ.get("ISSUE_LABEL", "daily-refinement")
PR_LABEL = os.environ.get("PR_LABEL", "auto-refinement")

# Agent budgets / safety
MAX_TOOL_CALLS = 40
MAX_CHANGED_LINES = 400
MAX_CHANGED_FILES = 8
MAX_FILE_BYTES = 200_000

# Paths the proposer may never modify.
PROTECTED_PREFIXES = (".git/", ".github/", "holdings.yaml")
PROTECTED_SUFFIXES = (".env",)

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
             "data", "data_out", ".pytest_cache", ".mypy_cache"}

STAGE_EXCLUDES = [
    ":(exclude)**/__pycache__/**", ":(exclude)**/*.pyc",
    ":(exclude).pytest_cache/**", ":(exclude)data/**", ":(exclude)data_out/**",
]

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


# --------------------------------------------------------------------------- #
# GitHub REST (stdlib)
# --------------------------------------------------------------------------- #
def gh_request(method: str, path: str, payload: dict | None = None,
               accept: str = "application/vnd.github+json") -> dict | list | str:
    url = path if path.startswith("http") else f"{GH_API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "market-refinement-bot")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
            if accept.endswith("diff") or accept.endswith("patch"):
                return body
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub {method} {path} -> {e.code}: {e.read().decode()}")


def list_target_issues() -> list[dict]:
    issues = gh_request(
        "GET", f"/repos/{REPO}/issues?state=open&labels={ISSUE_LABEL}&per_page=50")
    out = [i for i in issues if "pull_request" not in i]
    out.sort(key=lambda i: i["number"])
    return out


def comment_issue(number: int, body: str) -> None:
    gh_request("POST", f"/repos/{REPO}/issues/{number}/comments", {"body": body})


def close_issue(number: int) -> None:
    gh_request("PATCH", f"/repos/{REPO}/issues/{number}", {"state": "closed"})


def ensure_label(name: str, color: str = "ededed", description: str = "") -> None:
    """Create a label if it doesn't already exist (idempotent, non-fatal)."""
    try:
        gh_request("POST", f"/repos/{REPO}/labels",
                   {"name": name, "color": color, "description": description})
    except RuntimeError as e:
        if "already_exists" not in str(e) and "422" not in str(e):
            print(f"ensure_label '{name}' failed (non-fatal): {e}")


def add_labels(number: int, labels: list[str]) -> None:
    for name in labels:
        ensure_label(name)
    try:
        gh_request("POST", f"/repos/{REPO}/issues/{number}/labels", {"labels": labels})
    except RuntimeError as e:
        print(f"label add failed (non-fatal): {e}")


def set_commit_status(sha: str, state: str, context: str, description: str = "") -> None:
    """Publish a commit status (state: success|failure|pending|error)."""
    gh_request("POST", f"/repos/{REPO}/statuses/{sha}",
               {"state": state, "context": context, "description": description[:140]})


def create_pull(title: str, head: str, base: str, body: str) -> dict:
    return gh_request("POST", f"/repos/{REPO}/pulls",
                      {"title": title, "head": head, "base": base, "body": body})


def get_pull(number: int) -> dict:
    return gh_request("GET", f"/repos/{REPO}/pulls/{number}")


def pr_diff_via_api(number: int) -> str:
    return gh_request("GET", f"/repos/{REPO}/pulls/{number}",
                      accept="application/vnd.github.v3.diff")


def submit_review(number: int, event: str, body: str) -> None:
    # event: APPROVE | REQUEST_CHANGES | COMMENT
    gh_request("POST", f"/repos/{REPO}/pulls/{number}/reviews",
               {"event": event, "body": body})


def merge_pull(number: int, method: str = "squash") -> dict:
    return gh_request("PUT", f"/repos/{REPO}/pulls/{number}/merge",
                      {"merge_method": method})


def delete_branch(branch: str) -> None:
    try:
        gh_request("DELETE", f"/repos/{REPO}/git/refs/heads/{branch}")
    except RuntimeError as e:
        print(f"branch delete failed (non-fatal): {e}")


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
def run_git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=check)


def configure_git_identity() -> None:
    run_git(["config", "user.name", BOT_NAME])
    run_git(["config", "user.email", BOT_EMAIL])


def stage_all() -> None:
    run_git(["add", "-A", "--", ".", *STAGE_EXCLUDES], check=False)


def working_tree_dirty() -> bool:
    out = run_git(["status", "--porcelain"]).stdout.strip()
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        if any(part in SKIP_DIRS for part in Path(path).parts):
            continue
        if path:
            return True
    return False


def diff_stats() -> tuple[int, int]:
    stage_all()
    out = run_git(["diff", "--cached", "--numstat"]).stdout.strip()
    run_git(["reset"], check=False)
    if not out:
        return 0, 0
    files = lines = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) != 3:
            continue
        files += 1
        a, d = parts[0], parts[1]
        lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return files, lines


def revert_all() -> None:
    run_git(["reset", "--hard", "HEAD"], check=False)
    run_git(["clean", "-fd"], check=False)


def current_branch() -> str:
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def checkout_new_branch(name: str, base: str = "main") -> None:
    run_git(["checkout", "-B", name, f"origin/{base}"], check=False)


def commit_all(message: str) -> bool:
    configure_git_identity()
    stage_all()
    if not run_git(["diff", "--cached", "--quiet"], check=False).returncode:
        return False
    run_git(["commit", "-m", message])
    return True


def push_branch(name: str) -> bool:
    r = run_git(["push", "-u", "origin", f"HEAD:{name}"], check=False)
    if r.returncode != 0:
        print(f"push failed: {r.stderr}")
    return r.returncode == 0


def branch_added_lines(base: str = "main") -> list[str]:
    """Added lines (without leading '+') in the current branch vs base."""
    run_git(["fetch", "origin", base], check=False)
    diff = run_git(["diff", f"origin/{base}...HEAD"], check=False).stdout
    return [ln[1:] for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")]


# --------------------------------------------------------------------------- #
# Path safety + repo map
# --------------------------------------------------------------------------- #
def safe_path(rel: str) -> Path:
    p = (REPO_ROOT / rel).resolve()
    if REPO_ROOT not in p.parents and p != REPO_ROOT:
        raise ValueError(f"path escapes repo: {rel}")
    return p


def is_protected(rel: str) -> bool:
    while rel.startswith("./"):
        rel = rel[2:]
    if any(rel == p.rstrip("/") or rel.startswith(p) for p in PROTECTED_PREFIXES):
        return True
    return rel.endswith(PROTECTED_SUFFIXES)


def repo_map(limit: int = 400) -> str:
    lines = []
    for path in sorted(REPO_ROOT.rglob("*")):
        rel = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_file():
            lines.append(str(rel))
        if len(lines) >= limit:
            lines.append("... (truncated)")
            break
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Agent tools (filesystem + validation)
# --------------------------------------------------------------------------- #
READ_TOOLS = [
    {"name": "list_dir",
     "description": "List files/folders under a repo-relative directory ('' = root).",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "read_file",
     "description": "Read a UTF-8 text file (repo-relative). Truncated if very large.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "run_command",
     "description": ("Run a read-only/validation command from repo root. Allowed: "
                     "python, pytest, ls, cat, grep, head, tail, git diff/status/log/show. "
                     "Network, installs, deletion, git write ops are blocked."),
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
]

WRITE_TOOL = {
    "name": "write_file",
    "description": ("Overwrite/create a text file with full new contents. Protected "
                    "paths (.github/, holdings.yaml, .git/, *.env) are rejected."),
    "input_schema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "content": {"type": "string"}},
                     "required": ["path", "content"]},
}

ALLOWED_CMD_PREFIXES = ("python", "python3", "pytest", "ls", "cat", "grep",
                        "head", "tail", "git diff", "git status", "git log",
                        "git show")
BLOCKED_CMD_TOKENS = ("push", "commit", "reset", "checkout", "rm ", "rmdir",
                      "curl", "wget", "pip", "poetry", "chmod", "mv ", ">", ">>",
                      "sudo", "ssh", "nc ", "clean")


def tool_list_dir(path: str) -> str:
    d = safe_path(path or ".")
    if not d.is_dir():
        return f"ERROR: not a directory: {path}"
    out = []
    for c in sorted(d.iterdir()):
        if c.name in SKIP_DIRS:
            continue
        out.append(f"{'d' if c.is_dir() else 'f'} {c.relative_to(REPO_ROOT)}")
    return "\n".join(out) or "(empty)"


def tool_read_file(path: str) -> str:
    p = safe_path(path)
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    raw = p.read_bytes()
    text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    if len(raw) > MAX_FILE_BYTES:
        text += f"\n... (truncated, {len(raw)} bytes total)"
    return text


def tool_write_file(path: str, content: str) -> str:
    if is_protected(path):
        return f"REJECTED: '{path}' is a protected path and may not be modified."
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def tool_run_command(command: str) -> str:
    cmd = command.strip()
    if not cmd.startswith(ALLOWED_CMD_PREFIXES):
        return f"REJECTED: command must start with one of {ALLOWED_CMD_PREFIXES}"
    if any(tok in cmd for tok in BLOCKED_CMD_TOKENS):
        return "REJECTED: command contains a blocked token."
    try:
        r = subprocess.run(cmd, cwd=REPO_ROOT, shell=True, capture_output=True,
                           text=True, timeout=300)
        return f"exit={r.returncode}\n{((r.stdout or '') + (r.stderr or ''))[:8000]}"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 300s"


def build_dispatch(allow_write: bool) -> dict:
    d = {
        "list_dir": lambda i: tool_list_dir(i["path"]),
        "read_file": lambda i: tool_read_file(i["path"]),
        "run_command": lambda i: tool_run_command(i["command"]),
    }
    if allow_write:
        d["write_file"] = lambda i: tool_write_file(i["path"], i["content"])
    return d


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_changes() -> tuple[bool, str]:
    log = []
    changed = run_git(["diff", "--name-only"]).stdout.split()
    py = [f for f in changed if f.endswith(".py")]
    if py:
        r = subprocess.run([sys.executable, "-m", "py_compile", *py],
                           cwd=REPO_ROOT, capture_output=True, text=True)
        log.append(f"$ py_compile {' '.join(py)}\nexit={r.returncode}\n{r.stderr}")
        if r.returncode != 0:
            return False, "\n".join(log)
    if (REPO_ROOT / "tests").is_dir():
        r = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_*.py"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        combined = (r.stdout or "") + (r.stderr or "")
        log.append(f"$ unittest discover\nexit={r.returncode}\n{combined[-4000:]}")
        if r.returncode != 0:
            return False, "\n".join(log)
    return True, "\n".join(log) or "no python changes / no tests"


# --------------------------------------------------------------------------- #
# Claude agent loop
# --------------------------------------------------------------------------- #
def make_client():
    try:
        from anthropic import Anthropic
    except ImportError:
        sys.exit("anthropic SDK not installed. Run: pip install anthropic")
    return Anthropic()  # reads ANTHROPIC_API_KEY


def run_agent_loop(client, model: str, system: str, first_message: str,
                   tools: list, dispatch: dict, finish_tool: str,
                   max_calls: int = MAX_TOOL_CALLS) -> dict | None:
    """Run a tool-use loop until the model calls `finish_tool`. Returns its input."""
    messages = [{"role": "user", "content": first_message}]
    result = None
    for _ in range(max_calls):
        resp = client.messages.create(model=model, max_tokens=4096,
                                      system=system, tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            messages.append({"role": "user",
                             "content": f"Please finish by calling {finish_tool}."})
            continue
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if block.name == finish_tool:
                result = block.input
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": "recorded"})
                continue
            try:
                output = dispatch[block.name](block.input)
            except Exception as e:  # noqa: BLE001
                output = f"ERROR: {e}"
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": str(output)})
        messages.append({"role": "user", "content": tool_results})
        if result is not None:
            break
    return result


def require_env(*names: str) -> None:
    for n in names:
        if not os.environ.get(n):
            sys.exit(f"Missing required env var: {n}")
