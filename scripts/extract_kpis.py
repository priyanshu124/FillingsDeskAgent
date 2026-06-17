"""
Extract operating KPIs from indexed filings using two-step semantic retrieval.

Industry-agnostic: discovers whatever metrics the company reports (whatever the
industry), then uses vector similarity to find the relevant chunks per metric,
then extracts the precise value from those chunks.

Two-step flow:
  Step A — Discovery: first 12 chunks → Claude → list of DiscoveredMetric
  Step B — Retrieval per metric: embed query → top-8 chunks → Claude → precise value

Plausibility: relative-scale check against the same metric's prior periods.
              No hardcoded bounds. No metric-specific or industry-specific config.
Dedup: correction-aware — higher-confidence extractions update lower-confidence
       rows. hand_seeded rows are never overwritten.

Scope: processes both 8-K and 10-Q filings, ordered filed_date ASC with 8-K
       before 10-Q on the same date, so earnings releases take priority.

API cost estimate (per company onboarding, ~8 filings, ~15 metrics/filing):
  Step A: ~8 Claude Haiku calls
  Step B: ~120 Voyage embeds + ~120 Claude Haiku calls
  Total:  ~$0.08–$0.12, ~90–150 s sequential.
  For companies with 20+ filings, move KPI extraction to a background step.

Run after scripts/index_documents.py.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import time
from datetime import date
from decimal import Decimal

import anthropic
import psycopg2
from dotenv import load_dotenv

from scripts.kpi_canonical import (
    CANONICAL_NAMES,
    _confidence_rank,
    check_plausibility,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MODEL            = "claude-haiku-4-5-20251001"
VOYAGE_MODEL     = "voyage-finance-2"
DISCOVERY_CHUNKS = 12   # Step A: first N chunks per filing
RETRIEVAL_K      = 8    # Step B: top-K chunks retrieved per metric
SLEEP_S          = 0.3  # between Claude calls (rate-limit headroom)

_Q_END = {"Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "Q4": (12, 31), "FY": (12, 31)}


# ── Data structure ─────────────────────────────────────────────────────────────

@dataclasses.dataclass
class DiscoveredMetric:
    canonical:     str    # after CANONICAL_NAMES lookup
    reported_name: str    # exact name as printed in the filing
    unit:          str    # USD | % | USD/share | x | count | other
    value_type:    str    # "level" | "rate"
    period:        str    # e.g. "Q1 2026"
    segment:       str    # "total" | specific segment name
    period_end:    date
    fiscal_period: str    # Q1 | Q2 | Q3 | Q4 | FY


# ── Period parsing ─────────────────────────────────────────────────────────────

def _parse_period(period_str: str) -> tuple[date, str] | None:
    """Parse 'Q1 2026', 'FY 2025', 'Full Year 2024' → (period_end_date, fiscal_period)."""
    s = period_str.strip().upper()
    s = re.sub(r"FULL[\s\-]+YEAR", "FY", s)
    s = re.sub(r"\bANNUAL\b",      "FY", s)
    s = re.sub(r"\bYEAR\b",        "FY", s)
    m = re.match(r"(Q1|Q2|Q3|Q4|FY)\s+(\d{4})", s)
    if not m:
        return None
    quarter, year = m.group(1), int(m.group(2))
    if not (2010 <= year <= 2040):
        return None
    month, day = _Q_END[quarter]
    return date(year, month, day), quarter


# ── Pure helper ────────────────────────────────────────────────────────────────

def _build_kpi_name(canonical: str, value_type: str) -> str:
    """Rate metrics stored as '{canonical}_growth'; levels use canonical as-is."""
    return canonical if value_type == "level" else f"{canonical}_growth"


# ── Step A: Discovery ──────────────────────────────────────────────────────────

_DISCOVERY_PROMPT = """\
This is a {ticker} {form} filing dated {filed_date}. List every operating and \
financial KPI explicitly reported, whatever the industry. For each return:
  reported_name : exact name as printed in the filing
  canonical     : snake_case normalized identifier
  unit          : USD | % | USD/share | x | count | other
  value_type    : "level" (an absolute reported figure, e.g. $215.78) or \
"rate" (a growth or change percentage, e.g. +1.2%)
  period        : reporting period, e.g. "Q1 2026"
  segment       : "total" for whole-company, or the specific labeled segment name

Return ONLY valid JSON, no other text:
{{"metrics": [
  {{"reported_name": "...", "canonical": "...", "unit": "...",
    "value_type": "level", "period": "Q1 2026", "segment": "total"}}
]}}

Max 20 entries. Actuals only — no guidance or forecast values.

FILING TEXT (opening sections):
{text}"""


def _discover_metrics(
    conn: psycopg2.extensions.connection,
    claude_client,
    doc_id: str,
    ticker: str,
    form: str,
    filed_date,
) -> list[DiscoveredMetric]:
    """Step A: read first DISCOVERY_CHUNKS, ask Claude what metrics exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT text FROM doc_chunks WHERE doc_id = %s ORDER BY chunk_index LIMIT %s",
            (doc_id, DISCOVERY_CHUNKS),
        )
        chunks = [r[0] for r in cur.fetchall()]

    if not chunks:
        logger.warning("[%s] No chunks for doc %s — skipping discovery.", ticker, doc_id)
        return []

    text   = "\n\n---\n\n".join(chunks)
    prompt = _DISCOVERY_PROMPT.format(
        ticker=ticker, form=form, filed_date=str(filed_date), text=text,
    )

    try:
        response = claude_client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("[%s] Step A Claude call failed for %s: %s", ticker, doc_id, exc)
        return []

    raw   = response.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logger.warning("[%s] No JSON in Step A response for %s", ticker, doc_id)
        return []

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("[%s] JSON parse error in Step A for %s: %s", ticker, doc_id, exc)
        return []

    metrics: list[DiscoveredMetric] = []
    for item in data.get("metrics", []):
        reported_name = str(item.get("reported_name", "")).strip()
        suggested     = str(item.get("canonical",     "")).lower().strip()
        if not reported_name or not suggested:
            continue

        canonical = CANONICAL_NAMES.get(suggested, suggested)
        if suggested not in CANONICAL_NAMES:
            logger.warning(
                "[%s] Unmapped metric %r (doc %s) — add to CANONICAL_NAMES "
                "if it recurs under variant spellings", ticker, suggested, doc_id,
            )

        unit       = str(item.get("unit",       "USD")).strip() or "USD"
        value_type = str(item.get("value_type", "level")).lower().strip()
        period     = str(item.get("period",     "")).strip()
        segment    = str(item.get("segment",    "total")).lower().strip() or "total"

        parsed = _parse_period(period)
        if not parsed:
            logger.debug(
                "[%s] Skipping unparseable period %r in %s", ticker, period, doc_id,
            )
            continue

        period_end, fiscal_period = parsed
        metrics.append(DiscoveredMetric(
            canonical=canonical,
            reported_name=reported_name,
            unit=unit,
            value_type=value_type if value_type in ("level", "rate") else "level",
            period=period,
            segment=segment,
            period_end=period_end,
            fiscal_period=fiscal_period,
        ))

    logger.info("[%s] Step A: %d metric(s) in %s", ticker, len(metrics), doc_id)
    return metrics


# ── Step B: Retrieval and extraction ──────────────────────────────────────────

_LEVEL_EXTRACTION_PROMPT = """\
Extract the exact reported value of '{reported_name}' for period '{period}', \
segment '{segment}' from the passages below.

CRITICAL:
- Extract the ABSOLUTE LEVEL value (e.g., 215.78 for RevPAR; 3400000000 for ARR).
- Do NOT extract a year-over-year growth percentage instead of the level.
- If both a level and a % change appear in the text, take the level.
- If ONLY a percentage change exists with no absolute level, return null for value.
- Preserve exact reported precision including all decimal places (215.78, not 216).

Return ONLY valid JSON, no other text:
{{"value": <number or null>, "unit": "...", "source_note": "<where in the passage>"}}

PASSAGES:
{text}"""

_RATE_EXTRACTION_PROMPT = """\
Extract the year-over-year or period-over-period percentage change of \
'{reported_name}' for period '{period}', segment '{segment}' from the passages below.

Return ONLY valid JSON, no other text:
{{"value": <number or null>, "unit": "%", "source_note": "<where in the passage>"}}

PASSAGES:
{text}"""


def _retrieve_and_extract(
    conn: psycopg2.extensions.connection,
    claude_client,
    voyage_client,
    doc_id: str,
    metric: DiscoveredMetric,
) -> tuple[float | None, str, str]:
    """
    Step B: embed retrieval query, find top-K chunks by cosine similarity,
    extract precise value from those chunks via Claude.

    Returns (value, unit, source_note). value=None if not found or only rate available.
    """
    query = f"{metric.reported_name} {metric.period} {metric.segment} reported value"

    # Embed the retrieval query
    try:
        embed_result    = voyage_client.embed([query], model=VOYAGE_MODEL)
        query_embedding = embed_result.embeddings[0]
    except Exception as exc:
        logger.error("Voyage embed failed for %r: %s", query, exc)
        return None, metric.unit, ""

    # Vector retrieve from this specific filing's chunks
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT text
            FROM doc_chunks
            WHERE doc_id = %s
              AND embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (doc_id, query_embedding, RETRIEVAL_K),
        )
        chunk_texts = [r[0] for r in cur.fetchall()]

    if not chunk_texts:
        logger.warning("No chunks with embeddings for doc_id=%s", doc_id)
        return None, metric.unit, ""

    text   = "\n\n---\n\n".join(chunk_texts)
    prompt = (
        _RATE_EXTRACTION_PROMPT if metric.value_type == "rate"
        else _LEVEL_EXTRACTION_PROMPT
    ).format(
        reported_name=metric.reported_name,
        period=metric.period,
        segment=metric.segment,
        text=text,
    )

    try:
        response = claude_client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("Step B Claude call failed for %r: %s", metric.reported_name, exc)
        return None, metric.unit, ""

    raw   = response.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logger.warning("No JSON in Step B response for %r", metric.reported_name)
        return None, metric.unit, ""

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None, metric.unit, ""

    value_raw = data.get("value")
    if value_raw is None:
        return None, metric.unit, data.get("source_note", "")

    try:
        value = float(value_raw)
    except (TypeError, ValueError):
        return None, metric.unit, ""

    if not math.isfinite(value):
        return None, metric.unit, ""

    extracted_unit = str(data.get("unit", metric.unit)).strip() or metric.unit
    source_note    = str(data.get("source_note", "")).strip()

    return value, extracted_unit, source_note


# ── Main pipeline ──────────────────────────────────────────────────────────────

def extract_and_seed(
    conn: psycopg2.extensions.connection,
    claude_client,
    voyage_client,
    ticker: str | None = None,
    since_date: date | None = None,
) -> int:
    """
    Discover and extract KPIs from indexed 8-K and 10-Q filings.

    Processing order: filed_date ASC, 8-K before 10-Q on the same date.
    First source encountered wins within a run. Across runs: higher-confidence
    extractions update lower-confidence rows; hand_seeded rows are never overwritten.

    since_date: if set, only process filings filed strictly after this date.
                Used by the incremental update path to avoid reprocessing old filings.

    Returns total rows inserted + updated.
    """
    # Ensure pgvector type adaptation is registered on this connection
    try:
        from pgvector.psycopg2 import register_vector
        register_vector(conn)
    except Exception:
        pass  # already registered — safe to ignore

    # Industry lookup for kpi_category
    with conn.cursor() as cur:
        cur.execute("SELECT ticker, industry FROM companies")
        industry_map: dict[str, str | None] = {r[0]: r[1] for r in cur.fetchall()}

    # Fetch target filings: 8-K before 10-Q on same date
    with conn.cursor() as cur:
        clauses = ["form IN ('8-K', '10-Q')"]
        params: list = []
        if ticker:
            clauses.append("ticker = %s")
            params.append(ticker)
        if since_date:
            clauses.append("filed_date > %s")
            params.append(since_date)
        where = " AND ".join(clauses)
        cur.execute(
            f"""
            SELECT doc_id, ticker, cik, accession_no, filed_date, form
            FROM documents
            WHERE {where}
            ORDER BY filed_date ASC,
                     CASE WHEN form = '8-K' THEN 0 ELSE 1 END ASC
            """,
            params,
        )
        docs = cur.fetchall()

    logger.info(
        "Processing %d filing(s) (%s) …",
        len(docs),
        f"ticker={ticker}" if ticker else "all tickers",
    )

    seen: set[tuple] = set()  # (ticker, kpi_name, segment, period_end) within run
    total_inserted   = 0
    total_updated    = 0

    for doc_id, doc_ticker, cik, accession_no, filed_date, form in docs:

        # ── Step A: Discover what metrics this filing reports ──────────────────
        metrics = _discover_metrics(
            conn, claude_client, doc_id, doc_ticker, form, filed_date,
        )
        if not metrics:
            continue

        industry = industry_map.get(doc_ticker)

        for metric in metrics:
            kpi_name = _build_kpi_name(metric.canonical, metric.value_type)
            key      = (doc_ticker, kpi_name, metric.segment, metric.period_end)
            if key in seen:
                continue

            # ── Step B: Retrieve relevant chunks and extract precise value ─────
            value, unit, _source_note = _retrieve_and_extract(
                conn, claude_client, voyage_client, doc_id, metric,
            )
            time.sleep(SLEEP_S)

            if value is None or not math.isfinite(value):
                logger.debug(
                    "[%s] No value for %s %s/%s — skipping",
                    doc_ticker, kpi_name, metric.period, metric.segment,
                )
                continue

            confidence = check_plausibility(
                conn, doc_ticker, kpi_name, metric.segment, value,
            )
            if confidence == "low":
                logger.warning(
                    "[%s] Low-confidence: %s %s/%s = %s "
                    "(out of scale with prior periods)",
                    doc_ticker, kpi_name, metric.period, metric.segment, value,
                )

            db_value = Decimal(str(round(value, 6)))

            # ── Correction-aware dedup ─────────────────────────────────────────
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, confidence FROM kpis "
                    "WHERE ticker=%s AND kpi=%s AND segment=%s AND period_end=%s LIMIT 1",
                    (doc_ticker, kpi_name, metric.segment, metric.period_end),
                )
                existing = cur.fetchone()

            if existing:
                existing_id, existing_conf = existing
                if _confidence_rank(existing_conf) >= _confidence_rank(confidence):
                    seen.add(key)
                    continue  # equal or better confidence already in DB
                # Higher confidence → update the existing row
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE kpis "
                        "SET value=%s, unit=%s, confidence=%s, "
                        "    source_accession=%s, source_form=%s, source_filed_date=%s "
                        "WHERE id=%s",
                        (
                            db_value, unit, confidence,
                            accession_no, form, filed_date, existing_id,
                        ),
                    )
                conn.commit()
                total_updated += 1
                seen.add(key)
                logger.info(
                    "[%s] Updated %s %s/%s: %s→%s  value=%s",
                    doc_ticker, kpi_name, metric.period, metric.segment,
                    existing_conf, confidence, value,
                )
                continue

            # Fresh insert
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO kpis (
                        ticker, cik, kpi, segment, kpi_category, industry,
                        period_end, fiscal_period, value, unit,
                        source_accession, source_form, source_filed_date, confidence
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (ticker, kpi, segment, period_end, source_accession) DO NOTHING
                    """,
                    (
                        doc_ticker, cik,
                        kpi_name, metric.segment,
                        None,      # kpi_category — nullable; not extracted in Step A
                        industry,
                        metric.period_end, metric.fiscal_period,
                        db_value, unit,
                        accession_no, form, filed_date,
                        confidence,
                    ),
                )
                if cur.rowcount:
                    total_inserted += 1
            conn.commit()
            seen.add(key)
            logger.info(
                "[%s] Inserted %s %s/%s = %s (%s)",
                doc_ticker, kpi_name, metric.period, metric.segment, value, confidence,
            )

    logger.info(
        "KPI extraction complete: %d inserted, %d updated.",
        total_inserted, total_updated,
    )
    return total_inserted + total_updated


if __name__ == "__main__":
    db_url         = os.environ.get("DB_URL")
    anthropic_key  = os.environ.get("ANTHROPIC_API_KEY")
    voyage_api_key = os.environ.get("VOYAGE_API_KEY")

    if not db_url:         raise RuntimeError("DB_URL not set")
    if not anthropic_key:  raise RuntimeError("ANTHROPIC_API_KEY not set")
    if not voyage_api_key: raise RuntimeError("VOYAGE_API_KEY not set")

    import voyageai
    from pgvector.psycopg2 import register_vector

    _conn   = psycopg2.connect(db_url)
    _client = anthropic.Anthropic(api_key=anthropic_key)
    _vc     = voyageai.Client(api_key=voyage_api_key)

    register_vector(_conn)

    total = extract_and_seed(_conn, _client, _vc)
    _conn.close()
    print(f"Done — {total} KPI rows inserted/updated.")
