# Market — AI Paper-Trading Lab

A fully automated, self-refining **paper-trading system** for NSE equities.
No broker login, no real orders, no real money — a virtual ₹5,00,000 book run
by a transparent rule engine, monitored intraday, evaluated honestly, and
**refined daily by an AI review pipeline**.

> Everything here is simulation. Signals and results are informational only and
> **not investment advice**. The readiness gate never authorizes real-money
> trading — it can only mark the system "eligible for review" by a human.

## How it works

```
universe.csv (~170 NSE names)
   │  Sun 17:30 IST
   ▼
watchlist.py ── ranks by momentum/technicals + news → Top 50
   │  Mon-Fri 16:30 IST
   ▼
daily_analysis.py ── EOD indicators + strategy.decide() → signals
   │
   ▼
paper_trader.py ── virtual book: decides at close T, FILLS AT NEXT OPEN (T+1),
   │               with slippage + transaction costs, stops, sector caps,
   │               weekly churn control, and a 15% drawdown circuit-breaker
   │  every 15 min, market hours
   ▼
intraday_trader.py ── marks the book, protective stops (same adjusted basis)
   │
   ▼
incubation_report.py ── readiness gate: recomputed P&L, out-of-sample backtest
                        check, execution-model check. Caps at
                        "eligible_for_review" — never real money.
```

All state is JSON on the **`data` branch** (kept apart from code). Local runs
write to `data_out/` (git-ignored).

## Honest-simulation rules

The engine is built to avoid the classic ways backtests lie:

- **No look-ahead:** decisions use day T's close; fills happen at day T+1's
  open. Same model in the live paper book and the backtest.
- **Costs are real:** every fill pays slippage (5 bps) + transaction costs
  (12 bps/side ≈ NSE delivery STT + charges). Cumulative cost drag is tracked
  and reported.
- **One price basis:** all feeds use dividend/split-adjusted prices — the book
  is never marked against a different basis than it was filled on.
- **Partial data ⇒ no trading:** if EOD coverage drops below 80%, the day is
  flagged and no new orders are queued.
- **Known caveats stay visible:** survivorship bias (today's universe only),
  best-effort scheduling, delayed public data — all disclosed in outputs.

## The AI refinement loop

| Job | Schedule (IST) | What it does |
| --- | --- | --- |
| `intraday_watchdog.yml` | 09:05–15:35, 15-min | Snapshot Top-50, mark book, stops |
| `daily_analysis.yml` | 16:30 Mon–Fri | EOD signals + day-over-day changes |
| `daily_refinement.yml` | 17:15 Mon–Fri | Opens the day's refinement **issue** with P&L, gate, and backtest evidence |
| `refinement_pipeline.yml` | 18:00 Mon–Fri | **Claude Code pipeline**: propose fix → security scan → independent review → up to 3 fix/review cycles → auto-merge |
| `weekly_watchlist.yml` | Sun 17:30 | Rebuild Top-50 watchlist |
| `ci.yml` | on push/PR | Compile, unit tests, workflow YAML checks |

The refinement pipeline (see `REFINEMENT_SETUP.md`) is restricted to the repo
owner and its own `auto-refine/*` branches, gated by a sensitive-data security
scanner (`scripts/security_scan.py` + gitleaks on every PR), and instructed to
prefer **no change** unless the day's evidence justifies a small, testable,
paper-only edit.

## Project layout

```
universe.csv                 NSE universe (~170 names)
src/strategy.py              weighted rule engine → BUY/HOLD/SELL
src/strategy_config.py       ALL strategy knobs incl. costs/slippage/breaker
src/indicators.py            RSI / SMA / 52-week range
src/market_regime.py         NIFTY trend filter (risk-on/off)
src/news_sentiment.py        Google News RSS sentiment
src/watchlist.py             weekly Top-50 ranking
src/daily_analysis.py        EOD signals + change detection
src/paper_trader.py          EOD virtual book (next-open fills, costs, breaker)
src/intraday_trader.py       intraday marks + protective stops
src/intraday.py              intraday snapshots of the watchlist
src/backtest.py              next-open backtest + strategy lab + train/validation split
src/incubation_report.py     readiness gate (never authorizes real money)
src/market_calendar.py       NSE calendar (fails loudly when out of date)
src/datastore.py             JSON state on the data branch
scripts/                     refinement pipeline (security scan, labels, setup)
tests/                       unit tests (fills, costs, breaker, gate, calendar)
cloudflare/intraday-watchdog Cloudflare Worker that triggers the IST schedules
```

## Run locally

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python src/daily_analysis.py     # EOD signals
python src/paper_trader.py       # paper book step
python src/backtest.py           # backtest + lab
python -m unittest discover -s tests -p "test_*.py"
```

## Disclaimer

Paper trading only. Public, delayed data. Not investment advice. Past (simulated)
performance does not predict future results.
