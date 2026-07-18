# Refinement PR pipeline — setup (Max plan, no API billing)

Every day at **18:00 IST**, for the open `daily-refinement` issue:

1. **Propose** — Claude Code (running as the Claude GitHub App) makes a small,
   evidence-backed, paper-only change on a branch and opens a PR (`Closes #<n>`),
   or comments and closes the issue if no change is warranted.
2. **Security scan** — a Python scanner gates the PR for secrets and sensitive
   data (rupee amounts, holdings figures, keys, PII, protected-file edits). Any
   HIGH finding blocks the merge and publishes a `security/sensitive-data`
   commit status.
3. **Review** — a second, independent Claude Code pass reviews the PR and submits
   APPROVE / REQUEST_CHANGES.
4. **Merge** — squash-merged only if the scan passed and the review approved.

Auth is your **Claude Max** subscription via an OAuth token — **no Anthropic API
billing**.

## Files (already in the repo)

    .github/workflows/refinement_pipeline.yml   # the daily pipeline (Claude Code Action)
    .github/workflows/security_scan.yml          # PR security gate (gitleaks + custom)
    scripts/agent_lib.py                          # GitHub/git helpers used by the scanner
    scripts/security_scan.py                      # sensitive-data scanner
    scripts/ensure_labels.py                      # creates pipeline labels
    scripts/summarize_pipeline.py                 # (helper)
    scripts/setup_branch_protection.sh            # one-time: require the security check

## One-time setup

### 1. Install the Claude GitHub App
Go to **https://github.com/apps/claude** → Install → select **nihal467/Market**.
(This is what lets Claude Code open branches, PRs, comments, and reviews.)

### 2. Create your Max OAuth token and add it as a secret
On your Mac, with the Claude Code CLI installed (`npm i -g @anthropic-ai/claude-code`):
```
claude setup-token
```
Sign in with your **Max** account when prompted; it prints a token.
Then add it as a repo secret — **Settings → Secrets and variables → Actions →
New repository secret**:
- Name: `CLAUDE_CODE_OAUTH_TOKEN`
- Secret: the token from `claude setup-token`

That's the only secret. No `ANTHROPIC_API_KEY` needed.

### 3. Enable Actions to open/merge PRs
**Settings → Actions → General → Workflow permissions:**
- Select **Read and write permissions**
- Check **Allow GitHub Actions to create and approve pull requests**

### 4. (Recommended) require the security scan before merge
```
gh auth login                        # needs admin on the repo
./scripts/setup_branch_protection.sh nihal467/Market
```

### 5. Test it
**Actions → Refinement PR Pipeline → Run workflow** → set **auto_merge: false**
(optionally enter a specific issue number) → Run. Watch it open a PR and post a
review without merging. When happy, run normally or let the 18:00 IST schedule
take over.

## Notes / caveats

- **Beta:** the Claude Code Action is in beta; the OAuth-token flow uses your Max
  usage limits and can occasionally be flaky. If a run fails on auth, regenerate
  the token with `claude setup-token` and update the secret.
- **Independence:** proposer and reviewer are both Claude; the deterministic
  security scan is the real gate. For a human gate, set the dispatch input
  `auto_merge: false`.
- **Cron is best-effort** (can run late); use the manual trigger any time.
- **The scanner** still runs on every PR via `security_scan.yml` (gitleaks +
  custom), so human PRs are covered too.
