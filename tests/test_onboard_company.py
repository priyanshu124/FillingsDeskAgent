"""
Unit tests for tools/onboard_company.py.

EDGAR, Voyage, Anthropic, and all script helpers are mocked — no network calls.
Tests verify orchestration sequence and partial-failure handling.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch, call

import pytest

from tools.onboard_company import onboard_company, _sic_to_industry, _period_window


# ── Helper fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: cursor
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.return_value = (0,)   # _has_noncalendar_fy → False by default
    conn.cursor.return_value = cursor
    return conn


@pytest.fixture
def mock_voyage():
    return MagicMock()


@pytest.fixture
def mock_claude():
    return MagicMock()


# ── _sic_to_industry ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("sic,expected", [
    ("6798", "real_estate"),
    ("5912", "retail_wholesale"),
    ("7372", "services"),
    ("3674", "manufacturing"),
    ("8099", "healthcare"),
    ("9999", "other"),
    ("",     "other"),
])
def test_sic_to_industry(sic, expected):
    assert _sic_to_industry(sic) == expected


# ── _period_window ─────────────────────────────────────────────────────────────

def test_period_window_returns_valid_dates():
    start, end = _period_window(8)
    assert start < end
    assert end.month in (3, 6, 9, 12)
    assert start.month in (1, 4, 7, 10)   # quarter start months
    delta_months = (end.year - start.year) * 12 + (end.month - start.month)
    assert 20 <= delta_months <= 28        # roughly 2 years


# ── Happy path ────────────────────────────────────────────────────────────────

@patch("tools.onboard_company.extract_and_seed")
@patch("tools.onboard_company.index_one_filing")
@patch("tools.onboard_company.build_standalone")
@patch("tools.onboard_company.ingest_company")
@patch("tools.onboard_company._has_noncalendar_fy")
@patch("tools.onboard_company.register_vector")
@patch("tools.onboard_company.Company")
def test_onboard_happy_path(
    mock_company_cls,
    mock_register_vector,
    mock_noncal,
    mock_ingest,
    mock_build,
    mock_index,
    mock_extract,
    mock_conn,
    mock_voyage,
    mock_claude,
):
    # Configure mocks
    entity = MagicMock()
    entity.cik  = "1234567"
    entity.name = "Acme Corp"
    entity.sic  = "7372"
    entity.get_filings.return_value.filter.return_value = [MagicMock(), MagicMock()]
    mock_company_cls.return_value = entity

    mock_noncal.return_value  = False
    mock_ingest.return_value  = {"raw_rows": 200, "statement_rows": 60, "warnings": []}
    mock_build.return_value   = 90
    mock_index.return_value   = (25, 0)
    mock_extract.return_value = 8

    result = onboard_company(
        mock_conn, ticker="ACME",
        voyage_client=mock_voyage, claude_client=mock_claude,
    )

    assert result["success"] is True
    assert result["ticker"]  == "ACME"
    assert result["cik"]     == "1234567"
    assert result["name"]    == "Acme Corp"
    assert result["industry"] == "services"
    assert result["statements_rows"] == 60
    assert result["kpis_rows"]       == 8
    assert result["warnings"]        == []

    # Verify call sequence
    mock_ingest.assert_called_once()
    mock_build.assert_called_once()
    # 2 forms × 2 filings each = 4 index calls
    assert mock_index.call_count == 4
    mock_extract.assert_called_once_with(mock_conn, mock_claude, ticker="ACME")


# ── Ticker resolution failure ──────────────────────────────────────────────────

@patch("tools.onboard_company.Company")
def test_bad_ticker_returns_error_dict(mock_company_cls, mock_conn, mock_voyage, mock_claude):
    mock_company_cls.side_effect = Exception("Ticker not found on EDGAR")

    result = onboard_company(
        mock_conn, ticker="ZZZZZ",
        voyage_client=mock_voyage, claude_client=mock_claude,
    )

    assert result["success"] is False
    assert "error" in result
    assert result["ticker"] == "ZZZZZ"
    # No DB writes should have occurred
    mock_conn.commit.assert_not_called()


# ── Empty CIK ─────────────────────────────────────────────────────────────────

@patch("tools.onboard_company.Company")
def test_empty_cik_returns_error(mock_company_cls, mock_conn, mock_voyage, mock_claude):
    entity      = MagicMock()
    entity.cik  = ""
    entity.name = "Ghost Corp"
    entity.sic  = "0000"
    mock_company_cls.return_value = entity

    result = onboard_company(
        mock_conn, ticker="GHST",
        voyage_client=mock_voyage, claude_client=mock_claude,
    )

    assert result["success"] is False
    assert "CIK" in result["error"]


# ── Voyage embedding failure leaves structured data committed ──────────────────

@patch("tools.onboard_company.extract_and_seed")
@patch("tools.onboard_company.index_one_filing")
@patch("tools.onboard_company.build_standalone")
@patch("tools.onboard_company.ingest_company")
@patch("tools.onboard_company._has_noncalendar_fy")
@patch("tools.onboard_company.register_vector")
@patch("tools.onboard_company.Company")
def test_voyage_failure_partial_success(
    mock_company_cls,
    mock_register_vector,
    mock_noncal,
    mock_ingest,
    mock_build,
    mock_index,
    mock_extract,
    mock_conn,
    mock_voyage,
    mock_claude,
):
    entity = MagicMock()
    entity.cik  = "9999999"
    entity.name = "Flaky Corp"
    entity.sic  = "3674"
    entity.get_filings.return_value.filter.return_value = [MagicMock()]
    mock_company_cls.return_value = entity

    mock_noncal.return_value  = False
    mock_ingest.return_value  = {"raw_rows": 100, "statement_rows": 30, "warnings": []}
    mock_build.return_value   = 45
    # Voyage embedding fails inside index_one_filing
    mock_index.side_effect    = RuntimeError("Voyage rate limit")
    mock_extract.return_value = 0

    result = onboard_company(
        mock_conn, ticker="FLKY",
        voyage_client=mock_voyage, claude_client=mock_claude,
    )

    # Structured data succeeds; RAG fails with warning
    assert result["success"]         is True
    assert result["statements_rows"] == 30
    assert result["chunks_indexed"]  == 0
    assert any("Indexing failed" in w for w in result["warnings"])

    # Stages 2-3 committed despite indexing failure
    mock_ingest.assert_called_once()
    mock_build.assert_called_once()


# ── Non-calendar fiscal year warning ──────────────────────────────────────────

@patch("tools.onboard_company.extract_and_seed")
@patch("tools.onboard_company.index_one_filing")
@patch("tools.onboard_company.build_standalone")
@patch("tools.onboard_company.ingest_company")
@patch("tools.onboard_company._has_noncalendar_fy")
@patch("tools.onboard_company.register_vector")
@patch("tools.onboard_company.Company")
def test_noncalendar_fy_warning(
    mock_company_cls,
    mock_register_vector,
    mock_noncal,
    mock_ingest,
    mock_build,
    mock_index,
    mock_extract,
    mock_conn,
    mock_voyage,
    mock_claude,
):
    entity = MagicMock()
    entity.cik  = "0001393612"
    entity.name = "Intuit Inc"
    entity.sic  = "7372"
    entity.get_filings.return_value.filter.return_value = []
    mock_company_cls.return_value = entity

    mock_noncal.return_value  = True   # non-calendar FY detected
    mock_ingest.return_value  = {"raw_rows": 150, "statement_rows": 40, "warnings": []}
    mock_build.return_value   = 55
    mock_extract.return_value = 0

    result = onboard_company(
        mock_conn, ticker="INTU",
        voyage_client=mock_voyage, claude_client=mock_claude,
    )

    assert result["success"] is True
    assert any("non-calendar" in w for w in result["warnings"])
    assert any("de-cumulation" in w for w in result["warnings"])
