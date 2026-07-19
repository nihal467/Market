"""Daily news archive: persist per-symbol headlines + lexicon scores.

Why: Google News RSS is current-only, so the backtest cannot study whether the
news signal adds alpha unless a DATED history of that signal exists. This
advisory job snapshots, once per run day, the same headlines + lexicon scores
the live pipeline consumes, per current watchlist symbol, to the data branch:

    news/YYYY/MM/DD.json   # this run day's snapshot
    news/latest.json       # newest snapshot

The backtest's news-ablation pair replays these files, and lexicon_version
lets future signal-quality studies segment by scorer version.

Design notes (honest):
  - daily_analysis.py fetches sentiment per symbol but persists only the
    aggregate news_score (no headlines / per-headline scores), so its output
    cannot feed this archive — we re-fetch via the same news_sentiment module
    instead of changing the daily payload shape.
  - The aggregate ``score`` per symbol is the mean of the ARCHIVED headline
    scores, so it is exactly reproducible from the file itself.
  - Tolerant by construction: per-symbol failures are skipped with a note,
    and an empty result still writes a valid file. Advisory only — wired with
    continue-on-error so it can never break the trading path.
  - Idempotent per date by deterministic overwrite: a same-date rerun simply
    replaces the day's snapshot with the freshest fetch (the archive has no
    double-run hazard, unlike the paper trader).
"""
from __future__ import annotations

import sys

import datastore as ds
from market_calendar import now_ist
from news_sentiment import LEXICON_VERSION, score_headlines
from strategy import NEWS_CONF_FULL
from universe import load_universe

MAX_SYMBOLS = 50                # cap, incl. the universe fallback
MAX_HEADLINES_PER_SYMBOL = 10
MAX_TITLE_CHARS = 200


def _symbols_to_archive() -> tuple[list[dict], str]:
    """[{symbol, name}, ...] to archive plus the source used.

    Prefers the current weekly watchlist; falls back to the first MAX_SYMBOLS
    universe names when the data branch has no watchlist yet (it starts
    empty). Returns ([], "none") only when both sources are unavailable.
    """
    wl = ds.read_json("watchlist/latest.json", default={}) or {}
    rows = [r for r in wl.get("watchlist", []) if r.get("symbol")]
    if rows:
        return ([{"symbol": r["symbol"], "name": r.get("name") or r["symbol"]}
                 for r in rows[:MAX_SYMBOLS]], "watchlist")
    try:
        uni = load_universe()
    except Exception as exc:  # noqa: BLE001 — advisory job, never hard-fail here
        print(f"  ! universe fallback failed: {exc}")
        return [], "none"
    return ([{"symbol": u["symbol"], "name": u.get("name") or u["symbol"]}
             for u in uni[:MAX_SYMBOLS]], "universe_fallback")


def _archive_symbol(symbol: str, name: str) -> dict:
    """Fetch + score headlines for one symbol (same query the pipeline uses)."""
    items = score_headlines(f"{name} share price NSE",
                            max_items=MAX_HEADLINES_PER_SYMBOL)
    headlines = [{
        "title": (item.get("title") or "")[:MAX_TITLE_CHARS],
        "published": item.get("published_at"),
        "score": item.get("score", 0.0),
    } for item in items[:MAX_HEADLINES_PER_SYMBOL]]
    scores = [h["score"] for h in headlines if h["score"] is not None]
    return {
        "symbol": symbol,
        "name": name,
        "score": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "confidence": round(min(1.0, len(headlines) / NEWS_CONF_FULL), 3),
        "n_headlines": len(headlines),
        "headlines": headlines,
    }


def run() -> dict:
    ist = now_ist()
    targets, source = _symbols_to_archive()
    print(f"News archive for {len(targets)} symbols (source: {source}) ...")

    results: list[dict] = []
    failures: list[dict] = []
    for t in targets:
        try:
            results.append(_archive_symbol(t["symbol"], t["name"]))
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the run
            failures.append({"symbol": t["symbol"], "error": str(exc)[:200]})
            print(f"  ! skipped {t['symbol']}: {exc}")

    note = (
        f"Advisory daily snapshot of the headlines + lexicon sentiment the "
        f"pipeline consumes (Google News RSS, current-only). Symbol source: "
        f"{source}. Max {MAX_HEADLINES_PER_SYMBOL} headlines/symbol, titles "
        f"truncated to {MAX_TITLE_CHARS} chars; per-symbol score is the mean "
        f"of the archived headline scores."
    )
    if failures:
        note += f" {len(failures)} symbol(s) skipped after fetch/score errors."
    if not targets:
        note += " No watchlist or universe available — nothing to archive."

    payload = {
        "generated_at": ds.now_utc().isoformat(),
        "ist": ist.isoformat(),
        "date": ist.strftime("%Y-%m-%d"),
        "lexicon_version": LEXICON_VERSION,
        "source": source,
        "n_symbols": len(results),
        "n_failed": len(failures),
        "failures": failures,
        "symbols": results,
        "note": note,
    }
    ds.write_json(ds.news_path(ist), payload)
    ds.write_json("news/latest.json", payload)
    with_news = sum(1 for r in results if r["n_headlines"])
    print(f"  archived {len(results)} symbols ({with_news} with headlines, "
          f"{len(failures)} failed) -> {ds.news_path(ist)}")
    return payload


if __name__ == "__main__":
    result = run()
    if not result.get("symbols") and not result.get("failures"):
        # Nothing archived (empty data branch and no universe) — advisory
        # job, exit clean so the trading workflow is never disturbed.
        sys.exit(0)
