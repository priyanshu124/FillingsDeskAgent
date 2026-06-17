from __future__ import annotations

from typing import Any

import psycopg2

Row = dict[str, Any]


def _edgar_url(cik: str, accession: str) -> str:
    cik_int = str(int(cik))  # strip leading zeros
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"


def query_financials(
    conn: psycopg2.extensions.connection,
    tickers: list[str],
    line_items: list[str] | None = None,
    statement: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> list[Row]:
    """
    Parameterized SQL over the statements table.

    Returns rows with full provenance: source_form, source_accession, source_filed_date.
    All values from callers enter only through %s parameters — never into SQL text.
    """
    clauses: list[str] = ["ticker = ANY(%s)"]
    params: list[Any] = [tickers]

    if line_items:
        clauses.append("line_item = ANY(%s)")
        params.append(line_items)
    if statement:
        clauses.append("statement = %s")
        params.append(statement)
    if period_start:
        clauses.append("period_end >= %s")
        params.append(period_start)
    if period_end:
        clauses.append("period_end <= %s")
        params.append(period_end)

    sql = (
        "SELECT ticker, cik, statement, line_item, period_end, fiscal_period, "
        "value, unit, is_derived, source_form, source_accession, source_filed_date "
        "FROM statements_standalone WHERE "
        + " AND ".join(clauses)
        + " ORDER BY period_end DESC, ticker, line_order"
    )

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["url"] = _edgar_url(r["cik"], r["source_accession"])
    return rows
