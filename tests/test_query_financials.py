"""
Direct tests for query_financials against the seed DB.
All tests hit the real database — no mocks.
Set DB_URL in the environment (or .env) before running.
"""

from __future__ import annotations

import os

import psycopg2
import pytest
from dotenv import load_dotenv

from tools.query_financials import query_financials

load_dotenv()


@pytest.fixture(scope="module")
def conn():
    db_url = os.environ.get("DB_URL")
    if not db_url:
        pytest.skip("DB_URL not set — seed DB required for these tests")
    c = psycopg2.connect(db_url)
    yield c
    c.close()


def test_peb_net_income_has_provenance(conn):
    rows = query_financials(conn, tickers=["PEB"], line_items=["net_income"])
    assert len(rows) > 0, "Expected net_income rows for PEB in the seed DB"
    for r in rows:
        assert r["ticker"] == "PEB"
        assert r["line_item"] == "net_income"
        assert r["source_accession"] is not None, "source_accession must not be null"
        assert r["source_form"] is not None, "source_form must not be null"
        assert r["source_filed_date"] is not None, "source_filed_date must not be null"
        # GAAP net income must come from a periodic filing, not an 8-K
        assert r["source_form"] in ("10-K", "10-Q"), (
            f"Expected periodic form, got {r['source_form']!r}"
        )


def test_empty_result_for_unknown_ticker(conn):
    rows = query_financials(conn, tickers=["ZZZ"])
    assert rows == [], f"Expected empty list for unknown ticker, got {rows}"


def test_period_filter_narrows_results(conn):
    all_rows = query_financials(conn, tickers=["PEB"], line_items=["revenues"])
    filtered = query_financials(
        conn,
        tickers=["PEB"],
        line_items=["revenues"],
        period_start="2026-01-01",
        period_end="2026-03-31",
    )
    assert len(filtered) <= len(all_rows)
    for r in filtered:
        assert str(r["period_end"]) <= "2026-03-31"
        assert str(r["period_end"]) >= "2026-01-01"


def test_statement_filter(conn):
    rows = query_financials(conn, tickers=["PEB"], statement="balance")
    assert len(rows) > 0
    for r in rows:
        assert r["statement"] == "balance"


def test_multi_ticker(conn):
    rows = query_financials(
        conn, tickers=["PEB", "HST"], line_items=["total_assets"]
    )
    tickers_returned = {r["ticker"] for r in rows}
    assert "PEB" in tickers_returned
    assert "HST" in tickers_returned
