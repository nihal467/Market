"""Fetch recent news per holding (Google News RSS) and score sentiment.

Uses a lightweight built-in lexicon so there is no heavy ML dependency — keeps
the GitHub Action fast. Returns an average sentiment score in [-1, 1] plus a
few sample headlines.
"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional

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
    items = _fetch_headlines(query, max_items)
    if not items:
        return {"score": 0.0, "count": 0, "headlines": [], "items": []}

    scores = [_score_text(item["clean_title"]) for item in items]
    avg = round(sum(scores) / len(scores), 3)
    return {
        "score": avg,
        "count": len(items),
        "headlines": [item["title"] for item in items[:5]],
        "items": items[:5],
        "newest_published": items[0].get("published_at"),
    }


def _fetch_headlines(query: str, max_items: int) -> list[dict]:
    url = GOOGLE_NEWS_RSS.format(query=urllib.parse.quote(query + " when:10d"))
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! news fetch failed for '{query}': {exc}")
        return []

    out = []
    seen = set()
    for item in root.iter("item"):
        title = item.findtext("title", default="").strip()
        if not title:
            continue
        clean, source = _clean_title(title)
        key = _dedupe_key(clean)
        if key in seen:
            continue
        seen.add(key)
        published = _parse_pubdate(item.findtext("pubDate", default=""))
        out.append({
            "title": title,
            "clean_title": clean,
            "source": source,
            "published_at": published,
        })
        if len(out) >= max_items:
            break
    return out


def _clean_title(title: str) -> tuple[str, Optional[str]]:
    parts = title.rsplit(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return title.strip(), None


def _dedupe_key(text: str) -> str:
    tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
    return " ".join(tokens[:14])


def _parse_pubdate(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


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
