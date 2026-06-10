"""Hybrid decision engine.

Numbers are computed deterministically upstream (technicals.py / providers).
This module does *judgment + narrative* on top of the evidence packet:

  - If ANTHROPIC_API_KEY is set, ask Claude to return a structured decision.
  - Otherwise (or on any failure) fall back to a transparent rule-based decision,
    so the app always produces a result end-to-end.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict

from .models import Decision

MODEL = os.environ.get("STOCK_AGENT_MODEL", "claude-sonnet-4-6")
log = logging.getLogger("app.decision")

_RUBRIC = """You are a disciplined equity analyst for Indian (NSE/BSE) stocks.
For the given holding you receive an evidence packet:
- the user's average buy price and current unrealized P&L,
- deterministic technicals (trend, RSI, 52-week range),
- company fundamentals (P/E, P/B, ROE, ROCE, etc. on a CONSOLIDATED basis),
- a company profile (what the business does, its segments, sector/industry),
- shareholding (promoter holding level and recent trend),
- sector relative strength vs the Nifty (relative_strength_1mo_pct),
- a news digest,
- a shared market-weather note and a structured market_regime
  (risk-on / cautious / neutral / risk-off).

Reason like a human analyst, not a spreadsheet. Work in this order:
1. THESIS: what is this business, and is it still intact? Use the profile,
   fundamentals, shareholding and news.
2. CONTEXT: read market_regime and sector relative strength to decide whether any
   weakness/strength is MARKET-WIDE or STOCK-SPECIFIC.
3. POSITION: combine the above with the user's cost basis and P&L to choose an
   action and a risk/reward-justified confidence.

Macro & loss-aversion (how a human analyst thinks — but DIFFERENTIATE):
- A RISK-OFF regime is NOT a blanket "hold everything" signal. Judge each holding on
  its own merits. In the same risk-off market you should still SELL/CUT names whose
  thesis is broken, whose fundamentals are deteriorating, that are richly valued, or
  that are materially UNDERPERFORMING the index — while HOLDING (or even BUYING)
  high-quality, attractively-valued names that are merely caught in a market-wide
  drawdown.
- The point is: do not REFLEXIVELY sell a sound business into broad weakness and
  crystallize losses; but do not REFLEXIVELY hold everything either. Decide.
- Use relative_strength_1mo_pct + market_regime to label weakness as MARKET-WIDE vs
  STOCK-SPECIFIC, and let that drive the call.
- In a RISK-ON regime, trend-following BUY/HOLD signals are more trustworthy.
- Anchor to the user's average buy price, but never let it alone drive the call —
  the decision must be forward-looking, not anchored to sunk cost.

Use of knowledge (ANCHORED HYBRID):
- You MAY use your knowledge of the company's business model, competitive position
  and industry economics to interpret the numbers.
- BUT for anything TIME-SENSITIVE — current geopolitics (wars, sanctions, oil
  shocks), recent results, deals, management changes, regulations — rely ONLY on the
  provided news digest and market data. Today's date is given as as_of_date. Your
  training is months out of date: do NOT assert that any current event is happening
  (e.g. an active conflict, an oil spike) unless it appears in the provided news.
  If the news doesn't mention it, treat it as a generic/hypothetical risk at most,
  not a present fact.
- Read the dated news digest for thesis-changing catalysts; ignore generic noise and
  stale items. Never invent specific figures not present in the packet.

Actions:
- BUY  : strong signals AND price attractive vs the user's average -> add.
- HOLD : no change warranted (may include "wait out a market-wide drawdown").
- CUT  : trim partially (reduce risk / book partial profit; conviction lowered, not gone).
- SELL : exit fully (thesis broken, or large profit with deteriorating signals).

Output:
- primary_action + primary_confidence (0-100): your single best call.
- alternatives: include a secondary action with its confidence ONLY when the call is
  genuinely close (e.g. HOLD primary, BUY secondary). Leave empty when you're clear.
- rationale: 3-6 SHORT bullet points, each a single specific claim; state whether any
  weakness/strength is market-wide or stock-specific. No long paragraphs.
- key_risks: 2-5 SHORT bullet points — what could make this call wrong.

Rules:
- You ADVISE only; you never trade.
- Treat extreme/meaningless ratios cautiously (e.g. a P/E in the hundreds for a
  barely-profitable group) rather than reading them literally.
- If data is missing, say so and lower confidence rather than inventing numbers."""

_TOOL = {
    "name": "record_decision",
    "description": "Record the analyst decision for one stock.",
    "input_schema": {
        "type": "object",
        "properties": {
            "primary_action": {"type": "string", "enum": ["BUY", "HOLD", "CUT", "SELL"]},
            "primary_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "alternatives": {
                "type": "array",
                "description": "Optional secondary views, only when the call is genuinely "
                               "close. 0-2 items, none equal to primary_action.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["BUY", "HOLD", "CUT", "SELL"]},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                    "required": ["action", "confidence"],
                },
            },
            "rationale": {
                "type": "array",
                "description": "3-6 concise bullet points justifying the primary action.",
                "items": {"type": "string"},
            },
            "key_risks": {
                "type": "array",
                "description": "2-5 concise bullet points: what could make this call wrong.",
                "items": {"type": "string"},
            },
        },
        "required": ["primary_action", "primary_confidence", "rationale", "key_risks"],
    },
}


# Anthropic enforces this exact identity as the first system block for OAuth
# (Claude Code) tokens; without it the token is rejected.
_CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
_OAUTH_BETA = "oauth-2025-04-20"


def decide(packet: Dict) -> Decision:
    ticker = packet.get("ticker", "?")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if oauth_token or api_key:
        try:
            return _decide_llm(packet, api_key=api_key, oauth_token=oauth_token)
        except Exception as exc:  # noqa: BLE001 - any failure -> safe fallback
            log.warning("%s: LLM decision failed (%s: %s) -> rule-based fallback",
                        ticker, type(exc).__name__, exc)
            fallback = _decide_rules(packet)
            fallback.rationale = (
                f"[LLM unavailable: {exc}. Rule-based fallback used.] "
                + fallback.rationale
            )
            return fallback
    log.info("%s: no API key / OAuth token set -> rule-based fallback", ticker)
    return _decide_rules(packet)


def _decide_llm(packet: Dict, api_key=None, oauth_token=None) -> Decision:
    import anthropic

    ticker = packet.get("ticker", "?")
    # Prefer the OAuth (Claude Code) token if present: Bearer auth + beta header +
    # Claude Code identity as the first system block. Otherwise standard API key.
    if oauth_token:
        log.info("%s: calling LLM via OAuth token (model=%s)", ticker, MODEL)
        client = anthropic.Anthropic(auth_token=oauth_token, api_key=None,
                                     default_headers={"anthropic-beta": _OAUTH_BETA})
        system = [
            {"type": "text", "text": _CLAUDE_CODE_IDENTITY},
            {"type": "text", "text": _RUBRIC},
        ]
    else:
        log.info("%s: calling LLM via API key (model=%s)", ticker, MODEL)
        client = anthropic.Anthropic(api_key=api_key)
        system = _RUBRIC

    t0 = time.time()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "record_decision"},
        messages=[
            {
                "role": "user",
                "content": "Evidence packet:\n" + json.dumps(packet, indent=2),
            }
        ],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
            d = block.input
            usage = getattr(msg, "usage", None)
            tokens = (f"{usage.input_tokens}in/{usage.output_tokens}out"
                      if usage else "n/a")
            # Keep only valid, non-duplicate alternatives.
            primary = d["primary_action"]
            alts = []
            for a in (d.get("alternatives") or []):
                act = a.get("action")
                if act in ("BUY", "HOLD", "CUT", "SELL") and act != primary:
                    alts.append({"action": act, "confidence": int(a.get("confidence", 0))})
            alt_str = (" alt: " + ", ".join(f"{a['action']} {a['confidence']}%" for a in alts)
                       if alts else "")
            log.info("%s: LLM decided %s (%s%%)%s in %.1fs [%s tokens]",
                     ticker, primary, d.get("primary_confidence"), alt_str,
                     time.time() - t0, tokens)
            return Decision(
                action=primary,
                confidence=int(d["primary_confidence"]),
                rationale=list(d.get("rationale") or []),
                key_risks=list(d.get("key_risks") or []),
                alternatives=alts,
            )
    raise RuntimeError("model did not return a structured decision")


def _decide_rules(packet: Dict) -> Decision:
    """Transparent heuristic so the system works without an API key."""
    tech = packet.get("technicals") or {}
    pnl = packet.get("unrealized_pnl_pct")
    rsi = tech.get("rsi14")
    above50 = tech.get("above_50dma")
    above200 = tech.get("above_200dma")

    score = 0  # positive => bullish, negative => bearish
    reasons = []

    if above200 is True:
        score += 1
        reasons.append("above 200DMA (long-term uptrend)")
    elif above200 is False:
        score -= 1
        reasons.append("below 200DMA (long-term downtrend)")

    if above50 is True:
        score += 1
        reasons.append("above 50DMA")
    elif above50 is False:
        score -= 1
        reasons.append("below 50DMA")

    if rsi is not None:
        if rsi >= 70:
            score -= 1
            reasons.append(f"overbought (RSI {rsi})")
        elif rsi <= 30:
            score += 1
            reasons.append(f"oversold (RSI {rsi})")

    if pnl is not None:
        if pnl <= -15:
            score -= 1
            reasons.append(f"underwater {pnl}% vs avg buy")
        elif pnl >= 25:
            reasons.append(f"sitting on {pnl}% gain")

    # Map score + P&L to a base (stock-only) action.
    if score >= 2:
        action = "BUY"
    elif score <= -2:
        action = "SELL" if (pnl is not None and pnl < 0) else "CUT"
    elif score <= -1:
        action = "CUT"
    else:
        action = "HOLD"

    confidence = min(85, 45 + abs(score) * 12)

    # ---- Macro overlay: don't sell into broad market weakness ----
    regime = (packet.get("market_regime") or {}).get("label")
    rel = (packet.get("sector") or {}).get("relative_strength_1mo_pct")
    macro_note = ""
    if regime == "risk-off" and action in ("SELL", "CUT"):
        # Is the fall market-wide, or specific to this stock? If the stock is
        # roughly tracking the market (not materially underperforming), the
        # weakness is macro-driven -> wait rather than crystallize losses.
        stock_specific = rel is not None and rel <= -5
        if not stock_specific:
            action = "HOLD"
            confidence = 58
            macro_note = (
                " Market regime is RISK-OFF and this fall looks market-wide rather "
                "than stock-specific, so holding to avoid selling into broad weakness "
                "— wait for conditions to stabilize before cutting."
            )
        else:
            macro_note = (
                " Market is RISK-OFF, but this stock is materially underperforming "
                "(relative strength {:+.1f}pp), so the weakness looks stock-specific "
                "— trimming still warranted.".format(rel)
            )
    elif regime == "risk-off" and action == "BUY":
        # Avoid catching a falling knife unless genuinely oversold.
        if not (rsi is not None and rsi <= 35):
            action = "HOLD"
            confidence = 55
            macro_note = (
                " Signals are constructive but the market regime is RISK-OFF; "
                "waiting for stabilization before adding."
            )
    elif regime == "risk-on" and action in ("BUY", "HOLD"):
        confidence = min(90, confidence + 5)
        macro_note = " Market regime is RISK-ON, supporting trend-following signals."

    rationale = ["Heuristic decision (no LLM key)."]
    rationale += [r.capitalize() for r in reasons] if reasons else ["Insufficient data."]
    rationale.append(f"Net trend score {score}.")
    if macro_note:
        rationale.append(macro_note.strip())
    risks = [
        "Rule-based fallback uses trend, P&L and market regime only — it does not "
        "read news sentiment or fundamentals nuance.",
        "Set an API key / OAuth token for full analyst-style analysis.",
    ]
    if not reasons and not macro_note:
        confidence = 30
    return Decision(action=action, confidence=confidence,
                    rationale=rationale, key_risks=risks, alternatives=[])
