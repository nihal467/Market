# Investment Tracker (Phase 1)

Track your stock + mutual fund portfolio across **Groww** and **5paisa** using
free public data sources. No broker login required — you keep your holdings in
`holdings.yaml`, and the tool fetches live prices/NAVs to value them.

> Phase 1 = tracker only. Daily signals (RSI/MA + news sentiment) and the
> GitHub Pages dashboard come in later phases.

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
python src/portfolio.py    # Phase 1: portfolio value, P&L, allocation
python src/signals.py      # Phase 2: daily BUY/HOLD/SELL signals
```
`portfolio.py` writes `data/portfolio.json`. `signals.py` writes
`data/signals.json` (technical indicators + news sentiment + recommendation).

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
data/portfolio.json    latest portfolio snapshot (git-ignored)
data/signals.json      latest signals (git-ignored)
```

## Disclaimer
For personal tracking only. Not investment advice.
