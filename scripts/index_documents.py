"""
Load SEC filing text into doc_chunks, embed via Voyage AI, build HNSW index.

Seed corpus (all three tickers):
  PEB  10-Q Q1 2026  — filed 2026-04-28
  PEB  8-K  Q1 2026  — filed 2026-04-28  (EX-99.1 earnings release)
  PEB  8-K  FY2025   — filed 2026-02-25  (EX-99.1 earnings release)
  HST  10-Q Q1 2026  — most recent 10-Q  since 2026-04-01
  HST  8-K  Q1 2026  — most recent 8-K   since 2026-04-15 with EX-99.1
  HST  8-K  FY2025   — most recent 8-K   since 2026-02-01 with EX-99.1
  SHO  10-Q Q1 2026  — most recent 10-Q  since 2026-04-01
  SHO  8-K  Q1 2026  — most recent 8-K   since 2026-04-15 with EX-99.1
  SHO  8-K  FY2025   — most recent 8-K   since 2026-02-01 with EX-99.1

edgartools v5+ API confirmed:
  filing.filing_date       → date
  filing.accession_no      → str "XXXXXXXXXX-YY-ZZZZZZ"
  filing.document          → Attachment (primary document)
  filing.document.markdown() → str
  list(filing.attachments)  → [Attachment, ...]
  attachment.document_type  → str  e.g. "EX-99.1"
  attachment.markdown()     → str
  filing.report_date        → str ISO date (must fromisoformat)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from edgar import Company, set_identity

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

VOYAGE_MODEL   = "voyage-finance-2"
CHUNK_SIZE     = 1500   # chars (~375 tokens at 4 chars/token)
CHUNK_OVERLAP  = 150    # chars
VOYAGE_BATCH   = 64
EDGAR_SLEEP_S  = 0.15

# Each entry: (ticker, cik, form, since_date, until_date)
# Script finds the most recent filing of that form in [since, until].
# Tight windows avoid picking up unrelated 8-Ks.
SEED_FILINGS = [
    # ── PEB (CIK 1474098) ────────────────────────────────────────────────
    ("PEB", "1474098", "8-K",  date(2024, 4, 20), date(2024, 5, 15)),   # Q1 2024 earnings
    ("PEB", "1474098", "8-K",  date(2024, 7, 15), date(2024, 8, 15)),   # Q2 2024 earnings
    ("PEB", "1474098", "8-K",  date(2024, 10, 15), date(2024, 11, 20)), # Q3 2024 earnings
    ("PEB", "1474098", "8-K",  date(2025, 2, 15), date(2025, 3, 10)),   # FY2024 earnings
    ("PEB", "1474098", "8-K",  date(2025, 4, 15), date(2025, 5, 15)),   # Q1 2025 earnings
    ("PEB", "1474098", "8-K",  date(2025, 7, 15), date(2025, 8, 15)),   # Q2 2025 earnings
    ("PEB", "1474098", "8-K",  date(2025, 10, 15), date(2025, 11, 20)), # Q3 2025 earnings
    ("PEB", "1474098", "8-K",  date(2026, 2, 15), date(2026, 3, 5)),    # FY2025 earnings
    ("PEB", "1474098", "8-K",  date(2026, 4, 20), date(2026, 5, 10)),   # Q1 2026 earnings
    ("PEB", "1474098", "10-Q", date(2026, 4, 20), date(2026, 5, 10)),   # Q1 2026 10-Q
    # ── HST (CIK 1070750) ────────────────────────────────────────────────
    ("HST", "1070750", "8-K",  date(2024, 4, 20), date(2024, 5, 20)),   # Q1 2024 earnings
    ("HST", "1070750", "8-K",  date(2024, 7, 15), date(2024, 8, 15)),   # Q2 2024 earnings
    ("HST", "1070750", "8-K",  date(2024, 10, 15), date(2024, 11, 20)), # Q3 2024 earnings
    ("HST", "1070750", "8-K",  date(2025, 2, 10), date(2025, 3, 5)),    # FY2024 earnings
    ("HST", "1070750", "8-K",  date(2025, 4, 15), date(2025, 5, 15)),   # Q1 2025 earnings
    ("HST", "1070750", "8-K",  date(2025, 7, 15), date(2025, 8, 15)),   # Q2 2025 earnings
    ("HST", "1070750", "8-K",  date(2025, 10, 15), date(2025, 11, 20)), # Q3 2025 earnings
    ("HST", "1070750", "8-K",  date(2026, 2,  1), date(2026, 3,  5)),   # FY2025 earnings
    ("HST", "1070750", "8-K",  date(2026, 4, 15), date(2026, 5, 10)),   # Q1 2026 earnings
    ("HST", "1070750", "10-Q", date(2026, 4, 20), date(2026, 5, 15)),   # Q1 2026 10-Q
    # ── SHO (CIK 1295810) ────────────────────────────────────────────────
    ("SHO", "1295810", "8-K",  date(2024, 4, 20), date(2024, 5, 20)),   # Q1 2024 earnings
    ("SHO", "1295810", "8-K",  date(2024, 7, 15), date(2024, 8, 15)),   # Q2 2024 earnings
    ("SHO", "1295810", "8-K",  date(2024, 10, 15), date(2024, 11, 20)), # Q3 2024 earnings
    ("SHO", "1295810", "8-K",  date(2025, 2, 15), date(2025, 3, 10)),   # FY2024 earnings
    ("SHO", "1295810", "8-K",  date(2025, 4, 15), date(2025, 5, 15)),   # Q1 2025 earnings
    ("SHO", "1295810", "8-K",  date(2025, 7, 15), date(2025, 8, 15)),   # Q2 2025 earnings
    ("SHO", "1295810", "8-K",  date(2025, 10, 15), date(2025, 11, 20)), # Q3 2025 earnings
    ("SHO", "1295810", "8-K",  date(2026, 2,  1), date(2026, 3,  5)),   # FY2025 earnings
    ("SHO", "1295810", "8-K",  date(2026, 4, 15), date(2026, 5, 10)),   # Q1 2026 earnings
    ("SHO", "1295810", "10-Q", date(2026, 4, 20), date(2026, 5, 15)),   # Q1 2026 10-Q
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_fiscal_period(report_date: date, form: str) -> str:
    if form == "10-K":
        return "FY"
    return {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}.get(report_date.month, "Q?")


def _chunk(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        chunks.append(text[start:end])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _get_8k_earnings_text(filing) -> str | None:
    """Return EX-99.1 text from an 8-K, or fall back to primary doc."""
    for att in filing.attachments:
        if att.document_type == "EX-99.1":
            try:
                text = att.markdown()
                if text and text.strip():
                    return text
            except Exception as exc:
                logger.warning("EX-99.1 markdown() failed: %s", exc)
    try:
        return filing.document.markdown()
    except Exception as exc:
        logger.warning("primary document markdown() failed: %s", exc)
        return None


def _get_filing_text(filing, form: str) -> str | None:
    if form == "8-K":
        return _get_8k_earnings_text(filing)
    try:
        return filing.document.markdown()
    except Exception as exc:
        logger.warning("primary document markdown() failed: %s", exc)
        return None


def _embed_batch(vc, texts: list[str]) -> list[list[float]]:
    embeddings = []
    total = len(texts)
    for i in range(0, total, VOYAGE_BATCH):
        batch = texts[i:i + VOYAGE_BATCH]
        result = vc.embed(batch, model=VOYAGE_MODEL)
        embeddings.extend(result.embeddings)
        logger.info("  embedded %d/%d chunks", min(i + len(batch), total), total)
    return embeddings


def _ensure_hnsw_index(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'doc_chunks' AND indexname = 'idx_chunks_embedding'
        """)
        if cur.fetchone():
            logger.info("HNSW index already exists — skipping.")
            return
    logger.info("Building HNSW index on doc_chunks.embedding …")
    with conn.cursor() as cur:
        cur.execute("""
            CREATE INDEX idx_chunks_embedding ON doc_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)
    conn.commit()
    logger.info("HNSW index created.")


# ── Main ingest ───────────────────────────────────────────────────────────────

def index_one_filing(
    conn: psycopg2.extensions.connection,
    vc,
    ticker: str,
    cik: str,
    filing,
    form: str,
) -> tuple[int, int]:
    """
    Index one already-fetched edgartools Filing object.
    Returns (chunks_inserted, chunks_skipped_already_indexed).
    Idempotent: skips if the filing's doc_id is already in doc_chunks.
    Called by index_filing (seed script) and onboard_company (dynamic tool).
    """
    filed_on_raw = getattr(filing, "filing_date", None) or getattr(filing, "filed", None)
    if isinstance(filed_on_raw, str):
        filed_on = date.fromisoformat(filed_on_raw)
    else:
        filed_on = filed_on_raw or date.today()

    accession_no     = str(filing.accession_no).replace("-", "")
    accession_no_fmt = str(filing.accession_no)
    _rd = filing.report_date or filed_on
    if isinstance(_rd, str):
        _rd = date.fromisoformat(_rd)
    fiscal_period = _infer_fiscal_period(_rd, form)

    filed_str = filed_on.strftime("%Y-%m-%d")
    doc_id    = f"{accession_no}/{form}"
    title     = f"{ticker} {form} {filed_str}"

    logger.info("[%s] Indexing %s filed %s (accession %s)", ticker, form, filed_str, accession_no_fmt)

    # ── Insert document row ────────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (doc_id, ticker, cik, form, accession_no, filed_date, title, url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (doc_id) DO NOTHING
            """,
            (doc_id, ticker, cik, form, accession_no_fmt, filed_on, title,
             f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no}/"),
        )
    conn.commit()

    # ── Check if already indexed ───────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM doc_chunks WHERE doc_id = %s", (doc_id,))
        existing = cur.fetchone()[0]
    if existing:
        logger.info("[%s] %s %s already indexed (%d chunks) — skipping.", ticker, form, filed_str, existing)
        return 0, existing

    # ── Get text ───────────────────────────────────────────────────────────────
    text = _get_filing_text(filing, form)
    if not text or not text.strip():
        logger.warning("[%s] Empty text for %s %s — skipping chunks.", ticker, form, filed_str)
        return 0, 0

    chunks = _chunk(text)
    logger.info("[%s] %s %s → %d chunks (text len=%d)", ticker, form, filed_str, len(chunks), len(text))

    # ── Embed ──────────────────────────────────────────────────────────────────
    embeddings = _embed_batch(vc, chunks)

    # ── Insert doc_chunks ─────────────────────────────────────────────────────
    rows = []
    for idx, (chunk_text, emb) in enumerate(zip(chunks, embeddings)):
        chunk_id = f"{doc_id}/{idx}"
        rows.append((
            chunk_id, doc_id, idx, chunk_text, emb,
            ticker, form, filed_on, fiscal_period,
        ))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO doc_chunks
                (chunk_id, doc_id, chunk_index, text, embedding,
                 ticker, form, filed_date, fiscal_period)
            VALUES %s
            ON CONFLICT (doc_id, chunk_index) DO NOTHING
            """,
            rows,
        )
    conn.commit()
    logger.info("[%s] %s %s → %d chunks inserted.", ticker, form, filed_str, len(rows))
    return len(rows), 0


def index_filing(
    conn: psycopg2.extensions.connection,
    vc,
    ticker: str,
    cik: str,
    form: str,
    since: date,
    until: date,
) -> tuple[int, int]:
    """
    Find the most recent filing of `form` in [since, until], embed, store.
    Returns (chunks_inserted, chunks_skipped_already_indexed).
    """
    since_str = since.strftime("%Y-%m-%d")
    until_str = until.strftime("%Y-%m-%d")
    logger.info("[%s] Looking for %s filed %s – %s …", ticker, form, since_str, until_str)

    filings = Company(ticker).get_filings(form=form).filter(date=f"{since_str}:{until_str}")
    if not filings:
        logger.warning("[%s] No %s found in [%s, %s] — skipping.", ticker, form, since_str, until_str)
        return 0, 0

    return index_one_filing(conn, vc, ticker, cik, filings[0], form)


def main() -> None:
    edgar_identity = os.environ.get("EDGAR_IDENTITY")
    if not edgar_identity:
        raise RuntimeError("EDGAR_IDENTITY env var not set.")
    db_url = os.environ.get("DB_URL")
    if not db_url:
        raise RuntimeError("DB_URL env var not set.")
    voyage_api_key = os.environ.get("VOYAGE_API_KEY")
    if not voyage_api_key:
        raise RuntimeError("VOYAGE_API_KEY env var not set.")

    import voyageai
    from pgvector.psycopg2 import register_vector

    set_identity(edgar_identity)
    vc = voyageai.Client(api_key=voyage_api_key)

    conn = psycopg2.connect(db_url)
    register_vector(conn)
    logger.info("Connected to DB, Voyage client ready.")
    logger.info("Indexing %d filings across PEB, HST, SHO …", len(SEED_FILINGS))

    total_inserted = 0
    total_skipped  = 0
    for i, (ticker, cik, form, since, until) in enumerate(SEED_FILINGS):
        inserted, skipped = index_filing(conn, vc, ticker, cik, form, since, until)
        total_inserted += inserted
        total_skipped  += skipped
        if i < len(SEED_FILINGS) - 1:
            time.sleep(EDGAR_SLEEP_S)

    _ensure_hnsw_index(conn)
    conn.close()

    logger.info("── Index complete ────────────────────────────────────────────")
    logger.info("  chunks inserted : %d", total_inserted)
    logger.info("  chunks skipped  : %d (already indexed)", total_skipped)


if __name__ == "__main__":
    main()
