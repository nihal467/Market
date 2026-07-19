"""Backtester: replay the paper-trading strategy over historical data.

Answers the only question that matters before risking real money: *would this
strategy have beaten simply holding the index, and how bumpy would the ride
have been?*

What it does:
  - Downloads ~LOOKBACK of daily history for the whole NSE universe (batched
    yfinance calls with retry/backoff) plus the NIFTY 50 benchmark.
  - Pre-computes rolling indicators (SMA50, SMA200, RSI14, 52w high/low) for
    the entire series, so scoring at each historical date is cheap.
  - Walks forward day by day with NEXT-OPEN execution: signals computed from
    day T's close are queued and FILLED at day T+1's adjusted open (no
    look-ahead). If T+1 has no bar for a symbol, that order is dropped. Fills
    pay SLIPPAGE_BPS against the trade plus COST_BPS_PER_SIDE on notional.
    The book is still marked to market at daily closes.
  - The HEADLINE run replays the ACTIVE_PROFILE from strategy_config (same
    score, cadence, hold band, caps and regime guard as the live paper
    trader), so the headline reflects the strategy actually running. The
    strategy-lab variants replay the same history with alternative knobs and
    use the identical next-open execution so results stay comparable.
  - Produces an equity curve, headline metrics (total return, CAGR, alpha vs
    NIFTY, max drawdown, Sharpe, positive-day rate, cost drag) and a simple
    60/40 train/validation split with out-of-sample alpha per variant.

Caveats (stated honestly):
  - News sentiment is excluded from the headline run (RSS is current-only), so
    live results can differ. The news-ablation pair replays the active profile
    with the DATED news archive (news/ on the data branch) enabled vs zeroed —
    but that archive only covers dates after it was switched on.
  - Survivorship: the universe is today's list; delisted names aren't included.
  - Single-history replay: the 60/40 split is a sanity check, not a true
    walk-forward optimization.

Writes backtest/latest.json to the data store for the dashboard.
"""
from __future__ import annotations

import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

import datastore as ds
import strategy
from market_calendar import now_ist
from news_ablation import build_news_ablation
from universe import load_universe

# Same knobs as the live bot so the backtest reflects real behaviour.
from strategy_config import (
    ACTIVE_PROFILE,
    COST_BPS_PER_SIDE,
    EXECUTION_MODEL,
    HOLD_UNTIL_RANK,
    MAX_POSITION_PCT,
    MAX_SECTOR_PCT,
    REBALANCE_INTERVAL,
    SLIPPAGE_BPS,
    START_CAPITAL,
    STOP_LOSS_PCT,
    TOP_N,
    TRAILING_STOP_PCT,
    USE_MARKET_REGIME_GUARD,
    strategy_metadata,
)

BENCHMARK = "^NSEI"
LOOKBACK = "2y"
# Headline cadence mirrors the live profile (weekly => every 5 trading days).
REBALANCE_EVERY = 5 if REBALANCE_INTERVAL == "weekly" else 1
WARMUP = 200            # need ~200 bars before SMA200 is meaningful
BATCH = 50
DOWNLOAD_RETRIES = 3
# Below this fraction of symbols with data the whole backtest aborts loudly —
# metrics from a half-fetched universe would be silently wrong.
MIN_COVERAGE = 0.8
# First 60% of trading days = train window, last 40% = validation window.
TRAIN_FRACTION = 0.6
# Walk-forward validation: after an initial training chunk, the remaining days
# are split into this many sequential validation folds (expanding window: each
# fold's "train" is everything before it).
WALK_FORWARD_FOLDS = 3

# Which lab score mode each live profile corresponds to (for the headline run).
PROFILE_SCORE_MODES = {
    "momentum_weekly_churn_control": "momentum",
}

LAB_VARIANTS = [
    {"id": "current_daily", "label": "Full score, daily rebalance, regime filter",
     "score": "full", "rebalance_every": 1, "use_regime": True, "hold_until_rank": TOP_N},
    {"id": "weekly_churn_control", "label": "Full score, weekly rebalance, hold until rank 20",
     "score": "full", "rebalance_every": 5, "use_regime": True, "hold_until_rank": 20},
    {"id": "no_regime_filter", "label": "Full score, daily rebalance, no regime filter",
     "score": "full", "rebalance_every": 1, "use_regime": False, "hold_until_rank": TOP_N},
    {"id": "momentum_only_weekly", "label": "3M momentum only, weekly rebalance",
     "score": "momentum", "rebalance_every": 5, "use_regime": True, "hold_until_rank": 20},
    {"id": "trend_only_weekly", "label": "SMA trend only, weekly rebalance",
     "score": "trend", "rebalance_every": 5, "use_regime": True, "hold_until_rank": 20},
    {"id": "rsi_only_weekly", "label": "RSI pullback only, weekly rebalance",
     "score": "rsi", "rebalance_every": 5, "use_regime": True, "hold_until_rank": 20},
]


def _active_variant() -> dict:
    """The live paper trader's configuration expressed as a lab variant."""
    return {
        "id": ACTIVE_PROFILE,
        "label": f"Active profile: {ACTIVE_PROFILE}",
        "score": PROFILE_SCORE_MODES.get(ACTIVE_PROFILE, "full"),
        "rebalance_every": REBALANCE_EVERY,
        "use_regime": USE_MARKET_REGIME_GUARD,
        "hold_until_rank": HOLD_UNTIL_RANK,
    }


def _news_ablation_pair() -> list[dict]:
    """Matched pair for the ACTIVE profile: news signal enabled vs zeroed.

    Both variants are byte-identical to the active configuration except for
    ``use_news``: the first scores with archived daily news sentiment where a
    news/YYYY/MM/DD.json file exists (zero contribution on uncovered dates),
    the second zeroes the news contribution everywhere. Returned in
    [with_news, without_news] order.
    """
    base = _active_variant()
    return [
        {**base, "id": f"{base['id']}__news_on",
         "label": "Active profile + archived news sentiment",
         "use_news": True},
        {**base, "id": f"{base['id']}__news_off",
         "label": "Active profile, news contribution zeroed",
         "use_news": False},
    ]


def _download_batch(chunk: list[str]) -> pd.DataFrame | None:
    """One batched download with retry + exponential backoff (2s/4s/8s)."""
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            data = yf.download(chunk, period=LOOKBACK, interval="1d",
                               auto_adjust=True, progress=False,
                               group_by="ticker", threads=True)
            if data is not None and not data.empty:
                return data
        except Exception as exc:  # noqa: BLE001
            print(f"  ! download attempt {attempt}/{DOWNLOAD_RETRIES} failed: {exc}")
        time.sleep(2 ** attempt)
    return None


def _download(symbols: list[str]) -> tuple[dict[str, pd.DataFrame], int]:
    """Return ({symbol: open/close frame with >= WARMUP bars}, n_with_any_data).

    Opens are kept alongside closes because fills happen at the NEXT session's
    adjusted open. auto_adjust=True gives adjusted opens on the same basis as
    the adjusted closes used for signals and marking.
    """
    out: dict[str, pd.DataFrame] = {}
    n_with_data = 0
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        print(f"  downloading {i + 1}-{i + len(chunk)} of {len(symbols)} ...")
        data = _download_batch(chunk)
        if data is None:
            continue
        for sym in chunk:
            try:
                bars = data if len(chunk) == 1 else data[sym]
                bars = bars[["Open", "Close"]].dropna(subset=["Close"])
            except (KeyError, TypeError):
                continue
            if bars.empty:
                continue
            n_with_data += 1
            if len(bars) >= WARMUP:
                out[sym] = pd.DataFrame({"open": bars["Open"], "close": bars["Close"]})
    return out, n_with_data


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(loss != 0, 100.0)      # all-gain window -> 100
    rsi = rsi.where(gain != 0, 50.0)       # flat window -> neutral-ish
    return rsi


def _precompute(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Per-symbol DataFrame of open/close + rolling indicators, by date."""
    feats: dict[str, pd.DataFrame] = {}
    for sym, bars in frames.items():
        close = bars["close"]
        df = pd.DataFrame({"close": close, "open": bars["open"]})
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


def _regime_on(bclose: pd.Series, date) -> bool:
    if bclose.empty:
        return True
    regime_row = bclose[bclose.index <= pd.Timestamp(date)].tail(200)
    if len(regime_row) < 200:
        return True
    sma50 = float(regime_row.rolling(50).mean().iloc[-1])
    sma200 = float(regime_row.rolling(200).mean().iloc[-1])
    price = float(regime_row.iloc[-1])
    return price >= sma50 and sma50 >= sma200


def _variant_score(ind: dict, mode: str, sentiment: dict | None = None) -> float:
    """Score one candidate for a variant.

    ``sentiment`` ({'score','count'}, from the dated news archive) is only
    passed for use_news variants: "full" mode feeds it straight into
    strategy.decide, every other mode adds the SAME graded news term the live
    scorer uses (W_NEWS x sentiment x confidence), so the news-ablation pair
    differs by exactly that term and nothing else. Default None keeps every
    existing variant byte-identical to before.
    """
    if mode == "full":
        rec = strategy.decide(ind, sentiment)
        return float(rec["score"]) if rec["signal"] == "BUY" else -999.0
    if mode == "momentum":
        score = float(ind.get("ret_3m") or -999.0)
    elif mode == "trend":
        price, sma50, sma200 = ind.get("price"), ind.get("sma50"), ind.get("sma200")
        if price is None or sma50 is None:
            return -999.0
        score = 0.0
        score += 1.0 if price > sma50 else -1.0
        if sma200 is not None:
            score += 1.0 if sma50 > sma200 else -1.0
    elif mode == "rsi":
        rsi = ind.get("rsi14")
        if rsi is None:
            return -999.0
        # Prefer constructive pullbacks, not panic lows or overheated names.
        score = 70.0 - abs(45.0 - rsi) if 25 <= rsi <= 65 else -999.0
    else:
        return -999.0
    if sentiment and score > -999.0:
        score += strategy._news_component(sentiment)[0]
    return score


def _load_archived_news(dates: list) -> dict[str, dict]:
    """{'YYYY-MM-DD': {symbol: {'score','count'}}} from the dated news archive.

    Reads news/YYYY/MM/DD.json (written daily by news_archive.py) for each
    replay date, exact-date match only. The data branch starts empty and the
    archive only grows going forward, so missing days are normal — they simply
    contribute no news signal to the with-news ablation run.
    """
    out: dict[str, dict] = {}
    for date in dates:
        ts = pd.Timestamp(date)
        payload = ds.read_json(ds.news_path(ts), default=None)
        if not payload:
            continue
        day = {}
        for row in payload.get("symbols", []) or []:
            sym = row.get("symbol")
            if not sym:
                continue
            day[sym] = {"score": row.get("score"),
                        "count": row.get("n_headlines") or 0}
        if day:
            out[ts.strftime("%Y-%m-%d")] = day
    return out


def _bench_level_at(bclose: pd.Series, date_str: str) -> float | None:
    if bclose.empty:
        return None
    win = bclose[bclose.index <= pd.Timestamp(date_str)]
    return float(win.iloc[-1]) if len(win) else None


def _walk_forward(equity: list[float], eq_dates: list[str],
                  bclose: pd.Series) -> dict | None:
    """Rolling walk-forward validation over the single replayed history.

    The full window is cut into WALK_FORWARD_FOLDS + 1 equal chunks: the first
    chunk is the initial training window and each remaining chunk is a
    sequential validation fold. The training window EXPANDS: fold k validates
    on chunk k+1 with all prior days as its train period. There is no
    per-fold re-fitting (the strategy has no fitted parameters); this measures
    whether out-of-sample alpha holds across sub-periods rather than being
    concentrated in one lucky stretch.
    """
    n = len(equity)
    # Need at least ~5 days per chunk for the fold returns to mean anything.
    if n < (WALK_FORWARD_FOLDS + 1) * 5:
        return None
    edges = [int(n * i / (WALK_FORWARD_FOLDS + 1)) for i in range(WALK_FORWARD_FOLDS + 2)]
    folds = []
    for k in range(WALK_FORWARD_FOLDS):
        start, end = edges[k + 1], edges[k + 2]      # fold = [start, end)
        anchor = start - 1                            # last day of its train window
        strat_ret = (equity[end - 1] / equity[anchor] - 1) * 100 if equity[anchor] else 0.0
        b0 = _bench_level_at(bclose, eq_dates[anchor])
        b1 = _bench_level_at(bclose, eq_dates[end - 1])
        bench_ret = round((b1 / b0 - 1) * 100, 2) if b0 and b1 else None
        folds.append({
            "fold": k + 1,
            "train_days": start,
            "validation_days": end - start,
            "validation_start": eq_dates[start],
            "validation_end": eq_dates[end - 1],
            "validation_return_pct": round(strat_ret, 2),
            "validation_benchmark_return_pct": bench_ret,
            "validation_alpha_pct": round(strat_ret - bench_ret, 2)
            if bench_ret is not None else None,
        })
    alphas = [f["validation_alpha_pct"] for f in folds
              if f["validation_alpha_pct"] is not None]
    return {
        "n_folds": len(folds),
        "expanding_window": True,
        "folds": folds,
        "mean_validation_alpha_pct": round(sum(alphas) / len(alphas), 2) if alphas else None,
        "folds_positive": sum(1 for a in alphas if a > 0),
        "note": ("Expanding-window walk-forward over one replayed history; "
                 "no per-fold re-optimization (the strategy has no fitted parameters)."),
    }


def _validation_split(equity: list[float], eq_dates: list[str],
                      bclose: pd.Series) -> dict | None:
    """60/40 train/validation split plus walk-forward folds of the replay."""
    if len(equity) < 10:
        return None
    split = max(1, min(len(equity) - 1, int(len(equity) * TRAIN_FRACTION)))
    train_ret = (equity[split - 1] / START_CAPITAL - 1) * 100
    val_ret = (equity[-1] / equity[split - 1] - 1) * 100 if equity[split - 1] else 0.0
    b0 = _bench_level_at(bclose, eq_dates[0])
    bs = _bench_level_at(bclose, eq_dates[split - 1])
    be = _bench_level_at(bclose, eq_dates[-1])
    train_bench = round((bs / b0 - 1) * 100, 2) if b0 and bs else None
    val_bench = round((be / bs - 1) * 100, 2) if bs and be else None
    return {
        "train_fraction": TRAIN_FRACTION,
        "split_date": eq_dates[split - 1],
        "train_days": split,
        "validation_days": len(equity) - split,
        "train_return_pct": round(train_ret, 2),
        "train_benchmark_return_pct": train_bench,
        "train_alpha_pct": round(train_ret - train_bench, 2) if train_bench is not None else None,
        "validation_return_pct": round(val_ret, 2),
        "validation_benchmark_return_pct": val_bench,
        "validation_alpha_pct": round(val_ret - val_bench, 2) if val_bench is not None else None,
        "walk_forward": _walk_forward(equity, eq_dates, bclose),
    }


def _metrics(equity: list[float], daily_returns: list[float], costs: float,
             turnover: float, stops: int, rebalances: int) -> dict:
    if not equity:
        return {"skipped": True}
    start_v, end_v = START_CAPITAL, equity[-1]
    total_ret = round((end_v / start_v - 1) * 100, 2)
    years = max(len(equity) / 252.0, 1e-9)
    cagr = round(((end_v / start_v) ** (1 / years) - 1) * 100, 2)
    win_rate = round(100 * sum(1 for r in daily_returns if r > 0) / len(daily_returns), 1) \
        if daily_returns else 0.0
    return {
        "final_value": round(end_v, 2),
        "total_return_pct": total_ret,
        "cagr_pct": cagr,
        "max_drawdown_pct": _max_drawdown(equity),
        "sharpe": _sharpe(daily_returns),
        "win_rate_pct": win_rate,
        "costs": round(costs, 2),
        "total_costs": round(costs, 2),
        "turnover_pct_of_start": round(turnover / max(START_CAPITAL, 1) * 100, 2),
        "n_stops": stops,
        "n_rebalances": rebalances,
    }


def _simulate_variant(feats: dict[str, pd.DataFrame], dates: list, bclose: pd.Series,
                      sectors: dict[str, str], variant: dict,
                      news_by_date: dict | None = None) -> dict:
    """Walk-forward replay with NEXT-OPEN execution.

    Day T: (a) fill orders queued at T-1's close at T's adjusted open (sells
    first; unpriceable orders are dropped), (b) decide from T's close — daily
    stop checks plus, on cadence days, the rebalance — queueing orders for
    T+1, (c) mark the book at T's close. Orders queued on the final day never
    fill (there is no T+1), exactly like live.

    ``news_by_date`` ({'YYYY-MM-DD': {symbol: {'score','count'}}}, from the
    dated news archive) is consulted only when the variant sets ``use_news`` —
    dates or symbols without archived news simply contribute no news signal.
    """
    cash = START_CAPITAL
    positions: dict[str, dict] = {}     # sym -> {qty, avg, peak}
    pending: list[dict] = []            # queued at prev close, fill at today's open
    equity: list[float] = []
    eq_dates: list[str] = []
    daily_returns: list[float] = []
    prev_value = START_CAPITAL
    costs = 0.0
    turnover = 0.0
    stops = 0
    rebalances = 0
    dropped = 0
    news_days_used = 0
    slip = SLIPPAGE_BPS / 10000.0
    cost_rate = COST_BPS_PER_SIDE / 10000.0

    def bar_at(sym, date, col):
        df = feats.get(sym)
        if df is None:
            return None
        try:
            v = df.loc[date, col]
        except KeyError:
            return None
        return None if pd.isna(v) else float(v)

    for di, date in enumerate(dates):
        # --- (a) Fill yesterday's queue at TODAY's open (sells first). ---
        if pending:
            for order in sorted(pending, key=lambda o: 0 if o["action"] == "SELL" else 1):
                sym = order["sym"]
                open_px = bar_at(sym, date, "open")
                if not open_px or open_px <= 0:
                    dropped += 1
                    continue
                if order["action"] == "SELL":
                    pos = positions.get(sym)
                    if not pos:
                        dropped += 1
                        continue
                    fill = open_px * (1 - slip)
                    proceeds = pos["qty"] * fill
                    cost = proceeds * cost_rate
                    costs += cost
                    turnover += proceeds
                    cash += proceeds - cost
                    positions.pop(sym)
                    if order.get("reason") == "stop":
                        stops += 1
                else:
                    fill = open_px * (1 + slip)
                    denom = fill * (1 + cost_rate)
                    qty = min(int(order["budget"] / denom), int(cash / denom))
                    if qty <= 0:
                        dropped += 1
                        continue
                    spend = qty * fill
                    cost = spend * cost_rate
                    costs += cost
                    turnover += spend
                    cash -= spend + cost
                    if sym in positions:
                        old = positions[sym]
                        nq = old["qty"] + qty
                        old["avg"] = (old["avg"] * old["qty"] + spend) / nq
                        old["qty"] = nq
                        old["peak"] = max(old.get("peak", fill), fill)
                    else:
                        positions[sym] = {"qty": qty, "avg": fill, "peak": fill}
            pending = []

        # --- (b) Decide from TODAY's close; queue orders for the next open. ---
        new_orders: list[dict] = []
        queued_sells: set[str] = set()
        # Daily protective stops (mirrors the live bot's EOD stop check).
        for sym in list(positions.keys()):
            px = bar_at(sym, date, "close") or positions[sym]["avg"]
            positions[sym]["peak"] = max(positions[sym].get("peak", px), px)
            avg = positions[sym]["avg"]
            if avg and (px <= avg * (1 - STOP_LOSS_PCT) or
                        px <= positions[sym]["peak"] * (1 - TRAILING_STOP_PCT)):
                new_orders.append({"action": "SELL", "sym": sym, "reason": "stop"})
                queued_sells.add(sym)

        if di % int(variant["rebalance_every"]) == 0:
            rebalances += 1
            day_news: dict | None = None
            if variant.get("use_news") and news_by_date:
                day_news = news_by_date.get(pd.Timestamp(date).strftime("%Y-%m-%d"))
                if day_news:
                    news_days_used += 1
            ranked = []
            for sym, df in feats.items():
                ind = _indicators_at(df, date)
                if not ind:
                    continue
                score = _variant_score(ind, variant["score"],
                                       (day_news or {}).get(sym))
                if score > 0:
                    ranked.append((score, ind.get("pct_from_high") or 0, sym, ind["price"]))
            ranked.sort(key=lambda x: (x[0], -x[1]), reverse=True)
            ranks = {sym: i + 1 for i, (_score, _pfh, sym, _px) in enumerate(ranked)}
            top = [t for t in ranked[:TOP_N] if t[2] not in queued_sells]
            target_syms = {t[2] for t in top}
            hold_until = int(variant.get("hold_until_rank") or TOP_N)
            for sym in list(positions.keys()):
                if sym in queued_sells:
                    continue
                if ranks.get(sym, 10**9) <= hold_until:
                    target_syms.add(sym)
                    if all(t[2] != sym for t in top):
                        px = bar_at(sym, date, "close") or positions[sym]["avg"]
                        top.append((0.01, 0, sym, px))

            # Queue exits for holdings that dropped out of the hold band.
            for sym in list(positions.keys()):
                if sym not in target_syms and sym not in queued_sells:
                    new_orders.append({"action": "SELL", "sym": sym, "reason": "exit"})
                    queued_sells.add(sym)

            total_value = cash + sum(
                positions[s]["qty"] * (bar_at(s, date, "close") or positions[s]["avg"])
                for s in positions)
            risk_on = _regime_on(bclose, date) if variant.get("use_regime") else True
            if top and risk_on:
                base_budget = total_value / max(len(top), 1)
                pos_cap = MAX_POSITION_PCT * total_value
                sector_cap = MAX_SECTOR_PCT * total_value
                # Queued sells free their value for tomorrow's buys; track an
                # estimated cash line so the queue stays fundable in rank
                # order (fills are hard-capped by actual cash anyway).
                est_cash = cash
                sector_alloc: dict[str, float] = {}
                for s in positions:
                    px = bar_at(s, date, "close") or positions[s]["avg"]
                    if s in queued_sells:
                        est_cash += positions[s]["qty"] * px
                        continue
                    sec = sectors.get(s, "Unknown")
                    sector_alloc[sec] = sector_alloc.get(sec, 0.0) + positions[s]["qty"] * px
                for _score, _pfh, sym, px in top[:TOP_N]:
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
                    want = min(target_val - held_val, est_cash)
                    if want <= px:
                        continue
                    est_cash -= want
                    sector_alloc[sec] = sector_alloc.get(sec, 0.0) + want
                    new_orders.append({"action": "BUY", "sym": sym, "budget": want,
                                       "reason": "rebalance"})
        pending = new_orders

        # --- (c) Mark the book at TODAY's close. ---
        value = cash
        for sym, pos in positions.items():
            value += pos["qty"] * (bar_at(sym, date, "close") or pos["avg"])
        if prev_value > 0:
            daily_returns.append(value / prev_value - 1)
        prev_value = value
        equity.append(round(value, 2))
        eq_dates.append(pd.Timestamp(date).strftime("%Y-%m-%d"))

    out = _metrics(equity, daily_returns, costs, turnover, stops, rebalances)
    out.update({
        "id": variant["id"],
        "label": variant["label"],
        "score_mode": variant["score"],
        "rebalance_every_days": variant["rebalance_every"],
        "use_regime": bool(variant.get("use_regime")),
        "hold_until_rank": variant.get("hold_until_rank"),
        "use_news": bool(variant.get("use_news")),
        "news_days_used": news_days_used,
        "execution_model": EXECUTION_MODEL,
        "n_orders_dropped": dropped,
        "validation": _validation_split(equity, eq_dates, bclose),
        "_equity": equity,
        "_eq_dates": eq_dates,
    })
    return out


def run() -> dict:
    ist = now_ist()
    uni = load_universe()
    sectors = {u["symbol"]: u.get("sector") or "Unknown" for u in uni}
    symbols = [u["symbol"] for u in uni]
    print(f"Backtest over {len(symbols)} symbols, lookback {LOOKBACK} ...")

    frames, n_with_data = _download(symbols)
    coverage = n_with_data / len(symbols) if symbols else 0.0
    if coverage < MIN_COVERAGE:
        raise RuntimeError(
            f"Backtest aborted: only {n_with_data}/{len(symbols)} symbols "
            f"({coverage * 100:.1f}%) returned price history — below the "
            f"{MIN_COVERAGE * 100:.0f}% coverage floor. Refusing to compute "
            "results from a partially fetched universe.")
    if not frames:
        print("No symbol has enough history after warmup — aborting backtest.")
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

    feats = _precompute(frames)

    # Master trading calendar = union of all dates, sorted; start after warmup.
    all_dates = sorted(set().union(*[df.index for df in feats.values()]))
    dates = [d for d in all_dates][WARMUP:]
    if len(dates) < 30:
        print("Not enough history after warmup — aborting.")
        return {"skipped": True}

    # Dated news archive (data branch) for the news-ablation pair. Empty until
    # news_archive.py has been running for a while — handled gracefully.
    news_by_date = _load_archived_news(dates)
    print(f"  archived news available for {len(news_by_date)}/{len(dates)} replay days")

    # HEADLINE: replay the ACTIVE_PROFILE — the exact configuration the live
    # paper trader runs — with next-open execution.
    active = _simulate_variant(feats, dates, bclose, sectors, _active_variant())
    equity = active.pop("_equity")
    eq_dates = active.pop("_eq_dates")

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
    total_ret = active["total_return_pct"]
    alpha = round(total_ret - bench_ret, 2) if bench_ret is not None else None

    # Secondary: the strategy lab, same history + same next-open execution,
    # plus the matched news-ablation pair for the active profile.
    ablation_pair = _news_ablation_pair()
    lab_variants = []
    for v in LAB_VARIANTS + ablation_pair:
        row = _simulate_variant(feats, dates, bclose, sectors, v, news_by_date)
        row.pop("_equity", None)
        row.pop("_eq_dates", None)
        row["benchmark_return_pct"] = bench_ret
        row["alpha_pct"] = round(row["total_return_pct"] - bench_ret, 2) \
            if bench_ret is not None and not row.get("skipped") else None
        lab_variants.append(row)
    rows_by_id = {r.get("id"): r for r in lab_variants}
    news_ablation = build_news_ablation(rows_by_id.get(ablation_pair[0]["id"]),
                                        rows_by_id.get(ablation_pair[1]["id"]))
    lab_variants.sort(key=lambda r: (r.get("alpha_pct") is not None,
                                     r.get("total_return_pct", -999)), reverse=True)

    # Down-sample the strategy equity curve for a compact dashboard payload.
    step = max(1, len(equity) // 250)
    curve = [{"date": eq_dates[i], "value": equity[i]}
             for i in range(0, len(equity), step)]
    if curve and curve[-1]["date"] != eq_dates[-1]:
        curve.append({"date": eq_dates[-1], "value": equity[-1]})

    validation = active.get("validation") or {}
    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "lookback": LOOKBACK,
        "rebalance_every_days": REBALANCE_EVERY,
        "active_profile": ACTIVE_PROFILE,
        "execution_model": EXECUTION_MODEL,
        "coverage_pct": round(coverage * 100, 2),
        "start_capital": START_CAPITAL,
        "start_date": eq_dates[0],
        "end_date": eq_dates[-1],
        "trading_days": len(equity),
        "n_rebalances": active["n_rebalances"],
        "n_stops": active["n_stops"],
        "n_orders_dropped": active["n_orders_dropped"],
        "costs": active["costs"],
        "total_costs": active["total_costs"],
        "turnover_pct_of_start": active["turnover_pct_of_start"],
        "final_value": active["final_value"],
        "total_return_pct": total_ret,
        "cagr_pct": active["cagr_pct"],
        "benchmark_name": "NIFTY 50",
        "benchmark_return_pct": bench_ret,
        "alpha_pct": alpha,
        "max_drawdown_pct": active["max_drawdown_pct"],
        "sharpe": active["sharpe"],
        "win_rate_pct": active["win_rate_pct"],
        "params": {
            **strategy_metadata(),
            "rebalance_every_days": REBALANCE_EVERY,
            "lookback": LOOKBACK,
        },
        "validation": {
            **validation,
            "out_of_sample_warning": "single-history replay; not a true train/test optimization",
            "bias_warnings": [
                "today's universe only (survivorship bias)",
                "news sentiment excluded historically",
                "signals at close fill at next session's adjusted open "
                f"(+{SLIPPAGE_BPS} bps slippage, {COST_BPS_PER_SIDE} bps costs per side)",
            ],
        },
        "strategy_lab": {
            "description": ("Same downloaded history replayed with signal/churn "
                            "variants under identical next-open execution. News is "
                            "excluded everywhere except the *_news_on ablation "
                            "variant, which uses the dated news archive where it exists."),
            "variants": lab_variants,
            "takeaway": "Prefer variants that beat benchmark after costs with lower turnover and drawdown.",
        },
        "news_ablation": news_ablation,
        "equity_curve": curve,
        "benchmark_curve": bench_curve,
        "note": ("Technicals-only historical replay for the headline run (news "
                 "excluded; see news_ablation for the with/without-news study). "
                 "Today's universe (survivorship bias). Signals from the close "
                 "fill at the NEXT session's adjusted open with slippage + "
                 "costs. Past performance does not guarantee future results."),
    }
    ds.write_json("backtest/latest.json", payload)
    print(f"  news ablation: {news_ablation.get('verdict')} "
          f"(delta {news_ablation.get('delta_validation_alpha_pct')} pp on "
          f"{news_ablation.get('news_days_used')} archived news days)")
    val_alpha = validation.get("validation_alpha_pct")
    bench_txt = f"{bench_ret:+.2f}%" if bench_ret is not None else "n/a"
    alpha_txt = f"{alpha:+.2f}%" if alpha is not None else "n/a"
    print(f"  [{ACTIVE_PROFILE}] total {total_ret:+.2f}% | CAGR {active['cagr_pct']:+.2f}% | "
          f"NIFTY {bench_txt} | alpha {alpha_txt} | "
          f"maxDD {active['max_drawdown_pct']:.2f}% | Sharpe {active['sharpe']} | "
          f"win {active['win_rate_pct']}% | "
          f"OOS alpha {val_alpha if val_alpha is not None else 'n/a'}% | "
          f"{len(equity)} days")
    return payload


if __name__ == "__main__":
    result = run()
    if result.get("skipped"):
        sys.exit(0)
