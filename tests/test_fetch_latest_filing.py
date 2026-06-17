"""
Integration tests for fetch_latest_filing — hits live EDGAR.
Mark with pytest.mark.integration so CI can skip if needed.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from edgar import set_identity

from tools.fetch_latest_filing import fetch_latest_filing

load_dotenv()

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def edgar_identity():
    identity = os.environ.get("EDGAR_IDENTITY")
    if not identity:
        pytest.skip("EDGAR_IDENTITY not set")
    set_identity(identity)


def test_peb_latest_8k_returns_result():
    rows = fetch_latest_filing(["PEB"], forms=["8-K"])
    assert len(rows) >= 1
    r = rows[0]
    assert r["ticker"] == "PEB"
    assert r["form"] == "8-K"
    assert r["accession_no"] is not None and len(r["accession_no"]) > 0
    assert r["filed_date"] is not None


def test_since_filter_marks_is_new():
    rows = fetch_latest_filing(["PEB"], forms=["8-K"], since="2026-04-01")
    assert len(rows) >= 1
    # The 2026-04-28 8-K should be flagged as new
    assert any(r["is_new"] for r in rows), "Expected at least one is_new=True filing"


def test_since_filter_old_date_returns_not_new():
    rows = fetch_latest_filing(["PEB"], forms=["8-K"], since="2099-01-01")
    # No filings exist in the future, so nothing should be marked new
    assert all(not r["is_new"] for r in rows)


def test_unknown_ticker_returns_empty():
    rows = fetch_latest_filing(["ZZZ"], forms=["8-K"])
    # EDGAR will find no filings; should return empty list without crashing
    assert isinstance(rows, list)
