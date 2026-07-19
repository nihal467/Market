"""Backtest-vs-live drift monitor (advisory sanity check).

Replays the backtest simulator with the ACTIVE_PROFILE configuration over the
exact calendar window the live paper book has been running (first to last date
in the paper history) and compares the simulated equity curve against the
actual recorded paper values:

  - final-value divergence % (simulated final vs actual final)
  - correlation of daily returns on common dates
  - max single-day gap between simulated and actual daily returns

Verdict:
  - "ok"                |final divergence| <= 2.0% and correlation >= 0.9
  - "drift"             otherwise — worth a human look
  - "insufficient_data" fewer than 10 paper days (or the replay could not be
                        built) — nothing to conclude yet, and that's fine.

This is a SANITY MONITOR, not an exact reconciliation: the live book trades
off an evolving weekly watchlist (plus news-aware signals) while the replay
uses today's universe and technicals only, so modest divergence is expected
and honest. Writes drift/latest.json for the dashboard/readiness report.
"""
from __future__ import annotations

import math
import sys

import datastore as ds
from market_calendar import now_ist
from strategy_config import ACTIVE_PROFILE

OUTPUT_FILE = "drift/latest.json"
MIN_PAPER_DAYS = 10
FINAL_DIVERGENCE_LIMIT_PCT = 2.0
MIN_CORRELATION = 0.9

NOTE = (
    "Sanity monitor, not an exact reconciliation: the live book trades an "
    "evolving watchlist with news-aware signals while the replay uses today's "
    "universe and technicals only, so modest divergence is expected."
)


def _load_paper_history() -> list[dict]:
    """Chronological, date-deduped paper history rows with values."""
    state = ds.read_json("paper/state.json", default=None) or {}
    history = state.get("history") or []
    if not history:
        latest = ds.read_json("paper/latest.json", default={}) or {}
        history = latest.get("history") or []
    by_date = {
        row["date"]: row for row in history
        if row.get("date") and row.get("value")
    }
    return [by_date[d] for d in sorted(by_date)]


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None  # a flat series has no measurable co-movement
    return cov / math.sqrt(vx * vy)


def _compare(sim_by_date: dict[str, float], paper_rows: list[dict]) -> dict | None:
    """Divergence metrics on dates present in BOTH curves; None if too few."""
    common = [r for r in paper_rows if r["date"] in sim_by_date]
    if len(common) < MIN_PAPER_DAYS:
        return None
    paper_vals = [float(r["value"]) for r in common]
    sim_vals = [float(sim_by_date[r["date"]]) for r in common]
    final_div = round((sim_vals[-1] / paper_vals[-1] - 1) * 100, 3) \
        if paper_vals[-1] else None
    paper_rets = [paper_vals[i] / paper_vals[i - 1] - 1
                  for i in range(1, len(paper_vals)) if paper_vals[i - 1]]
    sim_rets = [sim_vals[i] / sim_vals[i - 1] - 1
                for i in range(1, len(sim_vals)) if sim_vals[i - 1]]
    corr = _pearson(sim_rets, paper_rets)
    max_gap = max((abs(s - p) for s, p in zip(sim_rets, paper_rets)), default=None)
    return {
        "n_common_days": len(common),
        "final_divergence_pct": final_div,
        "daily_return_correlation": round(corr, 3) if corr is not None else None,
        "max_single_day_gap_pct": round(max_gap * 100, 3) if max_gap is not None else None,
    }


def _verdict(metrics: dict | None) -> str:
    if metrics is None:
        return "insufficient_data"
    div = metrics.get("final_divergence_pct")
    corr = metrics.get("daily_return_correlation")
    if corr is None:
        # Flat/degenerate return series: nothing measurable yet.
        return "insufficient_data"
    if div is not None and abs(div) <= FINAL_DIVERGENCE_LIMIT_PCT \
            and corr >= MIN_CORRELATION:
        return "ok"
    return "drift"


def _write(verdict: str, ist, detail: str, metrics: dict | None = None,
           paper_rows: list[dict] | None = None) -> dict:
    rows = paper_rows or []
    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "active_profile": ACTIVE_PROFILE,
        "verdict": verdict,
        "detail": detail,
        "n_paper_days": len(rows),
        "paper_start": rows[0]["date"] if rows else None,
        "paper_end": rows[-1]["date"] if rows else None,
        "thresholds": {
            "final_divergence_limit_pct": FINAL_DIVERGENCE_LIMIT_PCT,
            "min_daily_return_correlation": MIN_CORRELATION,
            "min_paper_days": MIN_PAPER_DAYS,
        },
        "note": NOTE,
        **(metrics or {}),
    }
    ds.write_json(OUTPUT_FILE, payload)
    print(f"Drift monitor: {verdict} — {detail}")
    return payload


def run() -> dict:
    ist = now_ist()
    rows = _load_paper_history()
    if len(rows) < MIN_PAPER_DAYS:
        return _write(
            "insufficient_data", ist,
            f"only {len(rows)}/{MIN_PAPER_DAYS} paper days recorded — "
            "not enough live history to compare yet",
            paper_rows=rows)

    # Heavy imports stay inside run() so the pure comparison helpers above are
    # importable (and unit-testable) without pandas/yfinance.
    import pandas as pd
    import yfinance as yf

    import backtest as bt
    from universe import load_universe

    uni = load_universe()
    sectors = {u["symbol"]: u.get("sector") or "Unknown" for u in uni}
    symbols = [u["symbol"] for u in uni]
    print(f"Drift monitor: replaying {ACTIVE_PROFILE} over "
          f"{rows[0]['date']} .. {rows[-1]['date']} ({len(rows)} paper days)")

    frames, n_with_data = bt._download(symbols)
    coverage = n_with_data / len(symbols) if symbols else 0.0
    if coverage < bt.MIN_COVERAGE or not frames:
        return _write(
            "insufficient_data", ist,
            f"price download coverage {coverage * 100:.1f}% below "
            f"{bt.MIN_COVERAGE * 100:.0f}% floor — skipping replay",
            paper_rows=rows)

    try:
        bdata = yf.download(bt.BENCHMARK, period=bt.LOOKBACK, interval="1d",
                            auto_adjust=True, progress=False)
        bclose = bdata["Close"]
        if hasattr(bclose, "columns"):
            bclose = bclose.iloc[:, 0]
        bclose = bclose.dropna()
    except Exception as exc:  # noqa: BLE001 — benchmark is best-effort here
        print(f"  ! benchmark download failed: {exc}")
        bclose = pd.Series(dtype=float)

    feats = bt._precompute(frames)
    all_dates = sorted(set().union(*[df.index for df in feats.values()]))
    first, last = rows[0]["date"], rows[-1]["date"]
    window = [d for d in all_dates
              if first <= pd.Timestamp(d).strftime("%Y-%m-%d") <= last]
    if len(window) < MIN_PAPER_DAYS:
        return _write(
            "insufficient_data", ist,
            f"only {len(window)} downloadable trading days inside the paper "
            "window — cannot replay it",
            paper_rows=rows)

    sim = bt._simulate_variant(feats, window, bclose, sectors, bt._active_variant())
    sim_by_date = dict(zip(sim.get("_eq_dates") or [], sim.get("_equity") or []))
    metrics = _compare(sim_by_date, rows)
    verdict = _verdict(metrics)
    if metrics is None:
        detail = "too few overlapping dates between replay and paper history"
    else:
        detail = (
            f"final divergence {metrics['final_divergence_pct']}%, "
            f"daily-return correlation {metrics['daily_return_correlation']}, "
            f"max single-day gap {metrics['max_single_day_gap_pct']}% "
            f"over {metrics['n_common_days']} common days"
        )
    return _write(verdict, ist, detail, metrics=metrics, paper_rows=rows)


if __name__ == "__main__":
    result = run()
    if result.get("verdict") == "insufficient_data":
        sys.exit(0)
