"""Orchestration: the single run_analysis() entry point used by both triggers."""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Dict, List, Optional

from . import db
from .decision import decide
from .models import EvidencePacket, Holding
from .providers.macro import build_market_weather, classify_regime
from .providers.market_data import get_provider
from .providers.news import fetch_headlines
from .technicals import compute_technicals

NIFTY = "^NSEI"
log = logging.getLogger("app.analysis")


def build_packet(holding: Holding, market_weather: str,
                 nifty_return_1mo: Optional[float],
                 market_regime: Optional[Dict] = None) -> EvidencePacket:
    provider = get_provider()
    warnings: List[str] = []

    hist = provider.history(holding.ticker, period="1y")
    if hist is None or hist.empty:
        warnings.append("no price history returned")
        log.warning("  %s: no price history", holding.ticker)
    else:
        log.debug("  %s: %d days of history", holding.ticker, len(hist))
    tech = compute_technicals(hist)

    snap = provider.snapshot(holding.ticker)
    fundamentals = snap.get("fundamentals", {})
    company_profile = snap.get("profile", {})
    shareholding = snap.get("shareholding", {})
    if not any(v is not None for k, v in fundamentals.items()
               if k not in ("name", "sector", "industry")):
        warnings.append("fundamentals unavailable")
    if not company_profile.get("business_summary"):
        warnings.append("business summary unavailable")
    if not shareholding:
        warnings.append("shareholding data unavailable")

    current = tech.get("current_price")
    pnl = None
    if current is not None and holding.avg_buy_price:
        pnl = round((current - holding.avg_buy_price) / holding.avg_buy_price * 100, 1)

    # Sector relative strength vs Nifty (1-month).
    sector_block: Dict = {
        "sector": holding.sector or fundamentals.get("sector"),
        "nifty_return_1mo_pct": nifty_return_1mo,
        "stock_return_1mo_pct": tech.get("return_1mo_pct"),
    }
    if tech.get("return_1mo_pct") is not None and nifty_return_1mo is not None:
        sector_block["relative_strength_1mo_pct"] = round(
            tech["return_1mo_pct"] - nifty_return_1mo, 1
        )

    name = fundamentals.get("name") or holding.ticker
    headlines = fetch_headlines(name)
    news_digest = " | ".join(headlines) if headlines else "No recent headlines found."
    if not headlines:
        warnings.append("no news headlines found")
    log.debug("  %s: fundamentals source=%s, %d headlines, current=%s pnl=%s",
              holding.ticker, fundamentals.get("source"), len(headlines),
              current, pnl)
    if warnings:
        log.info("  %s: data warnings -> %s", holding.ticker, ", ".join(warnings))

    return EvidencePacket(
        ticker=holding.ticker,
        qty=holding.qty,
        avg_buy_price=holding.avg_buy_price,
        current_price=current,
        unrealized_pnl_pct=pnl,
        technicals=tech,
        fundamentals=fundamentals,
        company_profile=company_profile,
        shareholding=shareholding,
        sector=sector_block,
        news_digest=news_digest,
        market_weather=market_weather,
        market_regime=market_regime or {},
        as_of_date=date.today().isoformat(),
        data_warnings=warnings,
    )


def run_analysis(trigger: str) -> int:
    """Run analysis over the portfolio matching `trigger` ('scheduled'|'manual').

    Returns the run id. Persists the run, market weather, and one
    recommendation (with full evidence packet) per holding.
    """
    holdings = db.get_holdings(trigger)
    run_id = db.create_run(trigger)
    log.info("Run #%d started (trigger=%s) over %d holding(s)",
             run_id, trigger, len(holdings))

    if not holdings:
        log.warning("Run #%d: no holdings configured for '%s' portfolio", run_id, trigger)
        db.finish_run(run_id, market_weather="", status="failed",
                      error="No holdings configured for this portfolio.")
        return run_id

    t_run = time.time()
    try:
        # Fetch the Nifty once and derive both the macro note and the regime.
        provider = get_provider()
        nifty_hist = provider.history(NIFTY, period="1y")
        nifty_tech = compute_technicals(nifty_hist)
        nifty_1mo = nifty_tech.get("return_1mo_pct")
        market_regime = classify_regime(nifty_tech)
        market_weather = build_market_weather(nifty_tech, market_regime)
        log.info("Run #%d: market regime=%s (Nifty 1M=%s%%, 3M=%s%%)",
                 run_id, market_regime.get("label"),
                 market_regime.get("nifty_1mo_pct"), market_regime.get("nifty_3mo_pct"))

        for h in holdings:
            t0 = time.time()
            log.info("Run #%d: analyzing %s (qty=%s, avg=%s)",
                     run_id, h.ticker, h.qty, h.avg_buy_price)
            packet = build_packet(h, market_weather, nifty_1mo, market_regime)
            decision = decide(packet.to_dict())
            log.info("Run #%d: %s -> %s (%d%%) in %.1fs",
                     run_id, h.ticker, decision.action, decision.confidence,
                     time.time() - t0)
            db.add_recommendation(
                run_id,
                {
                    "ticker": h.ticker,
                    "qty": h.qty,
                    "avg_buy_price": h.avg_buy_price,
                    "current_price": packet.current_price,
                    "unrealized_pnl_pct": packet.unrealized_pnl_pct,
                    "action": decision.action,
                    "confidence": decision.confidence,
                    "rationale": decision.rationale,
                    "key_risks": decision.key_risks,
                    "alternatives": decision.alternatives,
                    "evidence_packet": packet.to_dict(),
                },
            )

        db.finish_run(run_id, market_weather=market_weather, status="done")
        log.info("Run #%d done: %d recommendation(s) in %.1fs",
                 run_id, len(holdings), time.time() - t_run)
    except Exception as exc:  # noqa: BLE001
        log.exception("Run #%d FAILED: %s", run_id, exc)
        db.finish_run(run_id, market_weather="", status="failed", error=str(exc))
    return run_id
