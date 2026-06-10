"""FastAPI app: runs list, run detail, portfolio editors, trigger runs."""
from __future__ import annotations

import os
import threading
from datetime import datetime

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .analysis import run_analysis
from .models import KIND_MANUAL, KIND_SCHEDULED, VALID_KINDS, Holding
from .portfolio_io import parse_csv

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


templates.env.filters["dt"] = fmt_dt
templates.env.filters["inr"] = fmt_inr
templates.env.filters["pct"] = fmt_pct
templates.env.filters["num"] = fmt_num
templates.env.filters["marketcap"] = fmt_marketcap

app = FastAPI(title="Stock Analysis Agent")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

db.init_db()

KIND_LABELS = {KIND_SCHEDULED: "Automated (scheduled) run",
               KIND_MANUAL: "Manual run"}


def _run_in_background(trigger: str) -> None:
    threading.Thread(target=run_analysis, args=(trigger,), daemon=True).start()


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
def trigger_run(kind: str):
    if kind not in VALID_KINDS:
        return RedirectResponse(url="/runs", status_code=303)
    _run_in_background(kind)
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
def portfolio_add(kind: str, ticker: str = Form(...), qty: float = Form(...),
                  avg_buy_price: float = Form(...), sector: str = Form("")):
    if kind not in VALID_KINDS:
        return RedirectResponse(url="/runs", status_code=303)
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
