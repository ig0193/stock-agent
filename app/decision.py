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

from .models import ACTIONS, Decision

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

Data granularity: returns are provided at 1D / 1W / 1M / 3M / 1Y for both the stock
and the Nifty, with relative strength at 1W / 1M / 3M. Use the horizon that fits your
point — don't default everything to 1M. Short-term (1D/1W) shows momentum/bounces;
3M/1Y shows the durable trend.

Two modes — check the "is_watchlist" flag in the packet:
A) OWNED position (is_watchlist = false) — actions:
   - BUY  : strong signals AND price attractive vs the user's average -> add.
   - HOLD : no change warranted (may include "wait out a market-wide drawdown").
   - CUT  : trim partially (reduce risk / book partial profit; conviction lowered).
   - SELL : exit fully (thesis broken, or large profit with deteriorating signals).
B) WATCHLIST candidate (is_watchlist = true) — there is NO position, qty/avg are
   absent, so there is no P&L to anchor to. Decide purely on forward attractiveness:
   - BUY   : attractive to initiate a position now (quality + valuation + setup).
   - WATCH : worth owning but not yet — wait for a better price, base, or catalyst.
   - AVOID : not worth buying (weak business, overvalued, or broken setup).
   Use ONLY these three for watchlist stocks; never HOLD/CUT/SELL something not owned.

Output:
- distribution: your honest conviction split across actions, confidences summing to
  ~100, ordered by confidence (first = primary call). A single label often hides real
  ambiguity — when you are genuinely torn, SHOW it (e.g. HOLD 55 / CUT 35 / BUY 10).
  When you are clear, one action can carry most of the weight. Don't manufacture a
  split that the evidence doesn't support, and don't collapse a real one.
- stance: one plain-English sentence telling the holder what to actually do, capturing
  any nuance the labels miss. It must follow from the evidence and the distribution.
- rationale: 3-6 SHORT bullet points, each a single specific claim; state whether any
  weakness/strength is market-wide or stock-specific. No long paragraphs.
- key_risks: 2-5 SHORT bullet points — what could make this call wrong.
- triggers: REQUIRED and especially important for HOLD/WATCH (otherwise the user has
  nothing to act on). Give the concrete conditions that would change your call — at
  minimum one up-trigger (what would make you BUY/add) and one down-trigger (what
  would make you CUT/SELL/AVOID). Be specific and checkable: name price levels (e.g.
  "add below ₹720"), indicator thresholds ("RSI < 30", "reclaims 50DMA"), or events
  ("if next quarter NIM compresses", "if the ADNOC deal closes"). Anchor levels to
  the actual current price and 52-week range in the packet.

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
            "stance": {
                "type": "string",
                "description": "ONE plain-English sentence capturing your actual, nuanced "
                               "recommendation — what you'd tell the holder to DO. This is "
                               "where you express nuance a single label can't, when the "
                               "evidence warrants it (e.g. 'Hold your core, but booking a "
                               "portion into this strength is reasonable'). Let it follow "
                               "honestly from the evidence; do not force any stance.",
            },
            "distribution": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": "Your honest conviction split across actions, as integer "
                               "confidences that SUM TO ~100. Order by confidence desc; the "
                               "first item is the primary call. If you are genuinely torn "
                               "(e.g. thesis intact but stretched), reflect that split here "
                               "rather than collapsing to a single label. Owned position: use "
                               "BUY/HOLD/CUT/SELL (CUT = trim / book partial profit). Watchlist "
                               "(not owned): use BUY/WATCH/AVOID.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["BUY", "HOLD", "CUT", "SELL", "WATCH", "AVOID"]},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                    "required": ["action", "confidence"],
                },
            },
            "rationale": {
                "type": "array",
                "description": "3-6 concise bullet points justifying the call.",
                "items": {"type": "string"},
            },
            "key_risks": {
                "type": "array",
                "description": "2-5 concise bullet points: what could make this call wrong.",
                "items": {"type": "string"},
            },
            "triggers": {
                "type": "array",
                "description": "Forward conditions that would CHANGE the call — this is what "
                               "makes a HOLD/WATCH actionable. Always include at least one "
                               "up-trigger (what would make you BUY/add) and one down-trigger "
                               "(what would make you CUT/SELL/AVOID). Be concrete: price levels, "
                               "indicator thresholds (e.g. RSI<30, reclaim 50DMA), or specific "
                               "events (e.g. next quarter's margin, a deal closing).",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["BUY", "HOLD", "CUT", "SELL", "WATCH", "AVOID"]},
                        "condition": {"type": "string"},
                    },
                    "required": ["action", "condition"],
                },
            },
        },
        "required": ["stance", "distribution", "rationale",
                     "key_risks", "triggers"],
    },
}


# Anthropic enforces this exact identity as the first system block for OAuth
# (Claude Code) tokens; without it the token is rejected.
_CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
_OAUTH_BETA = "oauth-2025-04-20"


def llm_available() -> bool:
    return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                or os.environ.get("ANTHROPIC_API_KEY"))


def build_client_and_system(system_text):
    """Return (client, system, label) honoring OAuth-token-or-API-key auth.

    `system` is a list-of-blocks (OAuth needs the Claude Code identity first) or a
    plain string (API key). Raises RuntimeError if no credential is configured.
    """
    import anthropic

    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if oauth_token:
        client = anthropic.Anthropic(auth_token=oauth_token, api_key=None,
                                     default_headers={"anthropic-beta": _OAUTH_BETA})
        system = [
            {"type": "text", "text": _CLAUDE_CODE_IDENTITY},
            {"type": "text", "text": system_text},
        ]
        return client, system, "OAuth token"
    if api_key:
        return anthropic.Anthropic(api_key=api_key), system_text, "API key"
    raise RuntimeError("no ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN configured")


def decide(packet: Dict) -> Decision:
    ticker = packet.get("ticker", "?")
    if llm_available():
        try:
            return _decide_llm(packet)
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


def _decide_llm(packet: Dict) -> Decision:
    ticker = packet.get("ticker", "?")
    client, system, label = build_client_and_system(_RUBRIC)
    log.info("%s: calling LLM via %s (model=%s)", ticker, label, MODEL)

    t0 = time.time()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=3072,  # must fit rationale + risks + triggers + alternatives
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
    if getattr(msg, "stop_reason", None) == "max_tokens":
        log.warning("%s: response hit max_tokens — output may be truncated", ticker)
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
            d = block.input
            usage = getattr(msg, "usage", None)
            tokens = (f"{usage.input_tokens}in/{usage.output_tokens}out"
                      if usage else "n/a")
            # Normalize the conviction distribution: valid actions, dedup, sorted desc.
            dist, seen = [], set()
            for item in (d.get("distribution") or []):
                act = item.get("action")
                if act in ACTIONS and act not in seen:
                    seen.add(act)
                    dist.append({"action": act, "confidence": int(item.get("confidence", 0))})
            dist.sort(key=lambda x: -x["confidence"])
            if not dist:
                raise RuntimeError("empty distribution")
            primary = dist[0]
            alts = dist[1:]
            dist_str = ", ".join(f"{x['action']} {x['confidence']}%" for x in dist)
            log.info("%s: LLM decided [%s] in %.1fs [%s tokens]",
                     ticker, dist_str, time.time() - t0, tokens)
            triggers = [
                {"action": tg.get("action"), "condition": tg.get("condition")}
                for tg in (d.get("triggers") or [])
                if tg.get("condition")
            ]
            return Decision(
                action=primary["action"],
                confidence=primary["confidence"],
                stance=(d.get("stance") or "").strip(),
                rationale=list(d.get("rationale") or []),
                key_risks=list(d.get("key_risks") or []),
                alternatives=alts,
                triggers=triggers,
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

    is_watch = packet.get("is_watchlist")
    confidence = min(85, 45 + abs(score) * 12)
    regime = (packet.get("market_regime") or {}).get("label")
    rel = (packet.get("sector") or {}).get("relative_strength_1mo_pct")
    macro_note = ""

    if is_watch:
        # No position: decide whether to initiate. BUY / WATCH / AVOID.
        if score >= 2:
            action = "BUY"
        elif score <= -1:
            action = "AVOID"
        else:
            action = "WATCH"
        if regime == "risk-off" and action == "BUY" and not (rsi is not None and rsi <= 35):
            action = "WATCH"
            confidence = 55
            macro_note = (" Setup is constructive but the market is RISK-OFF; "
                          "watching for a better entry rather than initiating now.")
        return Decision(
            action=action, confidence=confidence,
            stance=_RULE_STANCE.get(action, ""),
            rationale=_rule_rationale(reasons, score, macro_note),
            key_risks=_RULE_RISKS, alternatives=[],
            triggers=_rule_triggers(tech, is_watch=True),
        )

    # ---- Owned position: map score + P&L to an action ----
    if score >= 2:
        action = "BUY"
    elif score <= -2:
        action = "SELL" if (pnl is not None and pnl < 0) else "CUT"
    elif score <= -1:
        action = "CUT"
    else:
        action = "HOLD"

    # ---- Macro overlay: don't sell into broad market weakness ----
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

    if not reasons and not macro_note:
        confidence = 30
    return Decision(action=action, confidence=confidence,
                    stance=_RULE_STANCE.get(action, ""),
                    rationale=_rule_rationale(reasons, score, macro_note),
                    key_risks=_RULE_RISKS, alternatives=[],
                    triggers=_rule_triggers(tech, is_watch=False))


_RULE_RISKS = [
    "Rule-based fallback uses trend, P&L and market regime only — it does not "
    "read news sentiment or fundamentals nuance.",
    "Set an API key / OAuth token for full analyst-style analysis.",
]

_RULE_STANCE = {
    "BUY": "Trend signals support adding to the position.",
    "HOLD": "Signals warrant no change — hold the position.",
    "CUT": "Signals have weakened — trimming the position is reasonable.",
    "SELL": "Signals have broken down — exiting is warranted.",
    "WATCH": "Worth watching — wait for a better setup before initiating.",
    "AVOID": "Setup is weak — better to avoid initiating here.",
}


def _rule_rationale(reasons, score, macro_note):
    out = ["Heuristic decision (no LLM key)."]
    out += [r.capitalize() for r in reasons] if reasons else ["Insufficient data."]
    out.append(f"Net trend score {score}.")
    if macro_note:
        out.append(macro_note.strip())
    return out


def _rule_triggers(tech, is_watch):
    """Concrete forward conditions derived from the technical levels."""
    dma50, dma200 = tech.get("dma50"), tech.get("dma200")
    low_52w = tech.get("low_52w")
    trg = []
    if is_watch:
        if dma200:
            trg.append({"action": "BUY",
                        "condition": f"pulls back toward the 200DMA (~₹{dma200}) or RSI falls below 35"})
        else:
            trg.append({"action": "BUY", "condition": "dips on weakness with RSI below 35"})
        if low_52w:
            trg.append({"action": "AVOID",
                        "condition": f"breaks below its 52-week low (~₹{low_52w})"})
    else:
        if dma50:
            trg.append({"action": "BUY",
                        "condition": f"reclaims and holds above the 50DMA (~₹{dma50})"})
        if dma200:
            trg.append({"action": "CUT",
                        "condition": f"breaks decisively below the 200DMA (~₹{dma200})"})
        if not trg:
            trg.append({"action": "BUY",
                        "condition": "price recovers back above its moving averages"})
    return trg
