# Setup & Run Guide

Step-by-step guide to get the Stock Analysis Agent running locally. For what the
agent does and how it's designed, see [README.md](README.md) and [DESIGN.md](DESIGN.md).

---

## 1. Prerequisites

- **Python 3.9+** (verify with `python3 --version`)
- **git** (to clone)
- Internet access (the agent fetches prices from Yahoo Finance, fundamentals from
  screener.in, and news from Google News RSS)
- *(Optional but recommended)* an **Anthropic API key** for full LLM analysis

---

## 2. Get the code

```bash
git clone git@github.com:ig0193/stock-agent.git
cd stock-agent
```

---

## 3. Create a virtual environment & install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

> All later commands use `.venv/bin/python` / `.venv/bin/uvicorn` so you don't need
> to "activate" the venv. If you prefer, run `source .venv/bin/activate` once and
> then drop the `.venv/bin/` prefix.

---

## 4. Configure the API key (optional, recommended)

With a valid key the agent uses the full **hybrid LLM** analysis (reasons like an
analyst). **Without** a key it still works, using a transparent, macro-aware
**rule-based fallback**.

It must be a **billable API key** from
<https://console.anthropic.com/settings/keys> — this is *separate* from your
Claude Code / claude.ai login.

Pick **one** of these:

**Option A — `.env` file (simplest, recommended):**

```bash
cp .env.example .env
# then edit .env and set:
#   ANTHROPIC_API_KEY=sk-ant-api03-...
```

The app auto-loads `.env` on startup. It's gitignored, so your key is never committed.

**Option B — shell environment:**

```bash
export ANTHROPIC_API_KEY=sk-ant-api03-...
```

If you put this in `~/.zshrc`, note it's only loaded by **interactive** shells —
run the app from your normal terminal (not a script/cron) and it'll be picked up.

**Alternative — Claude Code OAuth token (uses your Claude subscription):**

Instead of an API key you can use a Claude Code OAuth token (`sk-ant-oat01-…`):

```bash
# in .env or your shell
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

If set, it takes precedence over `ANTHROPIC_API_KEY`. This draws on your Claude
**subscription** quota rather than API billing. ⚠️ It is an **unofficial** path —
these tokens are intended for the Claude Code client, so it may break if Anthropic
changes things; a proper API key is the supported option. The agent falls back to
rule-based decisions if the token is rejected.

**Optional overrides** (any method):

| Variable | Default | Purpose |
|---|---|---|
| `STOCK_AGENT_MODEL` | `claude-sonnet-4-6` | Which Claude model to use |
| `STOCK_AGENT_LOG_LEVEL` | `INFO` | Set `DEBUG` for per-fetch detail |

---

## 5. Run the web UI

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open **<http://127.0.0.1:8000>**.

On startup the log line confirms your config, e.g.:

```
Stock Analysis Agent starting — LLM ENABLED via API key (model=claude-sonnet-4-6)
```

(or `LLM DISABLED … using rule-based fallback` if no valid key).

---

## 6. First-time usage

1. **Add your holdings.** Go to **Manual portfolio** (`/portfolio/manual`) or
   **Automated portfolio** (`/portfolio/scheduled`) — the two are independent.
   - **Manual entry:** fill ticker / qty / avg buy price (+ optional sector).
   - **CSV upload:** columns `ticker, qty, avg_buy_price` (+ optional `sector`).
     A sample is in `data/portfolio_sample.csv`. Use Yahoo tickers — NSE `TCS.NS`,
     BSE `500570.BO`.
   - **Watchlist (not yet purchased):** leave **qty and avg buy price blank** (in the
     form or CSV). The stock is then analyzed as a candidate and gets a
     **BUY / WATCH / AVOID** call instead of BUY/HOLD/CUT/SELL.
2. **Run analysis.** From the home page (`/runs`) click **▶ Run analysis now**.
   The run appears in the table and auto-refreshes from `running` → `done`.
3. **View results.** Click a run to see each stock's action (owned: BUY / HOLD /
   CUT / SELL; watchlist: BUY / WATCH / AVOID), confidence, an optional secondary
   view, and — on expanding a row — bulleted rationale, key risks, and the
   full evidence (technicals, fundamentals, company profile, shareholding, news).

---

## 7. Scheduled (automated) runs

The automated portfolio is analyzed by a separate entry point you can put on cron:

```bash
.venv/bin/python run_scheduled.py
```

Example crontab — weekdays at 16:00 IST (after market close):

```cron
0 16 * * 1-5  cd /path/to/stock-agent && .venv/bin/python run_scheduled.py >> data/cron.log 2>&1
```

Results show up in the same web UI under the runs list.

---

## 8. Watching the logs

Logs go to stdout (visible in the terminal running uvicorn, or `data/cron.log` for
scheduled runs). A typical run shows:

```
Run #6 started (trigger=manual) over 4 holding(s)
Run #6: market regime=risk-off (Nifty 1M=-3.9%, 3M=-5.1%)
Run #6: analyzing HDFCBANK.NS (qty=15, avg=1550)
HDFCBANK.NS: calling LLM (model=claude-sonnet-4-6)
HDFCBANK.NS: LLM decided HOLD (62%) in 4.2s [3500in/180out tokens]
Run #6: HDFCBANK.NS -> HOLD (62%) in 6.1s
Run #6 done: 4 recommendation(s) in 38.4s
```

For more detail (per-fetch counts, data sources): `STOCK_AGENT_LOG_LEVEL=DEBUG`.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Startup says `LLM DISABLED` but you set a key | Key not visible to the process. If it's in `~/.zshrc`, that's only loaded by interactive shells — use a `.env` file instead, or `export` it in the same terminal. |
| Log shows `LLM decision failed (AuthenticationError: 401 invalid x-api-key)` | The key is **invalid/revoked**, even if the format looks right. Generate a fresh one at console.anthropic.com and update `.env`/shell. The agent falls back to rule-based meanwhile. |
| All recommendations look heuristic / mention "no LLM key" | Same as above — running on the fallback. Fix the key to get full analysis. |
| `fundamentals unavailable` or `shareholding data unavailable` warnings | screener.in didn't return data for that ticker (often small-caps or a changed page). Prices/technicals still work; the agent notes the gap rather than guessing. |
| A run is stuck on `running` | A run over many stocks takes time (each fetches prices + fundamentals + news sequentially). Watch the logs; it updates per stock. |
| `ModuleNotFoundError` | Dependencies not installed in the venv — re-run step 3. |
| Numbers differ from your broker (e.g. P/B) | The agent uses **consolidated** financials (standard for valuation); some apps show standalone. The source/basis is labeled on each recommendation. |

---

## 10. Project layout (quick reference)

```
stock-agent/
  app/
    main.py            # FastAPI app + routes + Jinja filters
    analysis.py        # run_analysis() orchestration
    decision.py        # LLM rubric + rule-based fallback
    technicals.py      # DMA / RSI / 52w range
    db.py              # SQLite: portfolios, runs, recommendations
    config.py          # .env loading + LLM status
    logging_config.py  # stdout logging setup
    providers/         # market_data (yfinance), screener.in, news, macro
    templates/ static/ # UI
  run_scheduled.py     # cron entry point (automated portfolio)
  data/                # SQLite db (gitignored) + sample CSV
  requirements.txt  .env.example
```
