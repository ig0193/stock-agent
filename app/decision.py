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

Macro & loss-aversion (IMPORTANT — this is how a human analyst thinks):
- In a RISK-OFF regime, do NOT recommend SELL/CUT merely because price is down and
  the stock is below its moving averages. If the fall is market-wide (the stock is
  roughly tracking the index, i.e. relative strength is not deeply negative) and the
  business thesis is intact, prefer HOLD and wait for conditions to stabilize rather
  than crystallize losses by selling into broad weakness.
- BUT avoid the opposite trap (sunk-cost): if the thesis is broken, fundamentals are
  deteriorating, promoters are exiting, or the stock is materially UNDERPERFORMING
  its market (relative strength deeply negative), then CUT/SELL even at a loss.
- In a RISK-ON regime, trend-following BUY/HOLD signals are more trustworthy.
- Anchor to the user's average buy price, but never let it alone drive the call —
  the decision must be forward-looking.

Use of knowledge (ANCHORED HYBRID):
- You MAY use your own knowledge of the company, its competitive position, and
  its industry to interpret the numbers.
- But every substantive claim must be consistent with the fetched profile,
  shareholding, fundamentals and news in the packet. Do not contradict them.
- Your training has a cutoff; if you rely on memory for anything time-sensitive
  (recent results, management changes, deals), say it may be outdated and lower
  your confidence. Never invent specific figures not present in the packet.
- Read the news digest for thesis-changing catalysts; ignore generic noise.

Actions:
- BUY  : strong signals AND price attractive vs the user's average -> add.
- HOLD : no change warranted (includes "wait out a market-wide drawdown").
- CUT  : trim partially (reduce risk / book partial profit; conviction lowered, not gone).
- SELL : exit fully (thesis broken, or large profit with deteriorating signals).

Rules:
- You ADVISE only; you never trade. Output a suggestion with reasoning.
- Treat extreme/meaningless ratios cautiously (e.g. a P/E in the hundreds for a
  barely-profitable group) rather than reading them literally.
- If data is missing, say so and lower confidence rather than inventing numbers.
- confidence is an integer 0-100 reflecting how sure you are of the action.
- rationale: 2-4 sentences, concrete and specific to THIS business; state whether
  any weakness/strength is market-wide or stock-specific.
- key_risks: the main things that could make this call wrong."""

_TOOL = {
    "name": "record_decision",
    "description": "Record the analyst decision for one stock.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["BUY", "HOLD", "CUT", "SELL"]},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "rationale": {"type": "string"},
            "key_risks": {"type": "string"},
        },
        "required": ["action", "confidence", "rationale", "key_risks"],
    },
}


def decide(packet: Dict) -> Decision:
    ticker = packet.get("ticker", "?")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            return _decide_llm(packet, api_key)
        except Exception as exc:  # noqa: BLE001 - any failure -> safe fallback
            log.warning("%s: LLM decision failed (%s: %s) -> rule-based fallback",
                        ticker, type(exc).__name__, exc)
            fallback = _decide_rules(packet)
            fallback.rationale = (
                f"[LLM unavailable: {exc}. Rule-based fallback used.] "
                + fallback.rationale
            )
            return fallback
    log.info("%s: no ANTHROPIC_API_KEY set -> rule-based fallback", ticker)
    return _decide_rules(packet)


def _decide_llm(packet: Dict, api_key: str) -> Decision:
    import anthropic

    ticker = packet.get("ticker", "?")
    log.info("%s: calling LLM (model=%s)", ticker, MODEL)
    t0 = time.time()
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_RUBRIC,
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
            log.info("%s: LLM decided %s (%s%%) in %.1fs [%s tokens]",
                     ticker, d.get("action"), d.get("confidence"),
                     time.time() - t0, tokens)
            return Decision(
                action=d["action"],
                confidence=int(d["confidence"]),
                rationale=d["rationale"],
                key_risks=d["key_risks"],
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

    rationale = (
        "Heuristic decision (no LLM key). Signals: "
        + ("; ".join(reasons) if reasons else "insufficient data")
        + f". Net trend score {score}." + macro_note
    )
    risks = (
        "Rule-based fallback uses trend, P&L and market regime only; it does not "
        "read news sentiment or fundamentals nuance. Set ANTHROPIC_API_KEY for "
        "full hybrid analysis."
    )
    if not reasons and not macro_note:
        confidence = 30
    return Decision(action=action, confidence=confidence,
                    rationale=rationale, key_risks=risks)
