"""Fetch recent news per holding (Google News RSS) and score sentiment.

Uses a lightweight built-in lexicon so there is no heavy ML dependency — keeps
the GitHub Action fast. Returns an average sentiment score in [-1, 1] plus a
few sample headlines.
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

POSITIVE = {
    "surge", "surges", "soar", "soars", "gain", "gains", "rise", "rises", "rally",
    "rallies", "jump", "jumps", "up", "high", "record", "profit", "profits",
    "beat", "beats", "growth", "grows", "strong", "bullish", "upgrade", "buy",
    "outperform", "boost", "boosts", "rebound", "positive", "wins", "win",
}
NEGATIVE = {
    "fall", "falls", "drop", "drops", "plunge", "plunges", "slump", "slumps",
    "decline", "declines", "loss", "losses", "down", "low", "weak", "bearish",
    "downgrade", "sell", "underperform", "cut", "cuts", "crash", "crashes",
    "negative", "fear", "fears", "slow", "slows", "miss", "misses", "warning",
}

_TOKEN_RE = re.compile(r"[a-zA-Z']+")


def get_news_sentiment(query: str, max_items: int = 8) -> dict:
    """Return {'score': float, 'count': int, 'headlines': [...]} for a query."""
    headlines = _fetch_headlines(query, max_items)
    if not headlines:
        return {"score": 0.0, "count": 0, "headlines": []}

    scores = [_score_text(h) for h in headlines]
    avg = round(sum(scores) / len(scores), 3)
    return {
        "score": avg,
        "count": len(headlines),
        "headlines": headlines[:5],
    }


def _fetch_headlines(query: str, max_items: int) -> list[str]:
    url = GOOGLE_NEWS_RSS.format(query=urllib.parse.quote(query))
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! news fetch failed for '{query}': {exc}")
        return []

    titles = [
        item.findtext("title", default="").strip()
        for item in root.iter("item")
    ]
    return [t for t in titles if t][:max_items]


def _score_text(text: str) -> float:
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    if not tokens:
        return 0.0
    pos = sum(1 for t in tokens if t in POSITIVE)
    neg = sum(1 for t in tokens if t in NEGATIVE)
    if pos == neg == 0:
        return 0.0
    return (pos - neg) / (pos + neg)


if __name__ == "__main__":
    print(get_news_sentiment("Nifty 50 ETF India"))
