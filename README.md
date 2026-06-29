# Investment Tracker

Track your stock + mutual fund portfolio across **Groww** and **5paisa** using
free public data sources. No broker login required — you keep your holdings in
`holdings.yaml`, and the tool fetches live prices/NAVs to value them, generates
daily BUY/HOLD/SELL signals, and builds a dashboard.

> Three phases: **(1)** portfolio tracker, **(2)** daily RSI/SMA + news-sentiment
> signals, **(3)** self-contained dashboard + daily GitHub Action. No auto-trading
> — signals are informational; you trade manually.

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure your holdings
Edit `holdings.yaml`:
- **Stocks (Groww):** add each stock with Yahoo symbol (`SYMBOL.NS`), `quantity`, and total `invested`.
- **Mutual funds (5paisa):** add `scheme_code` (from AMFI), `units`, and total `invested`.
  Find your scheme code in https://www.amfiindia.com/spages/NAVAll.txt
- **Lumpsum (₹5L):** list it under stocks or mutual_funds with `invested` filled in.

Update these numbers whenever a SIP installment buys new units.

## Run
```bash
source .venv/bin/activate
python src/run_daily.py     # full pipeline: portfolio -> signals -> dashboard
```
Or run stages individually:
```bash
python src/portfolio.py    # Phase 1: portfolio value, P&L, allocation
python src/signals.py      # Phase 2: daily BUY/HOLD/SELL signals
python src/dashboard.py    # Phase 3: build docs/index.html
```
`portfolio.py` writes `data/portfolio.json`. `signals.py` writes
`data/signals.json` (technical indicators + news sentiment + recommendation).

## Dashboard (Phase 3)
`docs/index.html` is a **self-contained** page (data embedded — no server needed).
Just open it in a browser (double-click). View it anytime after a pipeline run.

## Automation (Phase 3)
`.github/workflows/daily.yml` runs **every weekday at 16:00 IST** (after NSE close):
1. installs deps, runs `src/run_daily.py`
2. commits the refreshed `docs/index.html` back to the repo
3. uploads it as a workflow artifact

To see the latest dashboard: `git pull` then open `docs/index.html`, or download
the `dashboard` artifact from the workflow run.

> **Privacy:** the repo is **public** (for free GitHub Pages hosting), so rupee
> amounts are **hidden** — only %, signals, and names are shown. Amounts are
> **AES-256 encrypted** inside the page and unlocked in-browser with a password.
>
> **Password via 1Password:** the source of truth is a 1Password item
> (`op://Personal/Market Dashboard/password`, set in `holdings.yaml` as
> `password_op_ref`).
> - **Local runs** fetch it automatically via the 1Password CLI — just be signed
>   in (`op signin`), then `python src/dashboard.py`. No env var needed.
> - **CI** uses the `DASHBOARD_PASSWORD` GitHub Secret, which mirrors the same
>   1Password value. (A 1Password *service account* would let CI read the vault
>   directly, but this org-managed account blocks creating one.)
>
> To rotate the password, update it in 1Password and mirror it to CI:
> ```bash
> NEW=$(op read "op://Personal/Market Dashboard/password")
> printf '%s' "$NEW" | gh secret set DASHBOARD_PASSWORD --repo nihal467/Market
> ```
> Wrong passwords cannot decrypt the data (AES-GCM + PBKDF2-SHA256, 200k iters).

## How signals work (Phase 2)
For each equity/ETF holding the tool computes:
- **RSI(14)** — oversold (<30) bullish, overbought (>70) bearish
- **SMA50 vs price** — trend direction
- **SMA50 vs SMA200** — golden/death cross
- **52-week position** — near high (book profit) / near low (value)
- **News sentiment** — Google News headlines scored for tone

These combine into a score → **BUY / HOLD / SELL** with written reasons.
Mutual funds are shown as HOLD (long-term SIP, not a trade signal).
Signals are informational only — you decide and trade manually.

## Data sources
- Stock prices: Yahoo Finance (`yfinance`)
- MF NAVs: AMFI daily NAV feed
- No credentials needed.

## Project layout
```
holdings.yaml          your portfolio config
src/fetch_prices.py    stock prices (Yahoo)
src/fetch_nav.py       MF NAVs (AMFI)
src/portfolio.py       valuation + snapshot
src/indicators.py      RSI / SMA / 52-week range
src/news_sentiment.py  Google News RSS + sentiment scoring
src/strategy.py        rule engine -> BUY/HOLD/SELL
src/signals.py         daily signal orchestrator
src/dashboard.py       builds self-contained docs/index.html
src/run_daily.py       runs the full daily pipeline
docs/index.html        dashboard (open in browser)
.github/workflows/daily.yml   daily cron automation
data/portfolio.json    latest portfolio snapshot (git-ignored)
data/signals.json      latest signals (git-ignored)
```

## Disclaimer
For personal tracking only. Not investment advice.

## Market data pipeline (watchlist + monitoring)

Beyond your own portfolio, the repo runs three scheduled jobs that gather a
broad market dataset and publish it to a separate **`data` branch** (kept apart
from code so history stays clean — no paid service needed):

| Job | Schedule | What it does | Output (on `data` branch) |
|-----|----------|--------------|---------------------------|
| **Weekly watchlist** | Sun 17:30 IST | Ranks the NSE universe (`universe.csv`, ~170 names) by technicals + news → Top 50 | `watchlist/YYYY-Www.json`, `watchlist/latest.json` |
| **Intraday monitor** | every 15 min, market hours | Snapshots the Top-50 (price, %chg, RSI), flags movers | `intraday/YYYY/MM/DD.jsonl`, `intraday/latest.json` |
| **Daily analysis** | 16:30 & 01:00 IST | Full EOD signals + day-over-day change detection (signal flips, RSI regime) | `daily/YYYY/MM/DD.json`, `daily/latest.json` |

Local runs write to `data_out/` (git-ignored). In CI, `MARKET_DATA_DIR` points
at a checkout of the `data` branch.

**Important caveats**
- GitHub Actions cron is best-effort: runs can be 5–20+ min late or skipped.
  The intraday feed is *near-real-time monitoring*, **not** a low-latency
  trading trigger. The Python market-calendar gate no-ops outside NSE hours.
- Public price feeds (Yahoo) can rate-limit CI IPs; jobs batch requests and the
  universe is shipped in-repo so we never depend on NSE's (CI-blocked) site.
- Holdings/watchlists are signals only — **not investment advice**.
