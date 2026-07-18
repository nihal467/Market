#!/usr/bin/env python3
"""Create the pipeline labels if they don't already exist (idempotent)."""
import agent_lib as lib

LABELS = [
    ("auto-refinement", "1d76db", "Automated daily paper-trading refinement PR"),
    ("security-failed", "b60205", "Security scan found secrets / sensitive data"),
]

if __name__ == "__main__":
    lib.require_env("GITHUB_TOKEN", "GITHUB_REPOSITORY")
    for name, color, desc in LABELS:
        lib.ensure_label(name, color, desc)
        print(f"ensured label: {name}")
