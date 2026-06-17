"""
Build statements_standalone from statements by de-cumulating YTD income/cashflow values.

Income and cashflow facts from 10-Qs are YTD cumulative (Q2 = H1, Q3 = 9M).
Derives standalone quarterly values using a LAG window partitioned by fiscal year.

Fiscal year awareness:
  - FYE month is determined per company from the period_end of their most recent 10-K.
  - Fiscal year grouping: period belongs to the fiscal year whose FYE comes next.
    Formula: fiscal_year = calendar_year + 1 if period_end.month > fye_month.
  - Period ordering within fiscal year uses ROW_NUMBER() over period_end date,
    replacing the old hardcoded CASE (WHEN month=3 THEN 1 ...).
  - Q1 pass-through is gated on the period genuinely being fiscal Q1
    (period_end month = q1_month = fye_month - 9 mod 12).
  - Q2/Q3/Q4 derived rows are only emitted for fiscal years that have a genuine Q1
    in the DB. Fiscal years starting mid-window are skipped rather than mislabeled.

Balance sheet rows are point-in-time and pass through unchanged.
Rows where the prior period is absent are skipped and logged as warnings.

Idempotent: TRUNCATE + INSERT on every run.
"""
from __future__ import annotations

import logging
import os

import psycopg2
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS statements_standalone (
    id                     BIGSERIAL  PRIMARY KEY,
    ticker                 TEXT       NOT NULL,
    cik                    TEXT       NOT NULL,
    statement              TEXT       NOT NULL CHECK (statement IN ('income', 'balance', 'cashflow')),
    line_item              TEXT       NOT NULL,
    line_order             INT        NOT NULL,
    period_end             DATE       NOT NULL,
    fiscal_period          TEXT,
    value                  NUMERIC    NOT NULL,
    unit                   TEXT       NOT NULL DEFAULT 'USD',
    source_concept         TEXT       NOT NULL,
    source_accession       TEXT       NOT NULL,
    source_form            TEXT       NOT NULL,
    source_filed_date      DATE       NOT NULL,
    is_derived             BOOLEAN    NOT NULL DEFAULT FALSE,
    source_accession_prior TEXT,
    UNIQUE (ticker, statement, line_item, period_end, fiscal_period)
)
"""

_CREATE_INDEX_SQLS = [
    "CREATE INDEX IF NOT EXISTS idx_standalone_item_period ON statements_standalone (ticker, line_item, period_end)",
    "CREATE INDEX IF NOT EXISTS idx_standalone_period ON statements_standalone (period_end, ticker)",
]

_INSERT_SQL = """
INSERT INTO statements_standalone (
    ticker, cik, statement, line_item, line_order, period_end, fiscal_period,
    value, unit, source_concept, source_accession, source_form, source_filed_date,
    is_derived, source_accession_prior
)
WITH company_fy AS (
    -- FYE month + Q1 month per company, derived from their most recent 10-K period_end.
    -- q1_month = fye_month - 9 (mod 12, 1-based): first quarter of the fiscal year.
    -- Companies without a 10-K in the window are excluded from income/cashflow
    -- de-cumulation (same conservative behavior as the old hardcoded-month approach).
    SELECT DISTINCT ON (ticker, cik)
        ticker, cik,
        EXTRACT(MONTH FROM period_end)::INT AS fye_month,
        CASE WHEN EXTRACT(MONTH FROM period_end)::INT > 9
             THEN EXTRACT(MONTH FROM period_end)::INT - 9
             ELSE EXTRACT(MONTH FROM period_end)::INT + 3
        END AS q1_month
    FROM statements
    WHERE source_form = '10-K'
    ORDER BY ticker, cik, source_filed_date DESC
),
deduped AS (
    -- One row per (ticker, statement, line_item, period_end): latest filing wins.
    SELECT DISTINCT ON (s.ticker, s.cik, s.statement, s.line_item, s.period_end)
        s.*, cf.fye_month, cf.q1_month
    FROM statements s
    JOIN company_fy cf ON cf.ticker = s.ticker AND cf.cik = s.cik
    WHERE s.statement IN ('income', 'cashflow')
    ORDER BY s.ticker, s.cik, s.statement, s.line_item, s.period_end,
             s.source_filed_date DESC
),
with_fiscal_meta AS (
    SELECT *,
        -- Fiscal year: increment calendar year when period falls after the FYE month.
        -- Example (INTU, fye=7): Oct 2024 (month 10 > 7) → fiscal_year 2025.
        -- Calendar-FY companies (fye=12): no month > 12, so fiscal_year = calendar year.
        CASE WHEN EXTRACT(MONTH FROM period_end)::INT > fye_month
             THEN EXTRACT(YEAR FROM period_end)::INT + 1
             ELSE EXTRACT(YEAR FROM period_end)::INT
        END AS fiscal_year
    FROM deduped
),
with_period_num AS (
    SELECT *,
        -- ROW_NUMBER (not RANK) guarantees a clean 1/2/3/4 sequence after dedup.
        ROW_NUMBER() OVER (
            PARTITION BY ticker, cik, statement, line_item, fiscal_year
            ORDER BY period_end
        ) AS period_num
    FROM with_fiscal_meta
),
fiscal_years_with_q1 AS (
    -- Fiscal years that have their genuine Q1 present in the DB.
    -- Gates Q2/Q3/Q4 derivation: if the window starts mid-year and the true Q1 is
    -- absent, derived rows are skipped rather than mislabeled (e.g. YTD H1 emitted
    -- as a fake "Q1 standalone"). Honest degradation over silently wrong values.
    SELECT DISTINCT ticker, cik, statement, line_item, fiscal_year
    FROM with_period_num
    WHERE period_num = 1
      AND EXTRACT(MONTH FROM period_end)::INT = q1_month
),
with_prior AS (
    SELECT *,
        LAG(value)            OVER w AS prior_value,
        LAG(source_accession) OVER w AS prior_accession
    FROM with_period_num
    WINDOW w AS (
        PARTITION BY ticker, cik, statement, line_item, fiscal_year
        ORDER BY period_num
    )
)
-- Q1: pass-through only when the period is the genuine fiscal Q1
SELECT ticker, cik, statement, line_item, line_order, period_end,
       'Q1', value, unit, source_concept, source_accession, source_form,
       source_filed_date, FALSE, NULL
FROM with_prior
WHERE period_num = 1
  AND EXTRACT(MONTH FROM period_end)::INT = q1_month

UNION ALL

-- Q2/Q3/Q4: derived; only for fiscal years with a genuine Q1, and only when prior exists
SELECT ticker, cik, statement, line_item, line_order, period_end,
       CASE period_num WHEN 2 THEN 'Q2' WHEN 3 THEN 'Q3' WHEN 4 THEN 'Q4' END,
       value - prior_value, unit, source_concept, source_accession, source_form,
       source_filed_date, TRUE, prior_accession
FROM with_prior
JOIN fiscal_years_with_q1 USING (ticker, cik, statement, line_item, fiscal_year)
WHERE period_num IN (2, 3, 4) AND prior_value IS NOT NULL

UNION ALL

-- FY: pass-through for annual queries (fiscal_period='FY', distinct from Q4 above)
SELECT ticker, cik, statement, line_item, line_order, period_end,
       'FY', value, unit, source_concept, source_accession, source_form,
       source_filed_date, FALSE, NULL
FROM with_prior WHERE period_num = 4

UNION ALL

-- Balance sheet: point-in-time, copy unchanged (no de-cumulation needed)
SELECT ticker, cik, statement, line_item, line_order, period_end,
       fiscal_period, value, unit, source_concept, source_accession, source_form,
       source_filed_date, FALSE, NULL
FROM statements WHERE statement = 'balance'
"""

# Detect income/cashflow rows that did not make it into statements_standalone.
# No month filter — covers all companies regardless of fiscal year structure.
_SKIPPED_SQL = """
SELECT s.ticker, s.line_item, s.period_end
FROM statements s
WHERE s.statement IN ('income', 'cashflow')
  AND NOT EXISTS (
      SELECT 1 FROM statements_standalone ss
      WHERE ss.ticker    = s.ticker
        AND ss.statement = s.statement
        AND ss.line_item = s.line_item
        AND ss.period_end = s.period_end
  )
ORDER BY s.ticker, s.line_item, s.period_end
"""


def build_standalone(conn: psycopg2.extensions.connection) -> int:
    """
    Truncate and rebuild statements_standalone from statements.

    Creates the table and indexes if they don't exist yet (safe on existing DBs).
    Returns the number of rows inserted.
    """
    with conn.cursor() as cur:
        cur.execute(_CREATE_TABLE_SQL)
        for idx_sql in _CREATE_INDEX_SQLS:
            cur.execute(idx_sql)
        cur.execute("TRUNCATE statements_standalone")
        cur.execute(_INSERT_SQL)
        cur.execute("SELECT COUNT(*) FROM statements_standalone")
        n = cur.fetchone()[0]

    conn.commit()
    logger.info("statements_standalone: %d rows built", n)

    with conn.cursor() as cur:
        cur.execute(_SKIPPED_SQL)
        skipped = cur.fetchall()

    for ticker, line_item, period_end in skipped:
        logger.warning(
            "skipped %s %s %s, prior period missing",
            ticker, line_item, period_end,
        )
    if skipped:
        logger.warning("%d row(s) skipped — prior period absent in DB", len(skipped))

    return n


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    db_url = os.environ.get("DB_URL")
    if not db_url:
        raise RuntimeError("DB_URL not set — add it to .env")
    _conn = psycopg2.connect(db_url)
    count = build_standalone(_conn)
    _conn.close()
    print(f"statements_standalone: {count} rows built")
