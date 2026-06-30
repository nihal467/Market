# Cloudflare Intraday Watchdog

Free Cloudflare Worker cron for starting the GitHub intraday watchdog when
GitHub's own scheduled workflows are delayed or dropped.

Flow:

```text
Cloudflare Cron -> GitHub workflow_dispatch -> intraday_watchdog.yml -> intraday.yml
```

The GitHub watchdog checks whether an intraday loop is already queued/running, so
frequent Cloudflare pings do not duplicate market-data loops.

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

## Cron

`wrangler.toml` runs every 5 minutes on weekdays from `03:00-10:59 UTC`
(`08:30-16:29 IST`). The Worker itself gates dispatches to `09:05-15:35 IST`.

## Verify

After deploy:

```bash
npx wrangler tail
gh run list --workflow=intraday_watchdog.yml --limit 5
gh run list --workflow=intraday.yml --limit 5
```

You should see `intraday_watchdog.yml` runs with `event=workflow_dispatch`
created by Cloudflare, and then `intraday.yml` running if no intraday loop was
already active.
