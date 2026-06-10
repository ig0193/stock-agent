"""Per-stock news headlines via free RSS (Google News), summarized later by the LLM."""
from __future__ import annotations

import urllib.parse
from typing import List

import feedparser

_MAX_HEADLINES = 8


def fetch_headlines(query: str) -> List[str]:
    """Recent India-market headlines for a query (company name or ticker)."""
    q = urllib.parse.quote(f"{query} stock India")
    url = (
        f"https://news.google.com/rss/search?q={q}"
        "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    try:
        feed = feedparser.parse(url)
    except Exception:
        return []
    headlines = []
    for entry in feed.entries[:_MAX_HEADLINES]:
        title = getattr(entry, "title", "").strip()
        if title:
            headlines.append(title)
    return headlines
