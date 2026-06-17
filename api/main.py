"""
Peer Desk — FastAPI wrapper around the agent loop.

Endpoints:
  GET  /health                — liveness check
  POST /ask                   — run the agent; returns answer + tool trace + deduplicated sources
  GET  /export/financials     — full GAAP statements as CSV download
  GET  /export/kpis           — lodging KPIs as CSV download
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import queue
import threading

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import psycopg2

from agent.loop import ask_traced
from api.models import AskRequest, AskResponse, Source, ToolCallEntry
from tools.query_financials import query_financials
from tools.get_kpi import get_kpi

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(
    title="FilingsDesk",
    description="Agentic finance analyst — sourced answers from SEC filings for any public company",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_DB_URL = os.environ.get("DB_URL")


@app.get("/health")
def health():
    return {"status": "ok"}


def _rows_to_csv(rows: list[dict], filename: str) -> StreamingResponse:
    if not rows:
        raise HTTPException(status_code=404, detail="No data found for these parameters.")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/financials")
def export_financials(
    tickers: str = Query(default="PEB,HST,SHO", description="Comma-separated tickers"),
    statement: str = Query(default=None, description="income | balance | cashflow"),
    period_start: str = Query(default=None, description="YYYY-MM-DD"),
    period_end: str = Query(default=None, description="YYYY-MM-DD"),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    conn = psycopg2.connect(_DB_URL)
    try:
        rows = query_financials(
            conn,
            tickers=ticker_list,
            statement=statement or None,
            period_start=period_start or None,
            period_end=period_end or None,
        )
    finally:
        conn.close()
    # For income/cashflow: drop FY pass-through rows — they duplicate the Q4 derived row
    # for the same Dec 31 period_end. Balance sheet is point-in-time; its Dec 31 row
    # has fiscal_period='FY' (from the 10-K) and must be kept — it is not a duplicate.
    rows = [
        r for r in rows
        if not (r.get("fiscal_period") == "FY" and r.get("statement") in ("income", "cashflow"))
    ]
    label = statement or "all"
    fname = f"peerdesk_financials_{label}_{'_'.join(ticker_list)}.csv"
    return _rows_to_csv(rows, fname)


@app.get("/export/kpis")
def export_kpis(
    tickers: str = Query(default="PEB,HST,SHO", description="Comma-separated tickers"),
    segment: str = Query(default="total", description="total | urban | resort"),
    period_start: str = Query(default=None, description="YYYY-MM-DD"),
    period_end: str = Query(default=None, description="YYYY-MM-DD"),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    conn = psycopg2.connect(_DB_URL)
    try:
        rows = get_kpi(
            conn,
            tickers=ticker_list,
            segment=segment,
            period_start=period_start or None,
            period_end=period_end or None,
        )
    finally:
        conn.close()
    fname = f"peerdesk_kpis_{segment}_{'_'.join(ticker_list)}.csv"
    return _rows_to_csv(rows, fname)


@app.post("/ask/stream")
async def ask_stream(body: AskRequest):
    """SSE endpoint — streams tool_start/tool_done events then a final 'done' event."""
    if not _DB_URL:
        raise HTTPException(status_code=500, detail="DB_URL not configured")
    if not body.question.strip():
        raise HTTPException(status_code=422, detail="question must not be empty")

    prior = [{"question": t.question, "answer": t.answer} for t in body.history]
    event_q: queue.Queue = queue.Queue()

    def _run():
        try:
            answer, trace, sources = ask_traced(
                body.question.strip(), _DB_URL,
                history=prior,
                progress_cb=lambda e: event_q.put(e),
            )
            event_q.put({"type": "done", "answer": answer,
                         "tool_calls": trace, "sources": sources})
        except Exception as exc:
            logging.exception("Agent stream error")
            event_q.put({"type": "error", "detail": str(exc)})

    threading.Thread(target=_run, daemon=True).start()

    async def _generate():
        while True:
            await asyncio.sleep(0.05)
            try:
                event = event_q.get_nowait()
            except queue.Empty:
                continue
            yield f"data: {json.dumps(event, default=str)}\n\n"
            if event.get("type") in ("done", "error"):
                return

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/ask", response_model=AskResponse)
async def ask_endpoint(body: AskRequest):
    if not _DB_URL:
        raise HTTPException(status_code=500, detail="DB_URL not configured")
    if not body.question.strip():
        raise HTTPException(status_code=422, detail="question must not be empty")

    prior = [{"question": t.question, "answer": t.answer} for t in body.history]
    try:
        answer, trace, sources = ask_traced(body.question.strip(), _DB_URL, history=prior)
    except Exception as exc:
        logging.exception("Agent loop error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return AskResponse(
        answer=answer,
        tool_calls=[ToolCallEntry(**t) for t in trace],
        sources=[Source(**s) for s in sources],
    )


# ── Serve Vue frontend (production Docker build) ──────────────────────────────
_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def _spa(full_path: str):
        return FileResponse(str(_DIST / "index.html"))
