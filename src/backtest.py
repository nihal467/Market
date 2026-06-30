"""Backtester: replay the paper-trading strategy over historical data.

Answers the only question that matters before risking real money: *would this
strategy have beaten simply holding the index, and how bumpy would the ride
have been?*

What it does:
  - Downloads ~LOOKBACK of daily history for the whole NSE universe (one batched
    yfinance call) plus the NIFTY 50 benchmark.
  - Pre-computes rolling indicators (SMA50, SMA200, RSI14, 52w high/low) for the
    entire series, so scoring at each historical date is cheap.
  - Walks forward day by day. On each rebalance day it scores every symbol with
    the SAME ``strategy.decide`` the live bot uses (technicals only — news is
    not available historically), picks the TOP_N BUYs, equal-weights them with
    the same trading cost, stop-loss, and position/sector caps, and marks the
    book to market.
  - Produces an equity curve and headline metrics: total return, CAGR, alpha vs
    NIFTY, max drawdown, Sharpe, and positive-day rate.

Caveats (stated honestly):
  - News sentiment is excluded (RSS is current-only), so live results can differ.
  - Survivorship: the universe is today's list; delisted names aren't included.
  - No intraday — fills are at daily close, like the live bot.

Writes backtest/latest.json to the data store for the dashboard.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

import datastore as ds
import strategy
from market_calendar import now_ist
from universe import load_universe

# Mirror the live bot's knobs so the backtest reflects real behaviour.
from paper_trader import (
    COST_PER_SIDE,
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    START_CAPITAL,
    STOP_LOSS_PCT,
    TOP_N,
    TRAILING_STOP_PCT,
)
from strategy_config import strategy_metadata

BENCHMARK = "^NSEI"
LOOKBACK = "2y"
REBALANCE_EVERY = 1     # trading days between rebalances (1 = daily, like live)
WARMUP = 200            # need ~200 bars before SMA200 is meaningful
BATCH = 50


def _download(symbols: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        print(f"  downloading {i + 1}-{i + len(chunk)} of {len(symbols)} ...")
        try:
            data = yf.download(chunk, period=LOOKBACK, interval="1d",
                               auto_adjust=True, progress=False,
                               group_by="ticker", threads=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! download failed: {exc}")
            continue
        for sym in chunk:
            try:
                close = (data["Close"] if len(chunk) == 1 else data[sym]["Close"]).dropna()
            except (KeyError, TypeError):
                continue
            if len(close) >= WARMUP:
                out[sym] = close
    return out


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(loss != 0, 100.0)      # all-gain window -> 100
    rsi = rsi.where(gain != 0, 50.0)       # flat window -> neutral-ish
    return rsi


def _precompute(closes: dict[str, pd.Series]) -> dict[str, pd.DataFrame]:
    """Per-symbol DataFrame of close + rolling indicators, indexed by date."""
    feats: dict[str, pd.DataFrame] = {}
    for sym, close in closes.items():
        df = pd.DataFrame({"close": close})
        df["sma50"] = close.rolling(50).mean()
        df["sma200"] = close.rolling(200).mean()
        df["rsi14"] = _rsi(close)
        df["high52"] = close.rolling(252, min_periods=60).max()
        df["low52"] = close.rolling(252, min_periods=60).min()
        df["ret3m"] = (close / close.shift(63) - 1) * 100
        feats[sym] = df
    return feats


def _indicators_at(df: pd.DataFrame, date) -> dict | None:
    try:
        row = df.loc[date]
    except KeyError:
        return None
    if pd.isna(row["close"]) or pd.isna(row["sma50"]):
        return None
    price = float(row["close"])
    high = float(row["high52"]) if not pd.isna(row["high52"]) else price
    low = float(row["low52"]) if not pd.isna(row["low52"]) else price
    return {
        "price": round(price, 2),
        "sma50": round(float(row["sma50"]), 2) if not pd.isna(row["sma50"]) else None,
        "sma200": round(float(row["sma200"]), 2) if not pd.isna(row["sma200"]) else None,
        "rsi14": round(float(row["rsi14"]), 2) if not pd.isna(row["rsi14"]) else None,
        "pct_from_high": round((price - high) / high * 100, 2) if high else None,
        "pct_from_low": round((price - low) / low * 100, 2) if low else None,
        "ret_3m": round(float(row["ret3m"]), 2) if not pd.isna(row["ret3m"]) else None,
    }


def _max_drawdown(equity: list[float]) -> float:
    peak = -float("inf")
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return round(mdd * 100, 2)


def _sharpe(daily_returns: list[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    arr = np.array(daily_returns)
    sd = arr.std()
    if sd == 0:
        return 0.0
    return round(float(arr.mean() / sd * np.sqrt(252)), 2)


def run() -> dict:
    ist = now_ist()
    uni = load_universe()
    sectors = {u["symbol"]: u.get("sector") or "Unknown" for u in uni}
    symbols = [u["symbol"] for u in uni]
    name_by = {u["symbol"]: u["name"] for u in uni}
    print(f"Backtest over {len(symbols)} symbols, lookback {LOOKBACK} ...")

    closes = _download(symbols)
    if not closes:
        print("No price history downloaded — aborting backtest.")
        return {"skipped": True}

    # Benchmark series.
    try:
        bdata = yf.download(BENCHMARK, period=LOOKBACK, interval="1d",
                            auto_adjust=True, progress=False)
        bclose = bdata["Close"]
        if hasattr(bclose, "columns"):
            bclose = bclose.iloc[:, 0]
        bclose = bclose.dropna()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! benchmark download failed: {exc}")
        bclose = pd.Series(dtype=float)

    feats = _precompute(closes)

    # Master trading calendar = union of all dates, sorted; start after warmup.
    all_dates = sorted(set().union(*[df.index for df in feats.values()]))
    dates = [d for d in all_dates][WARMUP:]
    if len(dates) < 30:
        print("Not enough history after warmup — aborting.")
        return {"skipped": True}

    cash = START_CAPITAL
    positions: dict[str, dict] = {}   # sym -> {qty, avg}
    equity: list[float] = []
    eq_dates: list[str] = []
    daily_returns: list[float] = []
    n_rebalances = 0
    n_stops = 0
    total_costs = 0.0
    turnover = 0.0
    prev_value = START_CAPITAL

    def price_at(sym, date):
        df = feats.get(sym)
        if df is None:
            return None
        try:
            v = df.loc[date, "close"]
        except KeyError:
            return None
        return None if pd.isna(v) else float(v)

    for di, date in enumerate(dates):
        # Mark to market using last known price (carry forward if a symbol has a
        # gap on this exact date).
        prices_today = {s: price_at(s, date) for s in positions}

        rebalance = (di % REBALANCE_EVERY == 0)
        if rebalance:
            n_rebalances += 1
            # Score every symbol as-of this date (technicals only).
            scored = []
            for sym, df in feats.items():
                ind = _indicators_at(df, date)
                if not ind:
                    continue
                rec = strategy.decide(ind, None)
                if rec["signal"] == "BUY":
                    scored.append((rec["score"], ind.get("pct_from_high") or 0, sym, ind["price"]))
            scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)
            targets = scored[:TOP_N]
            target_syms = {t[2] for t in targets}
            regime_row = bclose[bclose.index <= pd.Timestamp(date)].tail(200)
            risk_on = True
            if len(regime_row) >= 200:
                bsma50 = float(regime_row.rolling(50).mean().iloc[-1])
                bsma200 = float(regime_row.rolling(200).mean().iloc[-1])
                bpx = float(regime_row.iloc[-1])
                risk_on = bpx >= bsma50 and bsma50 >= bsma200

            # Stop-loss exits first.
            for sym in list(positions.keys()):
                px = prices_today.get(sym) or positions[sym]["avg"]
                avg = positions[sym]["avg"]
                positions[sym]["peak"] = max(positions[sym].get("peak", px), px)
                if avg and (px <= avg * (1 - STOP_LOSS_PCT) or
                            px <= positions[sym]["peak"] * (1 - TRAILING_STOP_PCT)):
                    proceeds = positions[sym]["qty"] * px
                    cost = proceeds * COST_PER_SIDE
                    total_costs += cost
                    turnover += proceeds
                    cash += proceeds - cost
                    positions.pop(sym)
                    target_syms.discard(sym)
                    targets = [t for t in targets if t[2] != sym]
                    n_stops += 1

            # Exit names no longer targeted.
            for sym in list(positions.keys()):
                if sym not in target_syms:
                    px = prices_today.get(sym) or positions[sym]["avg"]
                    proceeds = positions[sym]["qty"] * px
                    cost = proceeds * COST_PER_SIDE
                    total_costs += cost
                    turnover += proceeds
                    cash += proceeds - cost
                    positions.pop(sym)

            # Allocate to targets with caps.
            total_value = cash + sum(
                positions[s]["qty"] * (prices_today.get(s) or positions[s]["avg"])
                for s in positions)
            if targets and risk_on:
                base_budget = total_value / len(targets)
                pos_cap = MAX_POSITION_PCT * total_value
                sector_cap = MAX_SECTOR_PCT * total_value
                sector_alloc: dict[str, float] = {}
                for s in positions:
                    sec = sectors.get(s, "Unknown")
                    px = prices_today.get(s) or positions[s]["avg"]
                    sector_alloc[sec] = sector_alloc.get(sec, 0.0) + positions[s]["qty"] * px

                for _score, _pfh, sym, px in targets:
                    if not px or px <= 0:
                        continue
                    sec = sectors.get(sym, "Unknown")
                    held_qty = positions.get(sym, {}).get("qty", 0)
                    held_val = held_qty * px
                    target_val = min(base_budget, pos_cap)
                    room = sector_cap - sector_alloc.get(sec, 0.0)
                    if room <= 0:
                        continue
                    target_val = min(target_val, held_val + room)
                    want = target_val - held_val
                    if want <= px:
                        continue
                    qty = int(want / (px * (1 + COST_PER_SIDE)))
                    qty = min(qty, int(cash / (px * (1 + COST_PER_SIDE))))
                    if qty <= 0:
                        continue
                    spend = qty * px
                    cost = spend * COST_PER_SIDE
                    total_costs += cost
                    turnover += spend
                    cash -= spend + cost
                    sector_alloc[sec] = sector_alloc.get(sec, 0.0) + spend
                    if sym in positions:
                        old = positions[sym]
                        nq = old["qty"] + qty
                        old["avg"] = (old["avg"] * old["qty"] + spend) / nq
                        old["qty"] = nq
                        old["peak"] = max(old.get("peak", px), px)
                    else:
                        positions[sym] = {"qty": qty, "avg": px, "peak": px}

        # End-of-day mark to market.
        mv = 0.0
        for s in positions:
            px = price_at(s, date) or positions[s]["avg"]
            mv += positions[s]["qty"] * px
        value = cash + mv
        if prev_value > 0:
            daily_returns.append(value / prev_value - 1)
        prev_value = value
        equity.append(round(value, 2))
        eq_dates.append(pd.Timestamp(date).strftime("%Y-%m-%d"))

    # --- Metrics ---
    start_v, end_v = START_CAPITAL, equity[-1]
    total_ret = round((end_v / start_v - 1) * 100, 2)
    years = max(len(equity) / 252.0, 1e-9)
    cagr = round(((end_v / start_v) ** (1 / years) - 1) * 100, 2)
    mdd = _max_drawdown(equity)
    sharpe = _sharpe(daily_returns)
    win_rate = round(100 * sum(1 for r in daily_returns if r > 0) / len(daily_returns), 1) \
        if daily_returns else 0.0
    half = max(1, len(equity) // 2)
    first_half_ret = round((equity[half - 1] / START_CAPITAL - 1) * 100, 2)
    second_half_ret = round((equity[-1] / equity[half - 1] - 1) * 100, 2) if equity[half - 1] else 0.0
    turnover_pct = round(turnover / max(START_CAPITAL, 1) * 100, 2)

    # Benchmark buy & hold over the same window.
    bench_ret = None
    bench_curve = []
    if not bclose.empty:
        bwin = bclose[bclose.index >= pd.Timestamp(dates[0])]
        if len(bwin) > 1:
            b0 = float(bwin.iloc[0])
            bench_ret = round((float(bwin.iloc[-1]) / b0 - 1) * 100, 2)
            # Sample the benchmark equity (normalised to START_CAPITAL) lightly.
            step = max(1, len(bwin) // 250)
            for i in range(0, len(bwin), step):
                bench_curve.append({
                    "date": bwin.index[i].strftime("%Y-%m-%d"),
                    "value": round(START_CAPITAL * float(bwin.iloc[i]) / b0, 2),
                })
    alpha = round(total_ret - bench_ret, 2) if bench_ret is not None else None

    # Down-sample the strategy equity curve for a compact dashboard payload.
    step = max(1, len(equity) // 250)
    curve = [{"date": eq_dates[i], "value": equity[i]}
             for i in range(0, len(equity), step)]
    if curve and curve[-1]["date"] != eq_dates[-1]:
        curve.append({"date": eq_dates[-1], "value": equity[-1]})

    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "lookback": LOOKBACK,
        "rebalance_every_days": REBALANCE_EVERY,
        "start_capital": START_CAPITAL,
        "start_date": eq_dates[0],
        "end_date": eq_dates[-1],
        "trading_days": len(equity),
        "n_rebalances": n_rebalances,
        "n_stops": n_stops,
        "costs": round(total_costs, 2),
        "turnover_pct_of_start": turnover_pct,
        "final_value": end_v,
        "total_return_pct": total_ret,
        "cagr_pct": cagr,
        "benchmark_name": "NIFTY 50",
        "benchmark_return_pct": bench_ret,
        "alpha_pct": alpha,
        "max_drawdown_pct": mdd,
        "sharpe": sharpe,
        "win_rate_pct": win_rate,
        "params": {
            **strategy_metadata(),
            "rebalance_every_days": REBALANCE_EVERY,
            "lookback": LOOKBACK,
        },
        "validation": {
            "first_half_return_pct": first_half_ret,
            "second_half_return_pct": second_half_ret,
            "out_of_sample_warning": "single-history replay; not a true train/test optimization",
            "bias_warnings": [
                "today's universe only (survivorship bias)",
                "news sentiment excluded historically",
                "daily close fills; intraday delayed data not modeled",
            ],
        },
        "equity_curve": curve,
        "benchmark_curve": bench_curve,
        "note": ("Technicals-only historical replay (news excluded). Today's "
                 "universe (survivorship bias). Fills at daily close. Past "
                 "performance does not guarantee future results."),
    }
    ds.write_json("backtest/latest.json", payload)
    print(f"  total {total_ret:+.2f}% | CAGR {cagr:+.2f}% | "
          f"NIFTY {bench_ret:+.2f}% | alpha {alpha:+.2f}% | "
          f"maxDD {mdd:.2f}% | Sharpe {sharpe} | win {win_rate}% | "
          f"{len(equity)} days")
    return payload


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
