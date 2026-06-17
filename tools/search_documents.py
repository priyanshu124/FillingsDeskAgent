from __future__ import annotations

import logging
from typing import Any

import psycopg2

logger = logging.getLogger(__name__)

Row = dict[str, Any]

VOYAGE_MODEL = "voyage-finance-2"


def _edgar_url(cik: str, accession: str) -> str:
    cik_int = str(int(cik))
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"


def search_documents(
    conn: psycopg2.extensions.connection,
    voyage_client: Any,
    query: str,
    tickers: list[str] | None = None,
    forms: list[str] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    k: int = 5,
) -> list[Row]:
    """
    Hybrid metadata-filter + pgvector similarity search over doc_chunks.

    Embeds `query` via Voyage AI, then runs a single SQL query combining
    WHERE filters (metadata) and ORDER BY cosine distance (vector similarity).

    Returns up to k chunks with text, filing metadata, and similarity score.
    All filter values pass through %s parameters — never into SQL text.
    """
    try:
        from pgvector.psycopg2 import register_vector
        register_vector(conn)
    except ImportError:
        raise RuntimeError(
            "pgvector package not installed. Run: pip install pgvector"
        )

    # Check if any embeddings exist before calling Voyage API
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM doc_chunks WHERE embedding IS NOT NULL")
        count = cur.fetchone()[0]
    if count == 0:
        logger.warning(
            "doc_chunks has no embeddings — run scripts/index_documents.py first"
        )
        return []

    embedding = voyage_client.embed([query], model=VOYAGE_MODEL).embeddings[0]

    clauses: list[str] = ["dc.embedding IS NOT NULL"]
    params: list[Any] = []

    if tickers:
        clauses.append("dc.ticker = ANY(%s)")
        params.append(tickers)
    if forms:
        clauses.append("dc.form = ANY(%s)")
        params.append(forms)
    if period_start:
        clauses.append("dc.filed_date >= %s")
        params.append(period_start)
    if period_end:
        clauses.append("dc.filed_date <= %s")
        params.append(period_end)

    where = " AND ".join(clauses)

    # Embedding passed twice: once for score in SELECT, once for ORDER BY
    sql = (
        "SELECT dc.chunk_id, dc.text, dc.ticker, dc.form, dc.filed_date, "
        "dc.fiscal_period, d.title AS doc_title, d.cik, d.accession_no, "
        "1 - (dc.embedding <=> %s::vector) AS score "
        "FROM doc_chunks dc "
        "JOIN documents d ON d.doc_id = dc.doc_id "
        f"WHERE {where} "
        "ORDER BY dc.embedding <=> %s::vector "
        "LIMIT %s"
    )
    # params order: embedding (score), filter params, embedding (order), k
    full_params = [embedding] + params + [embedding, k]

    with conn.cursor() as cur:
        cur.execute(sql, full_params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["url"] = _edgar_url(r["cik"], r["accession_no"])
    return rows
