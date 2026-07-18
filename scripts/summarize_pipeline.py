#!/usr/bin/env python3
"""Print a short markdown summary of the pipeline state (for the job summary)."""
import json
import os

path = os.environ.get("STATE_FILE", "pipeline_state.json")
try:
    prs = json.load(open(path, encoding="utf-8")).get("prs", [])
except FileNotFoundError:
    prs = []

if not prs:
    print("No PRs opened (no change warranted, or no open issues).")
else:
    for p in prs:
        print(f"- PR #{p['number']} (issue #{p['issue']}): "
              f"security={p.get('security', '?')}, status={p.get('status', '?')}")
