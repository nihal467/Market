# Daily Refinement — PR pipeline (propose → scan → review → merge)

Every day at **18:00 IST**, for each open `daily-refinement` issue:

1. **Propose** — a Claude agent reviews the day's evidence and, only if it's
   justified, makes a small paper-only change on a branch and **opens a PR**
   (`Closes #<issue>`). If no change is warranted, it comments and closes the
   issue.
2. **Security scan** — the PR diff is scanned for leaked secrets and sensitive
   data (rupee amounts, holdings figures, `op://` refs, keys, PII,
   protected-file edits). Any **HIGH** finding blocks the PR.
3. **Review** — a separate, skeptical Claude reviewer re-derives the decision:
   evidence-backed? small? paper-only? tests pass? nothing leaked? It submits a
   GitHub **APPROVE** or **REQUEST_CHANGES** review.
4. **Merge** — a PR is squash-merged to `main` **only if** the security scan
   passed **and** the reviewer approved. Merging auto-closes the issue.

The only secret you add is your Claude API key (encrypted in GitHub). The
built-in `GITHUB_TOKEN` handles branches, PRs, reviews, and the merge — **no PAT
needed**.

## Files to add to the repo

    .github/workflows/daily-refinement.yml   # the daily pipeline
    .github/workflows/security-scan.yml       # PR security gate (gitleaks + custom)
    scripts/agent_lib.py                       # shared helpers
    scripts/propose_fix.py                     # proposer agent
    scripts/security_scan.py                   # sensitive-data scanner (also standalone)
    scripts/review_pr.py                       # reviewer agent + auto-merge
    scripts/ensure_labels.py                   # creates pipeline labels (idempotent)
    scripts/summarize_pipeline.py              # job-summary helper
    scripts/setup_branch_protection.sh         # one-time: require the security check

Drop them at those exact paths and commit to `main`.

The `auto-refinement` and `security-failed` labels are **created automatically**
on the first run (the "Ensure labels exist" step) — nothing to do by hand.

## One secret to add

1. Get a key at https://console.anthropic.com → **API Keys**.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `ANTHROPIC_API_KEY`
   - Value: your key

## Optional repo variables

**Settings → Secrets and variables → Actions → Variables**

| Variable         | Default            | Purpose                                   |
|------------------|--------------------|-------------------------------------------|
| `CLAUDE_MODEL`   | `claude-sonnet-4-5`| Model for both agents (set to one your key can use) |
| `PROPOSER_MODEL` | = `CLAUDE_MODEL`   | Override just the proposer                 |
| `REVIEWER_MODEL` | = `CLAUDE_MODEL`   | Override just the reviewer (e.g. a stronger model to review a cheaper proposer) |

Confirm the exact model id in the Anthropic console; a wrong id fails fast with
a clear API error.

## Try it safely first

- **Actions → Daily Refinement → Run workflow**, set **auto_merge: false**.
  It will open PRs and post reviews but not merge — you inspect and merge by hand.
- When you trust it, run normally (or wait for the 18:00 IST schedule).

## Make the security scan a required check (recommended)

The scanner publishes a commit status named **`security/sensitive-data`** on
every PR — for bot PRs (from the daily pipeline) *and* human PRs (from
`security-scan.yml`). Require it once so nothing can merge to `main` while it's
failing:

```bash
gh auth login                        # needs admin on the repo
./scripts/setup_branch_protection.sh nihal467/Market
```

This sets branch protection to require `security/sensitive-data`, with
`strict=false` (PRs need not be up to date) and **no required human review** —
otherwise the pipeline's own review + auto-merge would be blocked. The pipeline
runs the scan *before* it merges, so a passing status is in place by merge time.
Prefer a human gate? Skip this and set `AUTO_MERGE=0`, or require reviews and
merge by hand. (gitleaks runs as its own non-required check.)

## Repo settings to check

- **Settings → Actions → General → Workflow permissions**: enable
  *Read and write permissions* and *Allow GitHub Actions to create and approve
  pull requests* (needed for the bot to open/approve/merge PRs).
- Optional branch protection on `main`: if you require the `security-scan`
  checks before merge, note that bot-opened PRs are merged by the same pipeline;
  a required-review rule that forbids the actions bot's approval would block
  auto-merge — in that case set **auto_merge: false** and merge manually.

## The security gate (what blocks a merge)

`scripts/security_scan.py` scans **added** diff lines and flags:

- **Secrets/keys** — Anthropic/OpenAI/Google/AWS/Slack/GitHub tokens, private
  keys, JWTs, and hardcoded `password/secret/token = "…"` literals. Safe
  references (`${{ secrets.* }}`, `os.environ`, `op://…`, placeholders) are
  exempt.
- **Personal financial data** (this repo is public) — rupee amounts (`₹1234+`),
  holdings figures (`invested/units/quantity/avg_price = …`).
- **PII** — PAN, possible Aadhaar numbers.
- **Protected files** — any edit to `holdings.yaml`, `.env`, key files, or
  `.github/workflows/*` inside a PR.

Any **HIGH** finding fails the check and posts a `REQUEST_CHANGES` review. The
standalone `security-scan.yml` also runs **gitleaks** on every PR. Tune the
threshold with the `FAIL_ON` env (`HIGH` default; `MED` to be stricter).

## Guardrails baked into the agents

- Proposer defaults to **no change**; only small, evidence-backed, paper-only
  edits. Protected paths can't be written. Over-budget diffs (~8 files / ~400
  lines) are discarded.
- Every change is `py_compile` + `pytest` validated before a PR is opened, and
  again by the reviewer before approval.
- Reviewer runs with **read-only** tools and is prompted to reject anything
  unverified, unjustified, unsafe, or leaky.
- Commits use the `github-actions[bot]` identity; `data/`, `__pycache__`,
  `*.pyc` are never staged.

## Knobs (env in `daily-refinement.yml`)

| Var          | Default | Meaning                                  |
|--------------|---------|------------------------------------------|
| `ISSUE_LABEL`| `daily-refinement` | Which issues to process       |
| `MAX_ISSUES` | `3`     | Max issues per run                       |
| `AUTO_MERGE` | `1`     | `0` = approve but don't merge            |
| `MERGE_METHOD`| `squash`| `squash` / `merge` / `rebase`           |
| `FAIL_ON`    | `HIGH`  | Blocking severity for the security scan  |

## Good to know

- **Independence caveat:** proposer and reviewer are both Claude, so review
  isn't fully independent. The security scan is a deterministic, non-AI gate,
  and you can point `REVIEWER_MODEL` at a stronger model than the proposer.
  For a human gate, set `AUTO_MERGE=0`.
- **Cron is best-effort** (can run late); use the manual trigger any time.
- **Cost:** two short agent sessions per issue (propose + review). With 1–3
  issues/day, expect a few cents to low-tens-of-cents/day depending on model.
- **Rotate** `ANTHROPIC_API_KEY` periodically.
