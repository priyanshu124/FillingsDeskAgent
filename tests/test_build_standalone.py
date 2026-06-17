"""
Tests for build_standalone.

Uses the real seed DB (DB_URL required). Each test inserts a synthetic ticker 'TST'
into statements, runs build_standalone (which truncates + rebuilds statements_standalone
from all of statements), then asserts on the TST rows and cleans up.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from decimal import Decimal

import psycopg2
import pytest
from dotenv import load_dotenv
from psycopg2.extras import execute_values

from scripts.build_standalone import build_standalone

load_dotenv()


@pytest.fixture(scope="module")
def conn():
    db_url = os.environ.get("DB_URL")
    if not db_url:
        pytest.skip("DB_URL not set — seed DB required for these tests")
    c = psycopg2.connect(db_url)
    yield c
    c.close()


def _insert_statements(conn, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO statements
                (ticker, cik, statement, line_item, line_order, period_end,
                 fiscal_period, value, unit, source_concept, source_accession,
                 source_form, source_filed_date)
            VALUES %s
            ON CONFLICT DO NOTHING
            """,
            rows,
        )
    conn.commit()


def _cleanup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM statements WHERE ticker = 'TST'")
        cur.execute("DELETE FROM statements_standalone WHERE ticker = 'TST'")
    conn.commit()


def test_q2_derivation(conn):
    """Q2 standalone = H1 cumulative − Q1; is_derived=True; source_accession_prior=Q1 accession."""
    _cleanup(conn)
    _insert_statements(conn, [
        # Q1: $100M revenue (3-month, standalone)
        ("TST", "9999999", "income", "revenues", 10,
         date(2025, 3, 31), "Q1", Decimal("100000000"), "USD",
         "us-gaap:Revenues", "ACCTEST_Q1", "10-Q", date(2025, 5, 1)),
        # H1 cumulative: $220M (labeled Q2 by EDGAR)
        ("TST", "9999999", "income", "revenues", 10,
         date(2025, 6, 30), "Q2", Decimal("220000000"), "USD",
         "us-gaap:Revenues", "ACCTEST_Q2", "10-Q", date(2025, 8, 1)),
    ])

    build_standalone(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT fiscal_period, value, is_derived, source_accession_prior "
            "FROM statements_standalone "
            "WHERE ticker = 'TST' AND statement = 'income' AND line_item = 'revenues' "
            "ORDER BY period_end",
        )
        rows = cur.fetchall()

    assert len(rows) == 2, f"Expected 2 rows, got {rows}"
    q1_fp, q1_val, q1_derived, q1_prior = rows[0]
    q2_fp, q2_val, q2_derived, q2_prior = rows[1]

    assert q1_fp == "Q1"
    assert q1_val == Decimal("100000000")
    assert q1_derived is False
    assert q1_prior is None

    assert q2_fp == "Q2"
    assert q2_val == Decimal("120000000"), f"Q2 standalone should be 220M−100M=120M, got {q2_val}"
    assert q2_derived is True
    assert q2_prior == "ACCTEST_Q1", f"source_accession_prior should be Q1 accession, got {q2_prior!r}"

    _cleanup(conn)


def test_balance_sheet_passthrough(conn):
    """Balance sheet rows copy to standalone unchanged with is_derived=False."""
    _cleanup(conn)
    _insert_statements(conn, [
        ("TST", "9999999", "balance", "total_assets", 10,
         date(2025, 3, 31), "Q1", Decimal("5000000000"), "USD",
         "us-gaap:Assets", "ACCTEST_BAL", "10-Q", date(2025, 5, 1)),
    ])

    build_standalone(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT value, is_derived, source_accession_prior "
            "FROM statements_standalone "
            "WHERE ticker = 'TST' AND statement = 'balance'",
        )
        row = cur.fetchone()

    assert row is not None, "Balance sheet row should appear in statements_standalone"
    val, is_derived, prior = row
    assert val == Decimal("5000000000")
    assert is_derived is False
    assert prior is None

    _cleanup(conn)


def test_missing_prior_skipped(conn, caplog):
    """A Q3 row with no Q1/Q2 in DB must not appear in standalone (never a NULL or wrong value)."""
    _cleanup(conn)
    _insert_statements(conn, [
        # Only Q3 (9M cumulative) — no Q1 or Q2 to subtract from
        ("TST", "9999999", "income", "revenues", 10,
         date(2025, 9, 30), "Q3", Decimal("300000000"), "USD",
         "us-gaap:Revenues", "ACCTEST_Q3", "10-Q", date(2025, 11, 1)),
    ])

    with caplog.at_level(logging.WARNING, logger="scripts.build_standalone"):
        build_standalone(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM statements_standalone "
            "WHERE ticker = 'TST' AND statement = 'income'",
        )
        count = cur.fetchone()[0]

    assert count == 0, (
        "Orphaned Q3 (missing Q1/Q2) must be skipped — "
        f"got {count} rows instead of 0"
    )

    warning_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("TST" in m and "revenues" in m for m in warning_messages), (
        f"Expected a WARNING mentioning 'TST' and 'revenues'; got: {warning_messages}"
    )

    _cleanup(conn)
