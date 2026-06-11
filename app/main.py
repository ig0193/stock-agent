"""FastAPI app: runs list, run detail, portfolio editors, trigger runs."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .analysis import run_analysis
from .chat import chat_reply
from .config import llm_status, load_env
from .decision import llm_available
from .logging_config import setup_logging
from .models import KIND_MANUAL, KIND_SCHEDULED, VALID_KINDS, Holding
from .portfolio_io import parse_csv

load_env()
setup_logging()
log = logging.getLogger("app.main")
log.info("Stock Analysis Agent starting — %s", llm_status())

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt_inr(v):
    """Indian-format rupee amount, e.g. 738.35 -> ₹738.35, 1234567 -> ₹12,34,567."""
    if not _is_num(v):
        return "—"
    neg = v < 0
    n = abs(float(v))
    whole = int(n)
    frac = n - whole
    s = str(whole)
    if len(s) > 3:  # Indian grouping: last 3 digits, then pairs
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    out = f"₹{s}"
    if frac:
        out += f"{frac:.2f}".lstrip("0")
    return ("-" + out) if neg else out


def fmt_pct(v, signed=False):
    if not _is_num(v):
        return "—"
    return (f"{v:+.1f}%" if signed else f"{v:.1f}%")


def fmt_num(v, dp=2):
    if not _is_num(v):
        return "—"
    return f"{v:.{dp}f}"


def fmt_marketcap(v):
    """Large rupee value as crore / lakh crore."""
    if not _is_num(v):
        return "—"
    cr = v / 1e7  # 1 crore = 10^7
    if cr >= 1e5:
        return f"₹{cr / 1e5:.2f} lakh Cr"
    if cr >= 1:
        return f"₹{cr:,.0f} Cr"
    return f"₹{v:,.0f}"


def fmt_dt(v):
    """ISO timestamp -> friendly '09 Jun 2026, 6:24 PM' (+ relative if recent)."""
    if not v:
        return "—"
    try:
        dt = datetime.fromisoformat(str(v))
    except (ValueError, TypeError):
        return str(v)
    hour = dt.strftime("%I").lstrip("0") or "12"
    base = dt.strftime(f"%d %b %Y, {hour}:%M %p")
    delta = datetime.now() - dt
    secs = delta.total_seconds()
    if secs < 0:
        return base
    if secs < 60:
        rel = "just now"
    elif secs < 3600:
        rel = f"{int(secs // 60)}m ago"
    elif secs < 86400:
        rel = f"{int(secs // 3600)}h ago"
    elif secs < 604800:
        rel = f"{int(secs // 86400)}d ago"
    else:
        return base
    return f"{base} · {rel}"


def _md_inline(s):
    import re
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _md_row(s):
    return [c.strip() for c in s.strip().strip("|").split("|")]


def _is_table_sep(s):
    s = s.strip()
    import re
    return bool(s) and "|" in s and "-" in s and re.fullmatch(r"[\s|:\-]+", s)


def md_to_html(text):
    """Render a safe subset of markdown (headings, bullets, tables, bold) to HTML.

    Input is HTML-escaped first, so the output is safe to mark as markup.
    """
    import re
    from markupsafe import Markup, escape
    if not text:
        return Markup("")
    lines = str(escape(text)).split("\n")
    out, in_ul, i, n = [], False, 0, len(lines)

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>"); in_ul = False

    while i < n:
        s = lines[i].strip()
        # Table: a "| ... |" row followed by a "|---|---|" separator.
        if s.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            close_ul()
            header = _md_row(s)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_md_row(lines[i].strip())); i += 1
            th = "".join(f"<th>{_md_inline(c)}</th>" for c in header)
            body = "".join("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r)
                           + "</tr>" for r in rows)
            out.append(f'<table class="md-table"><thead><tr>{th}</tr></thead>'
                       f"<tbody>{body}</tbody></table>")
            continue
        if not s or re.match(r"^-{3,}$", s):
            close_ul(); i += 1; continue
        heading = re.match(r"^#{1,6}\s+(.*)$", s)
        bullet = re.match(r"^[-*]\s+(.*)$", s)
        if bullet:
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_md_inline(bullet.group(1))}</li>"); i += 1; continue
        close_ul()
        if heading:
            out.append(f'<p class="md-h">{_md_inline(heading.group(1))}</p>')
        else:
            out.append(f"<p>{_md_inline(s)}</p>")
        i += 1
    close_ul()
    return Markup("".join(out))


def action_label(action):
    """User-facing label. CUT (owned-position trim) reads clearer as TRIM."""
    return "TRIM" if action == "CUT" else action


templates.env.filters["dt"] = fmt_dt
templates.env.filters["md"] = md_to_html
templates.env.filters["alabel"] = action_label
templates.env.filters["inr"] = fmt_inr
templates.env.filters["pct"] = fmt_pct
templates.env.filters["num"] = fmt_num
templates.env.filters["marketcap"] = fmt_marketcap

app = FastAPI(title="Stock Analysis Agent")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

db.init_db()

KIND_LABELS = {KIND_SCHEDULED: "Automated (scheduled) run",
               KIND_MANUAL: "Manual run"}


def _run_in_background(trigger: str, holdings=None, title=None) -> None:
    log.info("Triggering '%s' analysis run in background (%s holdings, title=%r)",
             trigger, "ad-hoc" if holdings is not None else "saved", title)
    threading.Thread(target=run_analysis, args=(trigger, holdings, title),
                     daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse(url="/runs", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    runs = db.list_runs()
    counts = {k: len(db.get_holdings(k)) for k in VALID_KINDS}
    return templates.TemplateResponse(
        "runs.html",
        {"request": request, "runs": runs, "counts": counts,
         "labels": KIND_LABELS},
    )


@app.post("/run/{kind}")
def trigger_run(kind: str, title: str = Form("")):
    if kind not in VALID_KINDS:
        return RedirectResponse(url="/runs", status_code=303)
    _run_in_background(kind, title=title)
    return RedirectResponse(url="/runs", status_code=303)


@app.get("/individual", response_class=HTMLResponse)
def individual_form(request: Request, err: str = ""):
    return templates.TemplateResponse(
        "individual.html", {"request": request, "err": err})


@app.post("/run/individual")
def trigger_individual(
    title: str = Form(""),
    ticker: List[str] = Form(default=[]),
    qty: List[str] = Form(default=[]),
    avg_buy_price: List[str] = Form(default=[]),
    sector: List[str] = Form(default=[]),
):
    """One-off analysis of freshly entered stocks. Not saved to any portfolio."""
    def _num(seq, i):
        v = (seq[i].strip() if i < len(seq) else "")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    holdings = []
    for i, tk in enumerate(ticker):
        tk = (tk or "").strip().upper()
        if not tk:
            continue
        holdings.append(Holding(
            ticker=tk, qty=_num(qty, i), avg_buy_price=_num(avg_buy_price, i),
            sector=(sector[i].strip() if i < len(sector) and sector[i].strip() else None),
        ))
    if not holdings:
        return RedirectResponse(url="/individual?err=Add+at+least+one+ticker",
                                status_code=303)
    _run_in_background("individual", holdings=holdings, title=title)
    return RedirectResponse(url="/runs", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    run = db.get_run(run_id)
    if not run:
        return RedirectResponse(url="/runs", status_code=303)
    recs = db.get_recommendations(run_id)
    return templates.TemplateResponse(
        "run_detail.html",
        {"request": request, "run": run, "recs": recs},
    )


_SUGGESTIONS_OWNED = [
    "Should I book partial profit here?",
    "Should I average up — and where would my new average be?",
    "What price would make this a clear add?",
    "What's the single biggest risk to this position?",
]
_SUGGESTIONS_WATCH = [
    "Is now a good entry, or should I wait?",
    "What price would make this a clear BUY?",
    "What are the main red flags here?",
    "How does it compare to its sector peers?",
]


@app.get("/runs/{run_id}/stock/{rec_id}", response_class=HTMLResponse)
def stock_detail(request: Request, run_id: int, rec_id: int):
    run = db.get_run(run_id)
    rec = db.get_recommendation(rec_id)
    if not run or not rec or rec.get("run_id") != run_id:
        return RedirectResponse(url="/runs", status_code=303)
    is_watch = (rec.get("evidence_packet") or {}).get("is_watchlist")
    return templates.TemplateResponse(
        "stock_detail.html",
        {"request": request, "run": run, "rec": rec,
         "messages": db.get_chat_messages(rec_id),
         "llm_on": llm_available(),
         "suggestions": _SUGGESTIONS_WATCH if is_watch else _SUGGESTIONS_OWNED},
    )


@app.post("/runs/{run_id}/stock/{rec_id}/chat", response_class=HTMLResponse)
def stock_chat(request: Request, run_id: int, rec_id: int, message: str = Form(...)):
    rec = db.get_recommendation(rec_id)
    message = (message or "").strip()
    if not rec or rec.get("run_id") != run_id or not message:
        return HTMLResponse("")
    db.add_chat_message(rec_id, "user", message)
    history = db.get_chat_messages(rec_id)
    try:
        reply = chat_reply(rec, history)
    except Exception as exc:  # noqa: BLE001
        log.warning("chat failed for rec %s: %s", rec_id, exc)
        reply = f"⚠ Sorry, I couldn't answer just now ({type(exc).__name__}). Please try again."
    db.add_chat_message(rec_id, "assistant", reply)
    new_msgs = [{"role": "user", "content": message},
                {"role": "assistant", "content": reply}]
    return templates.TemplateResponse(
        "_chat_bubbles.html", {"request": request, "messages": new_msgs},
    )


@app.get("/portfolio/{kind}", response_class=HTMLResponse)
def portfolio_page(request: Request, kind: str, msg: str = "", err: str = ""):
    if kind not in VALID_KINDS:
        return RedirectResponse(url="/runs", status_code=303)
    holdings = db.get_holdings_with_ids(kind)
    return templates.TemplateResponse(
        "portfolio.html",
        {"request": request, "kind": kind, "label": KIND_LABELS[kind],
         "holdings": holdings, "msg": msg, "err": err},
    )


@app.post("/portfolio/{kind}/add")
def portfolio_add(kind: str, ticker: str = Form(...),
                  qty: Optional[float] = Form(None),
                  avg_buy_price: Optional[float] = Form(None),
                  sector: str = Form("")):
    if kind not in VALID_KINDS:
        return RedirectResponse(url="/runs", status_code=303)
    # Blank qty/price => watchlist (analyze as a not-yet-purchased candidate).
    db.add_holding(kind, Holding(
        ticker=ticker.strip().upper(), qty=qty, avg_buy_price=avg_buy_price,
        sector=sector.strip() or None,
    ))
    return RedirectResponse(url=f"/portfolio/{kind}?msg=Added+{ticker.upper()}",
                            status_code=303)


@app.post("/portfolio/{kind}/delete")
def portfolio_delete(kind: str, holding_id: int = Form(...)):
    if kind not in VALID_KINDS:
        return RedirectResponse(url="/runs", status_code=303)
    db.delete_holding(kind, holding_id)
    return RedirectResponse(url=f"/portfolio/{kind}?msg=Deleted", status_code=303)


@app.post("/portfolio/{kind}/upload")
async def portfolio_upload(kind: str, file: UploadFile = File(...),
                           mode: str = Form("replace")):
    if kind not in VALID_KINDS:
        return RedirectResponse(url="/runs", status_code=303)
    raw = await file.read()
    holdings, errors = parse_csv(raw)
    if errors and not holdings:
        return RedirectResponse(
            url=f"/portfolio/{kind}?err=" + "; ".join(errors).replace(" ", "+"),
            status_code=303,
        )
    if mode == "append":
        for h in holdings:
            db.add_holding(kind, h)
    else:
        db.replace_holdings(kind, holdings)
    note = f"Loaded {len(holdings)} holdings"
    if errors:
        note += f" ({len(errors)} rows skipped)"
    return RedirectResponse(url=f"/portfolio/{kind}?msg=" + note.replace(" ", "+"),
                            status_code=303)
