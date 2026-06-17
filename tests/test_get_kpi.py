"""
Direct tests for get_kpi against the seed DB.
Seed has 2 rows: PEB hotel_ebitdare and adj_ffo_per_share for Q1 2026 (segment=total).
"""

from __future__ import annotations

import os

import psycopg2
import pytest
from dotenv import load_dotenv

from tools.get_kpi import get_kpi

load_dotenv()


@pytest.fixture(scope="module")
def conn():
    db_url = os.environ.get("DB_URL")
    if not db_url:
        pytest.skip("DB_URL not set — seed DB required for these tests")
    c = psycopg2.connect(db_url)
    yield c
    c.close()


def test_peb_ebitdare_has_provenance(conn):
    rows = get_kpi(conn, tickers=["PEB"], kpis=["hotel_ebitdare"])
    assert len(rows) > 0, "Expected hotel_ebitdare rows for PEB in the seed DB"
    for r in rows:
        assert r["ticker"] == "PEB"
        assert r["kpi"] == "hotel_ebitdare"
        assert r["source_accession"] is not None
        assert r["source_form"] is not None
        assert r["source_filed_date"] is not None
        # KPIs come from 8-K earnings releases, not periodic XBRL filings
        assert r["source_form"] == "8-K", (
            f"Expected 8-K source_form for KPI, got {r['source_form']!r}"
        )


def test_adj_ffo_per_share_seeded(conn):
    rows = get_kpi(conn, tickers=["PEB"], kpis=["adj_ffo_per_share"])
    assert len(rows) >= 1
    # The hand-seeded Q1 2026 row must be present with exact provenance
    q1_rows = [r for r in rows if r["fiscal_period"] == "Q1" and float(r["value"]) == pytest.approx(0.32)]
    assert len(q1_rows) >= 1, "Expected hand-seeded Q1 2026 adj_ffo_per_share=0.32 in DB"
    assert q1_rows[0]["unit"] == "USD/share"


def test_empty_for_unknown_kpi(conn):
    rows = get_kpi(conn, tickers=["PEB"], kpis=["nonexistent_metric"])
    assert rows == []


def test_segment_filter_total_returns_rows(conn):
    rows = get_kpi(conn, tickers=["PEB"], segment="total")
    assert len(rows) > 0


def test_segment_filter_total_vs_urban(conn):
    total_rows = get_kpi(conn, tickers=["PEB"], segment="total")
    urban_rows = get_kpi(conn, tickers=["PEB"], segment="urban")
    # total must have more rows than urban (urban may or may not exist after extraction)
    assert len(total_rows) >= len(urban_rows)


def test_empty_for_unknown_ticker(conn):
    rows = get_kpi(conn, tickers=["ZZZ"])
    assert rows == []
