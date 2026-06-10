"""Per-stock follow-up chat. Answers questions in the context of one stock's
analysis (its evidence packet + the recommendation produced for it)."""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, List

from .decision import MODEL, build_client_and_system, llm_available

log = logging.getLogger("app.chat")

_CHAT_SYSTEM = """You are a seasoned equity analyst continuing a conversation about
ONE specific Indian (NSE/BSE) stock that the user holds or is watching. You already
produced the recommendation shown in the context below. Answer the user's follow-up
questions directly and practically.

You can go beyond the structured recommendation — e.g. whether to book partial
profit, whether to average up/down and how that changes the user's average price,
position sizing, re-entry levels, or holding-period considerations (general, not
personalised tax advice).

Rules:
- Ground every answer in the provided CONTEXT (position, P&L, recommendation,
  triggers, fundamentals, technicals, shareholding). Use the user's actual avg price,
  quantity and current price in any math (e.g. show the new average if they add).
- For time-sensitive facts (current geopolitics, latest results) rely ONLY on the
  news in the context; today's date is in the context. Flag anything you're unsure
  of as possibly out of date rather than stating it as fact.
- Be concise and concrete. Use short paragraphs or bullets. Give numbers, not vagueness.
- You ADVISE only; you never place trades. If you give an actionable suggestion, add
  a one-line reminder that it's not financial advice."""

_MAX_HISTORY = 20


def _context_block(rec: Dict) -> str:
    ev = rec.get("evidence_packet") or {}
    ctx = {
        "ticker": rec.get("ticker"),
        "position": {
            "qty": rec.get("qty"),
            "avg_buy_price": rec.get("avg_buy_price"),
            "current_price": rec.get("current_price"),
            "unrealized_pnl_pct": rec.get("unrealized_pnl_pct"),
            "is_watchlist": ev.get("is_watchlist"),
        },
        "recommendation": {
            "action": rec.get("action"),
            "confidence": rec.get("confidence"),
            "stance": rec.get("stance"),
            "distribution": rec.get("distribution"),
            "rationale": rec.get("rationale"),
            "key_risks": rec.get("key_risks"),
            "alternatives": rec.get("alternatives"),
            "triggers": rec.get("triggers"),
        },
        "evidence": {k: ev.get(k) for k in (
            "technicals", "fundamentals", "company_profile", "shareholding",
            "sector", "news_digest", "market_weather", "market_regime", "as_of_date")},
    }
    return json.dumps(ctx, indent=2)


def chat_reply(rec: Dict, history: List[Dict]) -> str:
    """history: [{role: 'user'|'assistant', content: str}, ...] ending with the
    latest user message. Returns the assistant's reply text."""
    ticker = rec.get("ticker", "?")
    system_text = _CHAT_SYSTEM + "\n\nCONTEXT:\n" + _context_block(rec)
    client, system, label = build_client_and_system(system_text)
    messages = [{"role": m["role"], "content": m["content"]}
                for m in history[-_MAX_HISTORY:]]
    log.info("%s: chat reply via %s (%d msgs)", ticker, label, len(messages))
    t0 = time.time()
    msg = client.messages.create(
        model=MODEL, max_tokens=2048, system=system, messages=messages,
    )
    if getattr(msg, "stop_reason", None) == "max_tokens":
        log.warning("%s: chat reply hit max_tokens — may be truncated", ticker)
    text = "\n".join(b.text for b in msg.content
                     if getattr(b, "type", None) == "text").strip()
    log.info("%s: chat replied in %.1fs", ticker, time.time() - t0)
    return text or "(no response)"
