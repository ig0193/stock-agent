# Stock Analysis Agent

A personal, **advisory** stock-analysis agent for Indian (NSE/BSE) equities.
You keep a portfolio (ticker, qty, avg buy price); the agent gathers price/trend,
fundamentals, sector strength, news and a shared market-weather note, then suggests
a per-stock action — **BUY / HOLD / CUT / SELL** — with a confidence score and the
reasoning behind it. **It only advises; it never trades.**

See [DESIGN.md](DESIGN.md) for the full design.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Optional (recommended) — enables the full hybrid LLM analysis. Without it the agent
falls back to a transparent rule-based decision so it still works:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# optional model override (default: claude-sonnet-4-6)
export STOCK_AGENT_MODEL=claude-sonnet-4-6
```

## Run the web UI

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000
```

- **Runs list** (`/runs`) — every analysis run as a row; click a row for its full
  analysis. "Run analysis now" buttons trigger a run (status auto-refreshes).
- **Run detail** (`/runs/{id}`) — ranked recommendations; click a stock to expand
  rationale, key risks, and the full evidence packet (audit trail).
- **Portfolios** — two independent sets:
  - **Automated** (`/portfolio/scheduled`) — used by the scheduled job.
  - **Manual** (`/portfolio/manual`) — used by manual runs.
  Each supports **CSV upload** and **manual stock entry**.

### CSV format

Columns `ticker, qty, avg_buy_price` (+ optional `sector`). Header aliases like
`symbol`, `quantity`, `avg price` are accepted. Use Yahoo tickers — NSE `TCS.NS`,
BSE `500570.BO`. A sample is in `data/portfolio_sample.csv`.

## Scheduled (automated) runs

The same analysis entry point is exposed for cron:

```bash
.venv/bin/python run_scheduled.py
```

Example crontab (weekdays 16:00 IST, after close):

```
0 16 * * 1-5  cd /path/to/stock-agent && .venv/bin/python run_scheduled.py >> data/cron.log 2>&1
```

## Notes & limitations

- **Data is free / unofficial** (yfinance prices, Google News RSS); quotes are
  delayed and the provider can rate-limit. A `MarketDataProvider` interface
  (`app/providers/market_data.py`) lets a broker API (Kite/Upstox) be slotted in later.
- **Fundamentals** come from **screener.in** (validated as accurate for Indian
  stocks), with yfinance as fallback. The data source is labeled on every
  recommendation. Prices/technicals come from yfinance and were validated to match
  broker data exactly (OHLC, 52-week range, market cap).
- **Small-cap fundamentals** may be missing — flagged in each packet's data warnings
  rather than guessed.
- **Macro/geopolitical** is one shared "market weather" note per run, not per-ticker.
- Each run is an independent snapshot — no cross-run diffing by design.

## Roadmap

WhatsApp delivery, in-UI portfolio editing niceties, broker data provider, and
optional push into Notion/Jira tasks. See DESIGN.md §10.
