# Cloudflare Market Scheduler

Free Cloudflare Worker cron for starting all scheduled GitHub workflows when
GitHub's own scheduler is delayed or dropped.

Flow:

```text
Cloudflare Cron -> GitHub workflow_dispatch -> GitHub workflow
```

The intraday GitHub watchdog still checks whether an intraday loop is already
queued/running, so frequent Cloudflare pings do not duplicate market-data loops.
The daily/weekly jobs use Worker-side duplicate checks before dispatching.

Deployed URL:

```text
https://market-intraday-watchdog.nihal467-market.workers.dev
```

## Setup

Create a GitHub fine-grained personal access token with access to
`nihal467/Market` and permission to run Actions. Store it only as a Cloudflare
Worker secret:

```bash
cd cloudflare/intraday-watchdog
npm install
npx wrangler login
printf '%s' '<GITHUB_TOKEN>' | npx wrangler secret put GITHUB_TOKEN
npx wrangler deploy
```

Optional manual trigger secret:

```bash
openssl rand -hex 24 | npx wrangler secret put TRIGGER_SECRET
```

## Schedule

`wrangler.toml` runs every 5 minutes. The Worker dispatches only when one of
these IST schedules is due:

| Task | GitHub workflow | Cloudflare schedule |
| --- | --- | --- |
| Intraday watchdog | `intraday_watchdog.yml` | Mon-Fri 09:05-15:35 IST |
| Dashboard deploy | `daily.yml` | Mon-Fri 16:00 IST |
| Daily analysis close | `daily_analysis.yml` | Mon-Fri 16:30 IST |
| Daily analysis overnight | `daily_analysis.yml` | Tue-Sat 01:00 IST |
| Weekly watchlist | `weekly_watchlist.yml` | Sun 17:30 IST |

## Verify

After deploy:

```bash
npx wrangler tail
gh run list --workflow=intraday_watchdog.yml --limit 5
gh run list --workflow=intraday.yml --limit 5
```

You should see workflow runs with `event=workflow_dispatch` created by
Cloudflare at the schedule times.
