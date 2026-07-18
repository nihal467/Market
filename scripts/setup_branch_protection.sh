#!/usr/bin/env bash
#
# One-time: require the security scan (context "security/sensitive-data") to
# pass before anything merges to main. Run locally once; needs admin on the
# repo and the GitHub CLI signed in (`gh auth login`).
#
#   ./scripts/setup_branch_protection.sh [owner/repo]
#
# Notes:
#  - strict=false: PRs need not be up-to-date with main (so auto-merge isn't
#    blocked by an intervening commit).
#  - required_pull_request_reviews is left null on purpose: the pipeline's own
#    Claude review + auto-merge would be blocked by a human-review requirement.
#    Set AUTO_MERGE=0 instead if you want a human to click merge.
#  - The scanner publishes this status for BOTH bot PRs (via the daily pipeline)
#    and human PRs (via security-scan.yml), so the required check is satisfiable
#    in both paths.
set -euo pipefail

REPO="${1:-nihal467/Market}"

# To ALSO require the AI review, add "review/approved" to the contexts list
# below. Only do that if every PR to main will actually receive a review
# (bot PRs do; a human PR would need review_pr.py --pr run on it), otherwise
# merges will deadlock waiting for a status that never arrives.

echo "Requiring 'security/sensitive-data' on ${REPO}:main ..."
gh api -X PUT "repos/${REPO}/branches/main/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["security/sensitive-data"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

echo "Done. 'security/sensitive-data' is now a required status check on main."
