"""Per-stock news headlines via free RSS (Google News), summarized later by the LLM.

Headlines are dated so the LLM can judge recency and not rely on stale memory for
time-sensitive events.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import List

import feedparser

_MAX_HEADLINES = 8


def _entry_date(entry) -> str:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        try:
            return time.strftime("%Y-%m-%d", parsed)
        except (ValueError, TypeError):
            return ""
    return ""


def fetch_headlines(query: str) -> List[str]:
    """Recent India-market headlines (each prefixed with its publish date)."""
    q = urllib.parse.quote(f"{query} stock India")
    url = (
        f"https://news.google.com/rss/search?q={q}"
        "&hl=en-IN&gl=IN&ceid=IN:en"
    )
    try:
        feed = feedparser.parse(url)
    except Exception:
        return []
    # Most recent first.
    entries = sorted(
        feed.entries[: _MAX_HEADLINES * 2],
        key=lambda e: getattr(e, "published_parsed", None) or time.gmtime(0),
        reverse=True,
    )
    headlines = []
    for entry in entries[:_MAX_HEADLINES]:
        title = getattr(entry, "title", "").strip()
        if not title:
            continue
        date = _entry_date(entry)
        headlines.append(f"[{date}] {title}" if date else title)
    return headlines
