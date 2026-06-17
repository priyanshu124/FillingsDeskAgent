"""
tests/test_get_insider_activity.py

Two categories:
  Real-DB (require DB_URL + Postgres):
    - test_multi_tranche_row_index: verifies ON CONFLICT behavior against the
      real Postgres unique constraint (accession_no, filer_cik, row_index).
    - test_query_since_filter / test_query_limit: verify the query path.

  Unit (mock Company + mock conn):
    - All other tests: is_open_market logic, footnotes, value computation, etc.
"""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from dotenv import load_dotenv

load_dotenv()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def conn():
    db_url = os.environ.get("DB_URL")
    if not db_url:
        pytest.skip("DB_URL not set")
    import psycopg2
    c = psycopg2.connect(db_url)
    yield c
    c.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

TEST_TICKER     = "_TEST_INSIDER_"
TEST_ACCESSION  = "0000000000-00-000001"
TEST_FILER_CIK  = "9999999999"
TEST_ISSUER_CIK = "8888888888"


def _make_mock_owner(cik=TEST_FILER_CIK, name="Test Filer", title="Chief Executive Officer",
                     is_director=False, is_officer=True, is_ten_pct_owner=False):
    owner = MagicMock()
    owner.cik              = cik
    owner.name             = name
    owner.officer_title    = title
    owner.is_director      = is_director
    owner.is_officer       = is_officer
    owner.is_ten_pct_owner = is_ten_pct_owner
    return owner


def _make_mock_form4(transactions_df: pd.DataFrame, owners=None, footnotes_dict=None):
    form4 = MagicMock()
    form4.issuer.cik = TEST_ISSUER_CIK
    form4.reporting_owners.owners = owners or [_make_mock_owner()]
    form4.non_derivative_table.transactions.data = transactions_df
    fd = footnotes_dict or {}
    form4.footnotes.get.side_effect = lambda fid: fd.get(fid)
    return form4


def _make_mock_filing(form4, accession=TEST_ACCESSION, filing_date=date(2025, 6, 1)):
    filing = MagicMock()
    filing.accession_no  = accession
    filing.filing_date   = filing_date
    filing.obj.return_value = form4
    return filing


def _make_company_mock(filings):
    company = MagicMock()
    company.get_filings.return_value.filter.return_value = filings
    return company


# ── Real-DB tests ─────────────────────────────────────────────────────────────

def test_multi_tranche_row_index(conn):
    """
    3 same-code same-date transactions → 3 rows with row_index 0/1/2.
    Re-running inserts 0 more (ON CONFLICT DO NOTHING).
    Uses real Postgres to exercise the unique constraint.
    """
    from tools.get_insider_activity import _ingest_form4s

    # Clean up any prior test data
    with conn.cursor() as cur:
        cur.execute("DELETE FROM insider_transactions WHERE ticker = %s", (TEST_TICKER,))
    conn.commit()

    df = pd.DataFrame([
        {"Code": "F", "Date": "2025-01-15", "Shares": "1000", "Price": "20.00", "footnotes": ""},
        {"Code": "F", "Date": "2025-01-15", "Shares": "2000", "Price": "20.50", "footnotes": ""},
        {"Code": "F", "Date": "2025-01-15", "Shares":  "500", "Price": "21.00", "footnotes": ""},
    ])
    form4   = _make_mock_form4(df)
    filing  = _make_mock_filing(form4)
    company = _make_company_mock([filing])

    with patch("tools.get_insider_activity.Company", return_value=company):
        inserted = _ingest_form4s(conn, TEST_TICKER,
                                  since_date=date(2025, 1, 1),
                                  until_date=date(2025, 12, 31))

    assert inserted == 3, f"Expected 3 inserted, got {inserted}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT row_index FROM insider_transactions WHERE ticker = %s ORDER BY row_index",
            (TEST_TICKER,),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == [0, 1, 2], f"Unexpected row_index values: {rows}"

    # Second run: ON CONFLICT DO NOTHING — should insert 0
    with patch("tools.get_insider_activity.Company", return_value=company):
        inserted2 = _ingest_form4s(conn, TEST_TICKER,
                                   since_date=date(2025, 1, 1),
                                   until_date=date(2025, 12, 31))

    assert inserted2 == 0, f"Expected 0 on re-run, got {inserted2}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM insider_transactions WHERE ticker = %s", (TEST_TICKER,)
        )
        count = cur.fetchone()[0]
    assert count == 3, "Row count should still be 3 after idempotent re-run"

    # Clean up
    with conn.cursor() as cur:
        cur.execute("DELETE FROM insider_transactions WHERE ticker = %s", (TEST_TICKER,))
    conn.commit()


def test_query_since_filter(conn):
    """Only transactions >= since date are returned."""
    from tools.get_insider_activity import get_insider_activity

    # Seed two rows directly
    with conn.cursor() as cur:
        cur.execute("DELETE FROM insider_transactions WHERE ticker = %s", (TEST_TICKER,))
        cur.executemany(
            """
            INSERT INTO insider_transactions
                (ticker, cik, filer_name, filer_cik, transaction_date, shares,
                 transaction_code, is_open_market, row_index, accession_no, filed_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            [
                (TEST_TICKER, "0", "A", "1", date(2024, 6, 1), 100, "P", True, 0, "ACC-A", date(2024, 6, 5)),
                (TEST_TICKER, "0", "B", "1", date(2025, 3, 1), 200, "S", False, 0, "ACC-B", date(2025, 3, 5)),
            ],
        )
    conn.commit()

    # Patch _loaded_through to return "today" so ingestion is skipped
    with patch("tools.get_insider_activity._loaded_through", return_value=date.today()):
        rows = get_insider_activity(conn, TEST_TICKER, since="2025-01-01", limit=50)

    dates = [r["transaction_date"] for r in rows]
    assert all(d >= "2025-01-01" for d in dates), f"since filter not applied: {dates}"
    assert "2024-06-01" not in dates

    with conn.cursor() as cur:
        cur.execute("DELETE FROM insider_transactions WHERE ticker = %s", (TEST_TICKER,))
    conn.commit()


def test_query_limit(conn):
    """limit parameter caps results."""
    from tools.get_insider_activity import get_insider_activity

    with conn.cursor() as cur:
        cur.execute("DELETE FROM insider_transactions WHERE ticker = %s", (TEST_TICKER,))
        for i in range(5):
            cur.execute(
                """
                INSERT INTO insider_transactions
                    (ticker, cik, filer_name, filer_cik, transaction_date, shares,
                     transaction_code, is_open_market, row_index, accession_no, filed_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (TEST_TICKER, "0", f"Filer{i}", "1", date(2025, i+1, 1), 100,
                 "P", True, i, f"ACC-{i:03d}", date(2025, i+1, 2)),
            )
    conn.commit()

    with patch("tools.get_insider_activity._loaded_through", return_value=date.today()):
        rows = get_insider_activity(conn, TEST_TICKER, since="2024-01-01", limit=2)

    assert len(rows) == 2

    with conn.cursor() as cur:
        cur.execute("DELETE FROM insider_transactions WHERE ticker = %s", (TEST_TICKER,))
    conn.commit()


# ── Unit tests (no DB, no EDGAR) ──────────────────────────────────────────────

def _run_ingest_mock(df: pd.DataFrame, owners=None, footnotes_dict=None,
                     accession=TEST_ACCESSION):
    """Helper: run _ingest_form4s with a fully mocked DB and return inserted rows."""
    from tools.get_insider_activity import _ingest_form4s

    form4   = _make_mock_form4(df, owners=owners, footnotes_dict=footnotes_dict)
    filing  = _make_mock_filing(form4, accession=accession)
    company = _make_company_mock([filing])

    inserted_rows: list[tuple] = []

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.rowcount = 1

    def capture_execute(sql, params=None):
        if params and "INSERT INTO insider_transactions" in (sql or ""):
            inserted_rows.append(params)

    mock_cur.execute.side_effect = capture_execute
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    with patch("tools.get_insider_activity.Company", return_value=company):
        _ingest_form4s(mock_conn, "FAKE", date(2025, 1, 1), date(2025, 12, 31))

    return inserted_rows


def test_single_tranche():
    df = pd.DataFrame([{"Code": "P", "Date": "2025-03-01", "Shares": "500", "Price": "15.00", "footnotes": ""}])
    rows = _run_ingest_mock(df)
    assert len(rows) == 1
    # row_index is index 12 in the params tuple (0-based: ticker,cik,name,filer_cik,title,date,shares,price,value,code,is_om,footnotes,row_index,...)
    # Just verify is_open_market and row count
    assert rows[0][10] is True  # is_open_market
    # row_index = 0
    assert rows[0][12] == 0    # row_index


def test_is_open_market_purchase_P():
    df = pd.DataFrame([{"Code": "P", "Date": "2025-01-01", "Shares": "100", "Price": "10.0", "footnotes": ""}])
    rows = _run_ingest_mock(df)
    assert rows[0][10] is True  # is_open_market at index 10


def test_is_open_market_sale_S():
    df = pd.DataFrame([{"Code": "S", "Date": "2025-01-01", "Shares": "100", "Price": "10.0", "footnotes": ""}])
    rows = _run_ingest_mock(df)
    assert rows[0][10] is False


def test_is_open_market_award_A():
    df = pd.DataFrame([{"Code": "A", "Date": "2025-01-01", "Shares": "500", "Price": None, "footnotes": ""}])
    rows = _run_ingest_mock(df)
    assert rows[0][10] is False


def test_is_open_market_P_is_always_true():
    """Code 'P' → is_open_market=True regardless of footnote text."""
    df = pd.DataFrame([{"Code": "P", "Date": "2025-01-01", "Shares": "100", "Price": "20.0", "footnotes": "F1"}])
    rows = _run_ingest_mock(df, footnotes_dict={"F1": "Transaction under compensation plan rule 10b5-1"})
    assert rows[0][10] is True   # code is authoritative; footnote text is ignored for is_open_market


def test_footnote_resolution():
    df = pd.DataFrame([{"Code": "A", "Date": "2025-01-01", "Shares": "100", "Price": None, "footnotes": "F1\nF2"}])
    fd = {"F1": "Grant under plan", "F2": "Vests in 3 years"}
    rows = _run_ingest_mock(df, footnotes_dict=fd)
    footnotes_value = rows[0][11]  # footnotes at index 11
    assert "Grant under plan" in (footnotes_value or "")
    assert "Vests in 3 years" in (footnotes_value or "")


def test_transaction_value_computed():
    df = pd.DataFrame([{"Code": "P", "Date": "2025-01-01", "Shares": "100", "Price": "20.0", "footnotes": ""}])
    rows = _run_ingest_mock(df)
    # tx_value = shares * price = 100 * 20 = 2000
    tx_value = rows[0][8]   # transaction_value at index 8
    assert tx_value == pytest.approx(2000.0)


def test_transaction_value_null_when_no_price():
    df = pd.DataFrame([{"Code": "A", "Date": "2025-01-01", "Shares": "500", "Price": None, "footnotes": ""}])
    rows = _run_ingest_mock(df)
    tx_value = rows[0][8]
    assert tx_value is None


def test_provenance_fields_present():
    df = pd.DataFrame([{"Code": "S", "Date": "2025-01-01", "Shares": "200", "Price": "25.0", "footnotes": ""}])
    rows = _run_ingest_mock(df)
    # accession_no at index 13, filed_date at index 14
    assert rows[0][13] == TEST_ACCESSION
    assert rows[0][14] == date(2025, 6, 1)  # from _make_mock_filing default


def test_skip_malformed_filing():
    """filing.obj() raises → warning logged, ingestion continues for remaining filings."""
    from tools.get_insider_activity import _ingest_form4s

    bad_filing = MagicMock()
    bad_filing.accession_no = "0000000000-00-000099"
    bad_filing.filing_date  = date(2025, 1, 1)
    bad_filing.obj.side_effect = RuntimeError("XML parse error")

    company = _make_company_mock([bad_filing])

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    with patch("tools.get_insider_activity.Company", return_value=company):
        result = _ingest_form4s(mock_conn, "FAKE", date(2025, 1, 1), date(2025, 12, 31))

    assert result == 0   # nothing inserted, but no exception raised


def test_multiple_owners_uses_primary():
    """Form 4 with 2 owners → primary owner used, transactions NOT duplicated."""
    df = pd.DataFrame([
        {"Code": "P", "Date": "2025-01-01", "Shares": "100", "Price": "20.0", "footnotes": ""},
        {"Code": "P", "Date": "2025-01-02", "Shares": "200", "Price": "20.5", "footnotes": ""},
    ])
    owner1 = _make_mock_owner(cik="1111", name="Primary Filer")
    owner2 = _make_mock_owner(cik="2222", name="Secondary Filer")
    rows = _run_ingest_mock(df, owners=[owner1, owner2])

    # Must be 2 rows (one per transaction), not 4 (which would mean fan-out per owner)
    assert len(rows) == 2
    # All rows attributed to primary owner
    assert all(r[3] == "1111" for r in rows)    # filer_cik at index 3
    assert all(r[2] == "Primary Filer" for r in rows)  # filer_name at index 2


def test_already_loaded_skips_ingest():
    """form4_loaded_through >= today → no Company() call made."""
    from tools.get_insider_activity import get_insider_activity

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.return_value = (date.today(),)  # loaded_through = today
    mock_cur.fetchall.return_value = []
    mock_cur.description = [("ticker",), ("cik",), ("filer_name",), ("filer_title",),
                             ("transaction_date",), ("shares",), ("price_per_share",),
                             ("transaction_value",), ("transaction_code",),
                             ("is_open_market",), ("footnotes",),
                             ("accession_no",), ("filed_date",)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    with patch("tools.get_insider_activity.Company") as mock_cls:
        get_insider_activity(mock_conn, "FAKE", since="2025-01-01")
        mock_cls.assert_not_called()
