"""
tools/onboard_company.py — Dynamic company onboarding tool.

Orchestrates six stages to make a ticker queryable:
  0. Resolve ticker → CIK, name, SIC via edgartools Company()
  1. Upsert into companies (SIC → coarse industry label)
  2. Ingest XBRL facts → raw_facts → statements  (calls load_seed.ingest_company)
  3. Rebuild statements_standalone               (calls build_standalone.build_standalone)
     NOTE: build_standalone does a full TRUNCATE+rebuild over ALL tickers, not just
     the one being onboarded. This is correct — the LAG window needs all rows — but it
     means a global rebuild (~1-2s) on every onboard. Incremental per-ticker rebuild
     is a planned future optimization.
  4. Index documents — fetch 8-K earnings releases and 10-Qs, chunk, embed, store
  5. Extract KPIs from this company's newly indexed 8-Ks via Claude Haiku
  6. Update company_data_status and return a structured result dict

Idempotent: safe to re-run for an existing ticker — refreshes metadata, skips
already-loaded data via ON CONFLICT patterns throughout.

Partial failure: if embedding (Stage 4) fails, structured data (Stages 2-3) remains
committed. docs_indexed_through is set only on embedding success. All errors go into
warnings[] so the caller can surface them in the agent trace.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg2
from edgar import Company, set_identity
from pgvector.psycopg2 import register_vector

from scripts.build_standalone import build_standalone
from scripts.extract_kpis import extract_and_seed
from scripts.index_documents import index_one_filing
from scripts.load_seed import ingest_company

logger = logging.getLogger(__name__)

EDGAR_SLEEP_S = 0.15

Row = dict[str, Any]


# ── SIC code → coarse industry label ──────────────────────────────────────────

def _sic_to_industry(sic: str) -> str:
    code = int(sic) if sic.isdigit() else 0
    if 1000 <= code <= 1499: return "mining"
    if 1500 <= code <= 1799: return "construction"
    if 2000 <= code <= 3999: return "manufacturing"
    if 4000 <= code <= 4999: return "transportation_utilities"
    if 5000 <= code <= 5999: return "retail_wholesale"
    if 6000 <= code <= 6399: return "banking_finance"
    if 6400 <= code <= 6499: return "insurance"
    if 6500 <= code <= 6799: return "real_estate"
    if 7000 <= code <= 7999: return "services"
    if 8000 <= code <= 8099: return "healthcare"
    if 8700 <= code <= 8999: return "professional_services"
    return "other"


# ── Period window ──────────────────────────────────────────────────────────────

def _default_period_end() -> date:
    today = date.today()
    m = today.month
    if m < 4:  return date(today.year - 1, 12, 31)
    if m < 7:  return date(today.year, 3, 31)
    if m < 10: return date(today.year, 6, 30)
    return date(today.year, 9, 30)


def _period_window(periods_back: int) -> tuple[date, date]:
    """Return (period_start, period_end) covering `periods_back` quarters back."""
    period_end   = _default_period_end()
    months_back  = periods_back * 3
    year_offset  = months_back // 12
    month_offset = months_back % 12
    start_year   = period_end.year - year_offset
    start_month  = period_end.month - month_offset
    if start_month <= 0:
        start_month += 12
        start_year  -= 1
    quarter_start = ((start_month - 1) // 3) * 3 + 1
    return date(start_year, quarter_start, 1), period_end


# ── Three-state guard helpers ─────────────────────────────────────────────────

STALENESS_CACHE_TTL_HOURS = 24


def _get_registered(
    conn: psycopg2.extensions.connection, ticker: str
) -> dict | None:
    """Return {cik, name, industry} from companies, or None if not registered."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cik, name, industry FROM companies WHERE ticker = %s", (ticker,)
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"cik": row[0], "name": row[1], "industry": row[2]}


def _get_status(conn: psycopg2.extensions.connection, ticker: str) -> dict:
    """Return loaded-through dates and cache timestamp from company_data_status."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT xbrl_loaded_through, docs_indexed_through, last_staleness_check_at
            FROM company_data_status WHERE ticker = %s
            """,
            (ticker,),
        )
        row = cur.fetchone()
    if not row:
        return {
            "xbrl_loaded_through": None,
            "docs_indexed_through": None,
            "last_staleness_check_at": None,
        }
    return {
        "xbrl_loaded_through":    row[0],
        "docs_indexed_through":   row[1],
        "last_staleness_check_at": row[2],
    }


def _update_staleness_check_at(
    conn: psycopg2.extensions.connection, ticker: str
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_data_status (ticker, last_staleness_check_at, updated_at)
            VALUES (%s, NOW(), NOW())
            ON CONFLICT (ticker) DO UPDATE
                SET last_staleness_check_at = NOW(),
                    updated_at = NOW()
            """,
            (ticker,),
        )
    conn.commit()


def _edgar_latest_dates(entity) -> dict[str, date | None]:
    """Fetch latest filed_date per form from EDGAR — metadata only, no document body."""
    result: dict[str, date | None] = {}
    for form in ("10-Q", "10-K", "8-K"):
        try:
            filings = entity.get_filings(form=form)
            f = filings[0] if filings else None
            if f:
                raw = getattr(f, "filing_date", None) or getattr(f, "filed", None)
                result[form] = date.fromisoformat(str(raw)) if raw else None
            else:
                result[form] = None
        except Exception as exc:
            logger.warning("EDGAR metadata call failed for %s: %s — treating as stale", form, exc)
            result[form] = None
    return result


def _xbrl_is_stale(status: dict, edgar: dict) -> bool:
    loaded = status.get("xbrl_loaded_through")
    if loaded is None:
        return True
    latest = max(
        (d for d in [edgar.get("10-Q"), edgar.get("10-K")] if d is not None),
        default=None,
    )
    return latest is not None and latest > loaded


def _docs_is_stale(status: dict, edgar: dict) -> bool:
    loaded = status.get("docs_indexed_through")
    if loaded is None:
        return True
    latest = max(
        (d for d in [edgar.get("8-K"), edgar.get("10-Q")] if d is not None),
        default=None,
    )
    return latest is not None and latest > loaded


def _run_incremental(
    conn: psycopg2.extensions.connection,
    ticker: str,
    cik: str,
    entity,
    status: dict,
    voyage_client,
    claude_client,
    xbrl_stale: bool,
    docs_stale: bool,
    period_start: date,
) -> dict:
    """Run narrowed Stages 2–6 for a registered but stale company."""
    warnings: list[str] = []
    until = date.today()

    since_xbrl = (
        status["xbrl_loaded_through"] + timedelta(days=1)
        if status["xbrl_loaded_through"]
        else period_start
    )
    since_docs = (
        status["docs_indexed_through"] + timedelta(days=1)
        if status["docs_indexed_through"]
        else period_start
    )

    # Stage 2 (XBRL) — only if stale
    statements_rows = 0
    if xbrl_stale:
        logger.info("[%s] Incremental Stage 2: XBRL since %s …", ticker, since_xbrl)
        ingest_result = ingest_company(conn, ticker, cik, since_xbrl, until)
        warnings.extend(ingest_result.get("warnings", []))
        statements_rows = ingest_result.get("statement_rows", 0)
        logger.info("[%s] Incremental Stage 2 done: statements=%d", ticker, statements_rows)

        # Stage 3 (rebuild) — required after XBRL change
        logger.info("[%s] Incremental Stage 3: Rebuilding statements_standalone …", ticker)
        build_standalone(conn)

    # Stage 4 (docs) — only if stale
    total_chunks = 0
    docs_indexed_through: date | None = status["docs_indexed_through"]
    if docs_stale:
        logger.info("[%s] Incremental Stage 4: Indexing docs since %s …", ticker, since_docs)
        try:
            register_vector(conn)
            since_str = since_docs.strftime("%Y-%m-%d")
            until_str = until.strftime("%Y-%m-%d")
            for form in ("8-K", "10-Q"):
                try:
                    filing_list = list(
                        entity.get_filings(form=form).filter(
                            date=f"{since_str}:{until_str}"
                        )
                    )
                except Exception as exc:
                    warnings.append(f"Could not fetch {form} filings: {exc}")
                    logger.warning("[%s] %s fetch failed: %s", ticker, form, exc)
                    continue
                logger.info("[%s] Found %d new %s filing(s).", ticker, len(filing_list), form)
                for filing in filing_list:
                    try:
                        inserted, _ = index_one_filing(
                            conn, voyage_client, ticker, cik, filing, form
                        )
                        total_chunks += inserted
                    except Exception as exc:
                        warnings.append(f"Indexing failed for one {form} filing: {exc}")
                        logger.warning("[%s] index_one_filing error: %s", ticker, exc)
                    time.sleep(EDGAR_SLEEP_S)
            docs_indexed_through = until
        except Exception as exc:
            warnings.append(f"Incremental doc indexing failed: {exc}")
            logger.error("[%s] %s", ticker, exc)

    # Stage 5 (KPIs) — scoped to new filings only
    logger.info("[%s] Incremental Stage 5: KPI extraction since %s …", ticker, since_docs)
    kpi_count = 0
    try:
        kpi_count = extract_and_seed(
            conn, claude_client, voyage_client,
            ticker=ticker,
            since_date=status["docs_indexed_through"],
        )
    except Exception as exc:
        warnings.append(f"KPI extraction failed: {exc}")
        logger.error("[%s] KPI extraction error: %s", ticker, exc)

    # Stage 6 — update status and staleness cache
    new_xbrl_through = until if xbrl_stale else status["xbrl_loaded_through"]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_data_status
                (ticker, xbrl_loaded_through, docs_indexed_through,
                 last_staleness_check_at, updated_at)
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (ticker) DO UPDATE
                SET xbrl_loaded_through      = EXCLUDED.xbrl_loaded_through,
                    docs_indexed_through     = EXCLUDED.docs_indexed_through,
                    last_staleness_check_at  = NOW(),
                    updated_at               = NOW()
            """,
            (ticker, new_xbrl_through, docs_indexed_through),
        )
    conn.commit()

    return {
        "success":         True,
        "incremental":     True,
        "ticker":          ticker,
        "cik":             cik,
        "since_xbrl":      str(since_xbrl),
        "since_docs":      str(since_docs),
        "until":           str(until),
        "statements_rows": statements_rows,
        "chunks_indexed":  total_chunks,
        "kpis_rows":       kpi_count,
        "warnings":        warnings,
    }


# ── Non-calendar fiscal year detection ────────────────────────────────────────

def _has_noncalendar_fy(conn: psycopg2.extensions.connection, ticker: str) -> bool:
    """Return True if any 10-Q period_end for this ticker falls outside months 3/6/9/12."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM raw_facts
            WHERE ticker = %s
              AND form IN ('10-Q', '10-K')
              AND EXTRACT(MONTH FROM period_end)::INT NOT IN (3, 6, 9, 12)
            LIMIT 1
            """,
            (ticker,),
        )
        return cur.fetchone() is not None


# ── Main function ──────────────────────────────────────────────────────────────

def onboard_company(
    conn: psycopg2.extensions.connection,
    ticker: str,
    voyage_client,
    claude_client,
    periods_back: int = 8,
    force_refresh: bool = False,
) -> dict:
    """
    Three-state onboarding:
      1. Not registered → full onboard (Stages 0–6).
      2. Registered + current (staleness check cached or no newer EDGAR filings) → no-op.
      3. Registered + stale → incremental top-up (new filings only).
      force_refresh=True bypasses the state machine and runs a full re-ingest.

    Returns a result dict. Never raises — errors go into warnings[].
    """
    ticker = ticker.upper().strip()

    period_start, period_end = _period_window(periods_back)

    if not force_refresh:
        registered = _get_registered(conn, ticker)
        if registered:
            status = _get_status(conn, ticker)
            last_check = status.get("last_staleness_check_at")
            cache_fresh = (
                last_check is not None
                and (
                    datetime.now(tz=timezone.utc) - last_check
                ).total_seconds() < STALENESS_CACHE_TTL_HOURS * 3600
            )

            if cache_fresh:
                logger.info("[%s] Staleness cache fresh — already current.", ticker)
                return {
                    "success":        True,
                    "already_loaded": True,
                    "status":         "current",
                    "cached":         True,
                    "ticker":         ticker,
                    "message": (
                        f"{ticker} is already loaded and current (cache age <{STALENESS_CACHE_TTL_HOURS}h). "
                        "Use query_financials, get_kpi, get_financial_trends, or search_documents directly."
                    ),
                    "warnings": [],
                }

            # Cache miss or expired — 3 cheap EDGAR metadata calls
            logger.info("[%s] Staleness cache expired — checking EDGAR filing dates …", ticker)
            identity = os.environ.get("EDGAR_IDENTITY", "")
            if identity:
                set_identity(identity)
            entity = Company(ticker)
            edgar = _edgar_latest_dates(entity)
            _update_staleness_check_at(conn, ticker)

            xbrl_stale = _xbrl_is_stale(status, edgar)
            docs_stale = _docs_is_stale(status, edgar)

            if not xbrl_stale and not docs_stale:
                logger.info("[%s] No new filings on EDGAR — already current.", ticker)
                return {
                    "success":        True,
                    "already_loaded": True,
                    "status":         "current",
                    "cached":         False,
                    "ticker":         ticker,
                    "message": (
                        f"{ticker} is already loaded and current. "
                        "Use query_financials, get_kpi, get_financial_trends, or search_documents directly."
                    ),
                    "warnings": [],
                }

            logger.info(
                "[%s] Stale (xbrl_stale=%s docs_stale=%s) — running incremental update.",
                ticker, xbrl_stale, docs_stale,
            )
            return _run_incremental(
                conn, ticker, registered["cik"], entity, status,
                voyage_client, claude_client,
                xbrl_stale, docs_stale, period_start,
            )

    warnings: list[str] = []

    # ── Stage 0: Resolve ticker via EDGAR ─────────────────────────────────────
    logger.info("[%s] Stage 0: Resolving ticker via EDGAR …", ticker)
    try:
        identity = os.environ.get("EDGAR_IDENTITY", "")
        if identity:
            set_identity(identity)
        entity = Company(ticker)
        cik    = str(entity.cik or "").lstrip("0")
        name   = entity.name or ticker
        sic    = str(entity.sic or "")
    except Exception as exc:
        return {"success": False, "ticker": ticker, "error": str(exc), "warnings": []}

    if not cik:
        return {
            "success": False,
            "ticker":  ticker,
            "error":   f"Could not resolve CIK for '{ticker}' — verify the ticker is SEC-registered.",
            "warnings": [],
        }

    industry = _sic_to_industry(sic)
    logger.info(
        "[%s] Resolved: CIK=%s  name=%r  sic=%s  industry=%s",
        ticker, cik, name, sic, industry,
    )

    period_start, period_end = _period_window(periods_back)
    logger.info(
        "[%s] Ingest window: %s → %s  (%d quarters back)",
        ticker, period_start, period_end, periods_back,
    )

    # ── Stage 1: Register company ──────────────────────────────────────────────
    logger.info("[%s] Stage 1: Upserting company record …", ticker)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies (ticker, cik, name, sic, industry)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (ticker) DO UPDATE
                SET name     = EXCLUDED.name,
                    sic      = EXCLUDED.sic,
                    industry = EXCLUDED.industry
            """,
            (ticker, cik, name, sic, industry),
        )
    conn.commit()

    # ── Stage 2: XBRL ingest ──────────────────────────────────────────────────
    logger.info("[%s] Stage 2: Ingesting XBRL facts (raw_facts → statements) …", ticker)
    ingest_result = ingest_company(conn, ticker, cik, period_start, period_end)
    warnings.extend(ingest_result.get("warnings", []))
    logger.info(
        "[%s] Stage 2 done: raw=%d  statements=%d",
        ticker,
        ingest_result.get("raw_rows", 0),
        ingest_result.get("statement_rows", 0),
    )

    # ── Stage 3: Rebuild statements_standalone ────────────────────────────────
    logger.info("[%s] Stage 3: Rebuilding statements_standalone (global rebuild) …", ticker)
    build_standalone(conn)

    if _has_noncalendar_fy(conn, ticker):
        msg = (
            f"{ticker} has a non-calendar fiscal year (SIC {sic}). "
            "Fiscal-year-aware de-cumulation applied. "
            "Verify standalone income/cashflow values are correct after onboarding."
        )
        warnings.append(msg)
        logger.warning("[%s] %s", ticker, msg)

    # ── Stage 4: Index documents ───────────────────────────────────────────────
    logger.info("[%s] Stage 4: Indexing documents (8-K earnings + 10-Q) …", ticker)
    total_chunks          = 0
    docs_indexed_through: date | None = None

    try:
        register_vector(conn)

        since_str = period_start.strftime("%Y-%m-%d")
        until_str = period_end.strftime("%Y-%m-%d")

        for form in ("8-K", "10-Q"):
            logger.info(
                "[%s] Fetching %s filings %s – %s …", ticker, form, since_str, until_str,
            )
            try:
                filings     = entity.get_filings(form=form).filter(
                    date=f"{since_str}:{until_str}"
                )
                filing_list = list(filings)
            except Exception as exc:
                warnings.append(f"Could not fetch {form} filings: {exc}")
                logger.warning("[%s] %s filing fetch failed: %s", ticker, form, exc)
                continue

            logger.info("[%s] Found %d %s filing(s) to index.", ticker, len(filing_list), form)
            for filing in filing_list:
                try:
                    inserted, _ = index_one_filing(
                        conn, voyage_client, ticker, cik, filing, form
                    )
                    total_chunks += inserted
                except Exception as exc:
                    warnings.append(f"Indexing failed for one {form} filing: {exc}")
                    logger.warning("[%s] index_one_filing error: %s", ticker, exc)
                time.sleep(EDGAR_SLEEP_S)

        docs_indexed_through = period_end

    except Exception as exc:
        msg = (
            f"Document indexing failed (structured data already committed, "
            f"RAG layer incomplete): {exc}"
        )
        warnings.append(msg)
        logger.error("[%s] %s", ticker, msg)

    logger.info("[%s] Stage 4 done: %d chunk(s) indexed.", ticker, total_chunks)

    # ── Stage 5: Extract KPIs (this company only) ─────────────────────────────
    logger.info("[%s] Stage 5: Extracting KPIs from indexed 8-Ks …", ticker)
    kpi_count = 0
    try:
        kpi_count = extract_and_seed(conn, claude_client, voyage_client, ticker=ticker)
    except Exception as exc:
        msg = f"KPI extraction failed: {exc}"
        warnings.append(msg)
        logger.error("[%s] %s", ticker, msg)

    logger.info("[%s] Stage 5 done: %d KPI row(s) inserted.", ticker, kpi_count)

    # ── Stage 6: Update company_data_status ───────────────────────────────────
    logger.info("[%s] Stage 6: Updating company_data_status …", ticker)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_data_status
                (ticker, xbrl_loaded_through, docs_indexed_through, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (ticker) DO UPDATE
                SET xbrl_loaded_through  = EXCLUDED.xbrl_loaded_through,
                    docs_indexed_through = EXCLUDED.docs_indexed_through,
                    updated_at           = NOW()
            """,
            (ticker, period_end, docs_indexed_through),
        )
    conn.commit()

    result: Row = {
        "success":         True,
        "ticker":          ticker,
        "cik":             cik,
        "name":            name,
        "industry":        industry,
        "period_start":    str(period_start),
        "period_end":      str(period_end),
        "statements_rows": ingest_result.get("statement_rows", 0),
        "chunks_indexed":  total_chunks,
        "kpis_rows":       kpi_count,
        "warnings":        warnings,
    }
    logger.info("[%s] Onboarding complete: %s", ticker, result)
    return result
