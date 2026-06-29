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

> **Privacy:** this is a **private** repo (financial data). GitHub Pages is not
> available on private repos under the free plan, so the dashboard is viewed
> locally rather than hosted. If you make the repo public, set
> `dashboard.hide_amounts: true` in `holdings.yaml` first to hide rupee values.

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
