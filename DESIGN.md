# Stock Analysis Agent — Design Doc

> Status: **Design / pre-implementation**
> Last updated: 2026-06-09

## 1. Purpose

A personal, advisory stock-analysis agent. You maintain a portfolio of holdings
(ticker, quantity, average buy price). Instead of manually opening each stock to
check price, trend, fundamentals, sector and market conditions, the agent gathers
all of that automatically and produces, **per stock**, a suggested action with a
confidence score and the reasoning behind it.

**The agent only advises. It never trades or takes any action.**

## 2. Scope & key decisions

| Decision | Choice |
|---|---|
| Market | India — NSE / BSE |
| Data cost | Free sources only |
| Triggers | Both scheduled (daily) **and** on-demand (manual) |
| Decision engine | Hybrid — deterministic numbers + LLM judgment/narrative (anchored hybrid: LLM may use its company/sector knowledge but must stay consistent with fetched facts and flag stale/uncertain claims) |
| Qualitative context | Company profile (business summary, segments, sector) + promoter/shareholding trend, so the LLM judges the business, not just ratios |
| Fundamentals | Yes — include company financials |
| Output | Per-stock list of `{action, confidence%}` + reasons/evidence below |
| Action enum | `BUY` / `HOLD` / `CUT` / `SELL` |
| Anchor | Recommendations are relative to **your avg buy price** (personal, not a generic screener) |
| Delivery (phase 1) | **Web UI only** (no WhatsApp / external deps yet) |
| UI shape | Master-detail: list of runs → click a run → that run's full analysis |
| Run independence | Each run is a self-contained snapshot. **No "changed since last run" diffing.** |
| Audit trail | Yes — every recommendation stores the exact evidence packet that produced it |
| Frontend | Server-rendered FastAPI + Jinja2 templates + HTMX (no JS build step) |
| Language/stack | Python end-to-end, SQLite storage |

### Action semantics
- **BUY** — add to position (strong signals + attractive price vs your avg).
- **HOLD** — no change.
- **CUT** — trim partially (reduce risk / book partial profit; conviction lowered, not gone).
- **SELL** — exit fully (thesis broken, or large profit + deteriorating signals).

Phase 1 outputs the action + confidence + reasoning only — no suggested quantity,
target price, or portfolio rebalancing. You make the final call.

## 3. Architecture

```
                  ┌─────────────── one entry point ───────────────┐
   manual button ─┤                                               │
   cron/scheduler ┤   run_analysis(portfolio):                    │
                  │     for each stock: build evidence packet     │
                  │       ├─ yfinance: price / history / fundamentals
                  │       ├─ compute technicals (pandas-ta)       │
                  │       ├─ news digest (RSS → LLM)              │
                  │       └─ shared market_weather (1 web summary)│
                  │     LLM decision per stock → {action,conf,..} │
                  │     write run + recommendations to SQLite     │
                  └───────────────────────────────────────────────┘
                                      │
                              FastAPI serves
                          /runs (list) · /runs/{id} (detail)
                                      │
                                  Web UI (Jinja + HTMX)
```

Everything runs locally. Outbound calls only to: yfinance, RSS feeds, web search
(macro summary), and the Claude API. No broker auth, no Twilio, no cloud infra.

### Pipeline stages
1. **Portfolio input** — CSV/Excel with `ticker, quantity, avg_buy_price, [sector]`.
   A UI form is a later nicety over the same schema.
2. **Data ingestion** — see Data Sources below.
3. **Analysis** — assemble a structured *evidence packet* per stock (numbers
   computed deterministically; news summarized by LLM).
4. **Decision** — LLM applies a rubric to the packet → `{action, confidence, rationale, key_risks}`.
5. **Delivery** — persist to SQLite; serve via web UI. (WhatsApp later.)

Scheduled and manual triggers call the **same** `run_analysis()` entry point.

## 4. Data sources (India, free)

| Signal | Source | Notes |
|---|---|---|
| Price + OHLCV history | `yfinance` (`RELIANCE.NS`, `INFY.NS`, `*.BO`) | Free, ~15-min delayed. Default. |
| Technicals (50/200 DMA, RSI, 52w hi/lo) | Computed locally (plain pandas) | No API; reliable from OHLCV. 52w hi/lo uses intraday High/Low (validated vs broker). |
| Fundamentals (P/E, EPS, P/B, book value, ROE, ROCE, div yield, face value) | **screener.in** (primary) → `yfinance` (fallback + sector/growth/margins) | Validated against broker data for HDFCBANK: screener.in matches closely (P/E 15.0 vs 14.35), yfinance was ~15% off. Source is labeled per recommendation in the UI. |
| Sector / index | yfinance index tickers (`^NSEI`, `^NSEBANK`, sector indices) | Map stock → sector index for relative strength. |
| News per stock | RSS (Moneycontrol, Economic Times, Google News) → LLM digest | Free but noisy; LLM summarizes headlines. |
| Macro / geopolitical | Nifty-derived market note + headlines | **One shared "market weather" note** + a structured **market_regime** (risk-on/cautious/neutral/risk-off), applied to all stocks. Both the LLM and the rule-based fallback use the regime to avoid selling into broad weakness (market-wide vs stock-specific, judged via sector relative strength). |

**Risk:** yfinance is unofficial and can rate-limit/break. Mitigation: a thin
**provider interface** so Kite/Upstox can be swapped in later without touching the
rest of the system. This abstraction is the single most important longevity decision.

## 5. Evidence packet (per stock)

```json
{
  "ticker": "INFY.NS",
  "qty": 50,
  "avg_buy_price": 1450.0,
  "current_price": 1612.0,
  "unrealized_pnl_pct": 11.2,
  "technicals":     { "above_50dma": true, "above_200dma": true, "rsi": 61, "pct_from_52w_high": -4.3 },
  "fundamentals":   { "pe": 24.1, "pb": 7.8, "roe_pct": 31.2, "roce_pct": 40, "ttm_eps": 74, "source": "screener.in (consolidated)" },
  "company_profile":{ "long_name": "...", "business_summary": "...", "sector": "...", "industry": "...", "employees": 12345 },
  "shareholding":   { "promoter_pct": 50.0, "promoter_change_q_pp": 0.0, "promoter_change_window_pp": -0.39, "fii_pct": 18.7, "dii_pct": 20.5, "as_of": "Mar 2026" },
  "sector":         { "nifty_return_1mo_pct": -3.9, "stock_return_1mo_pct": -3.3, "relative_strength_1mo_pct": 0.6 },
  "news_digest":  "LLM-summarized recent headlines...",
  "market_weather": "shared macro note for this run..."
}
```

The packet is stored verbatim with each recommendation (audit trail).

## 6. Data model (SQLite)

```
runs
  id            INTEGER PK
  created_at    TIMESTAMP
  trigger       TEXT     -- 'scheduled' | 'manual'
  status        TEXT     -- 'running' | 'done' | 'failed'
  market_weather TEXT    -- shared macro note for this run

recommendations
  id                 INTEGER PK
  run_id             INTEGER FK -> runs.id
  ticker             TEXT
  qty                REAL
  avg_buy_price      REAL
  current_price      REAL
  unrealized_pnl_pct REAL
  action             TEXT  -- BUY | HOLD | CUT | SELL
  confidence         INTEGER  -- 0..100
  rationale          TEXT
  key_risks          TEXT
  evidence_packet    TEXT  -- JSON blob (full audit trail)
```

- Runs list = `SELECT * FROM runs ORDER BY created_at DESC`.
- Run detail = `SELECT * FROM recommendations WHERE run_id = ?`.

## 7. Decision engine (hybrid)

- **Deterministic layer** computes all numeric signals (technicals, P&L vs avg
  price, relative strength). Reproducible, no hallucination risk.
- **LLM layer** receives the structured packet + a fixed rubric and returns
  `{action, confidence, rationale, key_risks}` via structured output. It does
  *judgment and narrative only* — it does not invent numbers.

Rubric anchors every call to the user's avg buy price and current P&L, weighing
trend + fundamentals + news + sector + macro into one of the four actions.

## 8. Web UI (FastAPI + Jinja + HTMX)

- `GET /` or `/runs` — table of runs (date, trigger, status, # holdings). Each row
  links to its detail page. A **"Run analysis now"** button POSTs to trigger a
  manual run (HTMX, shows progress).
- `GET /runs/{id}` — that run's analysis: the ranked recommendation list
  (`TICKER | ACTION | CONF%`), each row expandable to show rationale, key risks,
  and the full evidence packet.

## 9. Triggers

- **Manual** — button in UI → `run_analysis(trigger='manual')`.
- **Scheduled** — cron (or Claude Code scheduling) fires the same entry point
  daily after market close (~16:00 IST) with `trigger='scheduled'`.

## 10. Phasing

**MVP (phase 1):**
CSV in → yfinance prices/history/fundamentals → computed technicals → news digest
→ shared market weather → hybrid LLM decision → persist to SQLite → web UI
(runs list + detail). Both manual button and cron trigger.

**Later phases:**
- WhatsApp delivery (Twilio sandbox first, or Meta Cloud API).
- UI portfolio editing form (vs CSV upload).
- Broker data provider (Kite/Upstox) behind the provider interface.
- Optional: notification into existing tools (Notion/Jira) as tasks.

## 11. Disclaimers

This is a personal advisory/educational tool. All outputs are suggestions with
reasoning, not financial advice, and the agent never executes trades.

## 12. Proposed repo layout

```
stock-agent/
  DESIGN.md
  requirements.txt
  app/
    main.py            # FastAPI app + routes
    analysis.py        # run_analysis() entry point (orchestration)
    providers/
      __init__.py
      market_data.py   # yfinance wrapper (provider interface)
      news.py          # RSS fetch + digest
      macro.py         # market-weather web summary
    technicals.py      # pandas-ta computations
    decision.py        # LLM rubric + structured output
    db.py              # SQLite schema + queries
    models.py          # dataclasses / pydantic models
    templates/         # Jinja templates (runs list, run detail)
    static/            # minimal CSS, HTMX
  data/
    portfolio.csv      # sample input
    stock_agent.db     # SQLite (gitignored)
```
