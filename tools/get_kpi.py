from __future__ import annotations

from typing import Any

import psycopg2

Row = dict[str, Any]


def _edgar_url(cik: str, accession: str) -> str:
    cik_int = str(int(cik))
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"


def get_kpi(
    conn: psycopg2.extensions.connection,
    tickers: list[str],
    kpis: list[str] | None = None,
    segment: str = "total",
    period_start: str | None = None,
    period_end: str | None = None,
) -> list[Row]:
    """
    Parameterized SQL over the kpis table.

    Returns rows with full provenance: source_form, source_accession, source_filed_date.
    All values from callers enter only through %s parameters — never into SQL text.
    """
    clauses: list[str] = ["ticker = ANY(%s)"]
    params: list[Any] = [tickers]

    if kpis:
        clauses.append("kpi = ANY(%s)")
        params.append(kpis)

    clauses.append("segment = %s")
    params.append(segment)

    if period_start:
        clauses.append("period_end >= %s")
        params.append(period_start)
    if period_end:
        clauses.append("period_end <= %s")
        params.append(period_end)

    sql = (
        "SELECT ticker, cik, kpi, segment, period_end, fiscal_period, "
        "value, unit, source_form, source_accession, source_filed_date "
        "FROM kpis WHERE "
        + " AND ".join(clauses)
        + " ORDER BY period_end DESC, ticker, kpi"
    )

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for r in rows:
        r["url"] = _edgar_url(r["cik"], r["source_accession"])
    return rows
