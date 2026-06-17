"""
Tests for compare_peers against the seed DB.
"""

from __future__ import annotations

import os

import psycopg2
import pytest
from dotenv import load_dotenv

from tools.compare_peers import compare_peers

load_dotenv()


@pytest.fixture(scope="module")
def conn():
    db_url = os.environ.get("DB_URL")
    if not db_url:
        pytest.skip("DB_URL not set")
    c = psycopg2.connect(db_url)
    yield c
    c.close()


PEER_TICKERS  = ["PEB", "HST", "SHO"]
FOCUS_TICKER  = "PEB"


def test_compare_net_income_returns_peers(conn):
    rows = compare_peers(
        conn, tickers=PEER_TICKERS, focus_ticker=FOCUS_TICKER,
        metric="net_income", metric_kind="statement", period_end="2026-03-31",
    )
    assert len(rows) > 0
    assert "PEB" in {r["ticker"] for r in rows}


def test_exactly_one_focus_row(conn):
    rows = compare_peers(
        conn, tickers=PEER_TICKERS, focus_ticker=FOCUS_TICKER,
        metric="net_income", metric_kind="statement", period_end="2026-03-31",
    )
    focus = [r for r in rows if r["is_focus"]]
    assert len(focus) == 1
    assert focus[0]["ticker"] == "PEB"


def test_sorted_descending_by_value(conn):
    rows = compare_peers(
        conn, tickers=PEER_TICKERS, focus_ticker=FOCUS_TICKER,
        metric="revenues", metric_kind="statement", period_end="2026-03-31",
    )
    values = [float(r["value"]) for r in rows if r["value"] is not None]
    assert values == sorted(values, reverse=True), "Rows should be sorted by value descending"


def test_compare_kpi_ebitdare(conn):
    rows = compare_peers(
        conn, tickers=PEER_TICKERS, focus_ticker=FOCUS_TICKER,
        metric="hotel_ebitdare", metric_kind="kpi", period_end="2026-03-31",
    )
    assert len(rows) >= 1
    peb_row = next((r for r in rows if r["ticker"] == "PEB"), None)
    assert peb_row is not None
    assert peb_row["is_focus"] is True
    assert peb_row["source_form"] == "8-K"


def test_provenance_fields_present(conn):
    rows = compare_peers(
        conn, tickers=PEER_TICKERS, focus_ticker=FOCUS_TICKER,
        metric="total_assets", metric_kind="statement", period_end="2026-03-31",
    )
    for r in rows:
        assert r["source_accession"] is not None
        assert r["source_form"] is not None
        assert r["source_filed_date"] is not None
