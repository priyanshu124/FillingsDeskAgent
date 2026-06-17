from __future__ import annotations


def list_companies(conn) -> list[dict]:
    """Return all companies in the database with their data-through dates."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.ticker, c.name, c.industry,
                   s.xbrl_loaded_through, s.docs_indexed_through
            FROM companies c
            LEFT JOIN company_data_status s ON c.ticker = s.ticker
            ORDER BY c.ticker
        """)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        for k in ("xbrl_loaded_through", "docs_indexed_through"):
            if r.get(k):
                r[k] = str(r[k])
    return rows
