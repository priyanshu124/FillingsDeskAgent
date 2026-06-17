"""
tests/test_extract_kpis.py

Unit tests (no API, no DB):
  Canonical mapping, plausibility, confidence ranks, kpi-name building,
  Step A response parsing, Step B null handling.

Integration tests (real DB + Voyage + Claude API — mark with @pytest.mark.real_db):
  test_generality_no_hotel_config — ARR at $3.2B scale, no hotel config,
      empty CANONICAL_NAMES, 3 prior siblings seeded BEFORE the test period
      so the relative-scale check is actually exercised (not cold-start).
  test_revpar_through_pipeline — PEB RevPAR Q1 2026 = 215.78 via full pipeline.
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

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
    from pgvector.psycopg2 import register_vector
    c = psycopg2.connect(db_url)
    register_vector(c)
    yield c
    c.close()


@pytest.fixture(scope="module")
def claude_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    import anthropic
    return anthropic.Anthropic(api_key=key)


@pytest.fixture(scope="module")
def voyage_client():
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        pytest.skip("VOYAGE_API_KEY not set")
    import voyageai
    return voyageai.Client(api_key=key)


# ── Helper: mock conn returning specific values ───────────────────────────────

def _mock_conn_with_values(values: list[float]):
    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchall.return_value = [(v,) for v in values]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


# ── Unit tests: canonical mapping ─────────────────────────────────────────────

def test_canonical_empty_passthrough():
    """With empty CANONICAL_NAMES, any metric name passes through unchanged."""
    import scripts.kpi_canonical as kc
    original = dict(kc.CANONICAL_NAMES)
    kc.CANONICAL_NAMES.clear()
    try:
        result = kc.CANONICAL_NAMES.get("annual_recurring_revenue", "annual_recurring_revenue")
        assert result == "annual_recurring_revenue"
    finally:
        kc.CANONICAL_NAMES.update(original)


# ── Unit tests: plausibility ──────────────────────────────────────────────────

def test_plausibility_relative_outlier():
    """Priors [3.2e9, 3.1e9, 3.3e9], new 12.5 → 'low' (misread rate-as-level)."""
    from scripts.kpi_canonical import check_plausibility
    # median = 3.2e9; ratio = 12.5 / 3.2e9 ≈ 3.9e-9 << 0.1
    assert check_plausibility(
        _mock_conn_with_values([3.2e9, 3.1e9, 3.3e9]),
        "FAKE", "arr", "total", 12.5,
    ) == "low"


def test_plausibility_relative_inscale():
    """Priors [3.2e9, 3.1e9, 3.3e9], new 3.4e9 → 'high' (in scale)."""
    from scripts.kpi_canonical import check_plausibility
    # ratio = 3.4e9 / 3.2e9 ≈ 1.06 — within [0.1, 10]
    assert check_plausibility(
        _mock_conn_with_values([3.2e9, 3.1e9, 3.3e9]),
        "FAKE", "arr", "total", 3.4e9,
    ) == "high"


def test_plausibility_cold_start_one_prior():
    """< 2 priors → 'high' (cold-start, nothing to compare against)."""
    from scripts.kpi_canonical import check_plausibility
    assert check_plausibility(
        _mock_conn_with_values([3.2e9]),
        "FAKE", "arr", "total", 12.5,
    ) == "high"


def test_plausibility_cold_start_no_priors():
    """0 priors → 'high'."""
    from scripts.kpi_canonical import check_plausibility
    assert check_plausibility(
        _mock_conn_with_values([]),
        "FAKE", "arr", "total", 12.5,
    ) == "high"


def test_plausibility_negative_legit():
    """Priors [-5.0, -4.2, -6.1], new -5.5 → 'high' (negatives are fine)."""
    from scripts.kpi_canonical import check_plausibility
    # abs median = 5.0; ratio = 5.5/5.0 = 1.1 → in scale
    assert check_plausibility(
        _mock_conn_with_values([-5.0, -4.2, -6.1]),
        "FAKE", "net_margin", "total", -5.5,
    ) == "high"


def test_plausibility_zero_median():
    """All priors zero → 'high' (can't judge scale against zero)."""
    from scripts.kpi_canonical import check_plausibility
    assert check_plausibility(
        _mock_conn_with_values([0.0, 0.0, 0.0]),
        "FAKE", "some_metric", "total", 0.05,
    ) == "high"


# ── Unit tests: confidence rank ───────────────────────────────────────────────

def test_dedup_skips_equal_confidence():
    from scripts.kpi_canonical import _confidence_rank
    # same rank → existing wins; _confidence_rank(new) NOT > _confidence_rank(existing)
    assert not (_confidence_rank("high") > _confidence_rank("high"))


def test_dedup_updates_on_higher_confidence():
    from scripts.kpi_canonical import _confidence_rank
    assert _confidence_rank("high") > _confidence_rank("low")


def test_dedup_never_overwrites_hand_seeded():
    from scripts.kpi_canonical import _confidence_rank
    assert _confidence_rank("hand_seeded") > _confidence_rank("high")


# ── Unit tests: kpi name building ─────────────────────────────────────────────

def test_rate_stored_as_growth():
    from scripts.extract_kpis import _build_kpi_name
    assert _build_kpi_name("revpar", "rate") == "revpar_growth"


def test_level_keeps_canonical_name():
    from scripts.extract_kpis import _build_kpi_name
    assert _build_kpi_name("revpar", "level") == "revpar"


def test_level_arr_keeps_canonical_name():
    from scripts.extract_kpis import _build_kpi_name
    assert _build_kpi_name("annual_recurring_revenue", "level") == "annual_recurring_revenue"


# ── Unit tests: Step A response parsing ──────────────────────────────────────

def test_discovery_response_parsing():
    """Mock Claude returning a known JSON → correct DiscoveredMetric objects."""
    from scripts.extract_kpis import _discover_metrics
    import scripts.kpi_canonical as kc

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = json.dumps({"metrics": [
        {
            "reported_name": "Annual Recurring Revenue",
            "canonical": "annual_recurring_revenue",
            "unit": "USD",
            "value_type": "level",
            "period": "Q4 2026",
            "segment": "total",
        },
        {
            "reported_name": "ARR Growth",
            "canonical": "annual_recurring_revenue_growth",
            "unit": "%",
            "value_type": "rate",
            "period": "Q4 2026",
            "segment": "total",
        },
    ]})

    mock_claude = MagicMock()
    mock_claude.messages.create.return_value = mock_response

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchall.return_value = [("Some text about ARR metrics.",), ("More text here.",)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    original = dict(kc.CANONICAL_NAMES)
    kc.CANONICAL_NAMES.clear()
    try:
        metrics = _discover_metrics(
            mock_conn, mock_claude, "ACC123/8-K", "FAKE", "8-K", date(2027, 1, 30),
        )
    finally:
        kc.CANONICAL_NAMES.update(original)

    assert len(metrics) == 2
    level_m = next(m for m in metrics if m.value_type == "level")
    rate_m  = next(m for m in metrics if m.value_type == "rate")

    assert level_m.canonical     == "annual_recurring_revenue"
    assert level_m.reported_name == "Annual Recurring Revenue"
    assert level_m.period_end    == date(2026, 12, 31)
    assert level_m.fiscal_period == "Q4"

    assert rate_m.canonical  == "annual_recurring_revenue_growth"
    assert rate_m.value_type == "rate"


# ── Unit tests: Step B null handling ─────────────────────────────────────────

def test_step_b_null_skips_insert():
    """Step B Claude returns null value → _retrieve_and_extract returns (None, ..., ...)."""
    from scripts.extract_kpis import _retrieve_and_extract, DiscoveredMetric

    mock_response = MagicMock()
    mock_response.content = [MagicMock()]
    mock_response.content[0].text = '{"value": null, "unit": "USD", "source_note": "not found"}'

    mock_claude = MagicMock()
    mock_claude.messages.create.return_value = mock_response

    mock_voyage = MagicMock()
    mock_voyage.embed.return_value.embeddings = [[0.1] * 1024]

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchall.return_value = [("Some chunk text with no matching level value.",)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    metric = DiscoveredMetric(
        canonical="revpar", reported_name="RevPAR",
        unit="USD", value_type="level", period="Q1 2026", segment="total",
        period_end=date(2026, 3, 31), fiscal_period="Q1",
    )

    value, _unit, _note = _retrieve_and_extract(
        mock_conn, mock_claude, mock_voyage, "ACCXXX/8-K", metric,
    )
    assert value is None


# ── Integration tests ─────────────────────────────────────────────────────────

@pytest.mark.real_db
def test_generality_no_hotel_config(conn, voyage_client, claude_client):
    """
    Proves the pipeline discovers and extracts a metric it has never been
    configured for (Annual Recurring Revenue at $3.2B scale).

    CANONICAL_NAMES is cleared — no hotel config, no ARR config, no config at all.

    3 prior ARR periods are seeded in kpis BEFORE running the pipeline so that
    the relative-scale check has siblings and is NOT in cold-start mode. If the
    misread period (12.5%) were inserted first, cold-start would accept it at
    'high' by design, and the test would pass for the wrong reason.

    The period under test has level $3.4B adjacent to +12.5% YoY growth.
    Assert: level is extracted (~$3.4B, not 12.5), confidence='high' (in scale).
    """
    from scripts.extract_kpis import extract_and_seed
    import scripts.kpi_canonical as kc

    FAKE_TICKER = "_GENERALITY_TEST_"
    FAKE_CIK    = "7777777777"
    FAKE_ACC    = "0000000000-27-000001"
    FAKE_DOC_ID = FAKE_ACC.replace("-", "") + "/8-K"
    FILED_DATE  = date(2027, 1, 30)
    PERIOD_END  = date(2026, 12, 31)

    # Full cleanup before test
    with conn.cursor() as cur:
        for tbl in ("kpis", "doc_chunks", "documents"):
            cur.execute(f"DELETE FROM {tbl} WHERE ticker = %s", (FAKE_TICKER,))
        cur.execute("DELETE FROM companies WHERE ticker = %s", (FAKE_TICKER,))
    conn.commit()

    # Register company (needed for industry_map lookup)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO companies (ticker, cik, name, industry)
               VALUES (%s,%s,%s,%s) ON CONFLICT (ticker) DO NOTHING""",
            (FAKE_TICKER, FAKE_CIK, "Generality Test Corp", "tech"),
        )
    conn.commit()

    # ── Seed 3 prior ARR periods BEFORE running extraction ────────────────────
    # This is the critical ordering: siblings must exist so the plausibility
    # check has a baseline and is NOT in cold-start (< 2 priors) mode.
    prior_periods = [
        (date(2025, 9, 30), "Q3", 3_200_000_000.0),
        (date(2025, 6, 30), "Q2", 3_100_000_000.0),
        (date(2025, 3, 31), "Q1", 3_000_000_000.0),
    ]
    with conn.cursor() as cur:
        for pe, fp, v in prior_periods:
            cur.execute(
                """INSERT INTO kpis
                   (ticker, cik, kpi, segment, period_end, fiscal_period,
                    value, unit, source_accession, source_form, source_filed_date, confidence)
                   VALUES (%s,%s,'annual_recurring_revenue','total',%s,%s,%s,
                           'USD','ACC-PRIOR-ARR','8-K',%s,'hand_seeded')
                   ON CONFLICT (ticker, kpi, segment, period_end, source_accession) DO NOTHING""",
                (FAKE_TICKER, FAKE_CIK, pe, fp, Decimal(str(v)), pe),
            )
    conn.commit()

    # ── Create filing document + chunk with level AND adjacent growth rate ─────
    # Claude must extract the level ($3.4B), not the growth rate (12.5%).
    arr_text = (
        "GENERALITY TEST CORP — FOURTH QUARTER 2026 RESULTS\n\n"
        "Business Highlights:\n"
        "Annual Recurring Revenue (ARR) was $3.4 billion as of December 31, 2026, "
        "an increase of 12.5% year-over-year compared to $3.0 billion in Q4 2025.\n\n"
        "Q4 2026 Key Metrics:\n"
        "  Annual Recurring Revenue: $3,400,000,000\n"
        "  ARR Year-over-Year Growth: 12.5%\n"
        "  Total ARR (Q4 2026): $3.4B\n"
    )

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (doc_id, ticker, cik, form, accession_no, filed_date, title, url)
               VALUES (%s,%s,%s,'8-K',%s,%s,'Generality Test 8-K','https://example.com/')
               ON CONFLICT (doc_id) DO NOTHING""",
            (FAKE_DOC_ID, FAKE_TICKER, FAKE_CIK, FAKE_ACC, FILED_DATE),
        )
    conn.commit()

    embedding = voyage_client.embed([arr_text], model="voyage-finance-2").embeddings[0]
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO doc_chunks
               (chunk_id, doc_id, chunk_index, text, embedding,
                ticker, form, filed_date, fiscal_period)
               VALUES (%s,%s,0,%s,%s::vector,%s,'8-K',%s,'Q4')
               ON CONFLICT (doc_id, chunk_index) DO NOTHING""",
            (f"{FAKE_DOC_ID}/0", FAKE_DOC_ID, arr_text, embedding, FAKE_TICKER, FILED_DATE),
        )
    conn.commit()

    # ── Run extraction with empty CANONICAL_NAMES ─────────────────────────────
    original_names = dict(kc.CANONICAL_NAMES)
    kc.CANONICAL_NAMES.clear()
    try:
        count = extract_and_seed(conn, claude_client, voyage_client, ticker=FAKE_TICKER)
    finally:
        kc.CANONICAL_NAMES.update(original_names)

    assert count >= 1, f"Expected >= 1 KPI inserted/updated, got {count}"

    # Find the ARR level row (not the growth row)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT kpi, value, unit, confidence
               FROM kpis
               WHERE ticker=%s AND period_end=%s
                 AND kpi NOT LIKE '%%growth%%'
                 AND kpi LIKE '%%recurring%%'
               ORDER BY kpi""",
            (FAKE_TICKER, PERIOD_END),
        )
        rows = cur.fetchall()

    assert rows, (
        f"No ARR level row found in kpis for {FAKE_TICKER} period_end={PERIOD_END}. "
        f"Check what was actually inserted (kpi names may differ)."
    )

    kpi_name, arr_value, arr_unit, arr_conf = rows[0]
    arr_value = float(arr_value)

    # Must be the level (~$3.4B), NOT the growth rate (12.5)
    assert arr_value > 1_000_000, (
        f"ARR extracted value is {arr_value}, which looks like a rate (12.5%) "
        f"not a level ($3.4B). Level-vs-rate disambiguation failed."
    )
    assert 2_500_000_000 <= arr_value <= 4_500_000_000, (
        f"ARR value {arr_value} outside expected $2.5B–$4.5B range"
    )
    # In scale with siblings [3.0B, 3.1B, 3.2B] → ratio ≈ 1.06 → 'high'
    assert arr_conf == "high", (
        f"Expected high confidence for in-scale ARR ({arr_value:,.0f}), got {arr_conf}. "
        f"Prior siblings: {[v for _, _, v in prior_periods]}"
    )

    # Cleanup
    with conn.cursor() as cur:
        for tbl in ("kpis", "doc_chunks", "documents"):
            cur.execute(f"DELETE FROM {tbl} WHERE ticker = %s", (FAKE_TICKER,))
        cur.execute("DELETE FROM companies WHERE ticker = %s", (FAKE_TICKER,))
    conn.commit()


@pytest.mark.real_db
def test_revpar_through_pipeline(conn, voyage_client, claude_client):
    """
    Delete PEB RevPAR Q1 2026 from kpis, run the full two-step pipeline on the
    seeded 8-K, assert revpar = 215.78, confidence = 'high'.

    RevPAR is treated identically to any other metric — there is no RevPAR-specific
    code anywhere in the pipeline. This is a real filing: chunk 78 of the PEB
    Q1 2026 8-K contains $215.78 and was missed by the old first-8-chunks approach.
    """
    from scripts.extract_kpis import extract_and_seed

    TICKER     = "PEB"
    KPI        = "revpar"
    SEGMENT    = "total"
    PERIOD_END = date(2026, 3, 31)

    # Record original row to restore after test
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, value, unit, confidence, source_accession, source_form, source_filed_date
               FROM kpis
               WHERE ticker=%s AND kpi=%s AND segment=%s AND period_end=%s""",
            (TICKER, KPI, SEGMENT, PERIOD_END),
        )
        original_row = cur.fetchone()

    if original_row:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kpis WHERE id = %s", (original_row[0],))
        conn.commit()

    try:
        count = extract_and_seed(conn, claude_client, voyage_client, ticker=TICKER)
        assert count >= 1, f"Expected >= 1 KPI inserted/updated, got {count}"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT value, confidence FROM kpis "
                "WHERE ticker=%s AND kpi=%s AND segment=%s AND period_end=%s",
                (TICKER, KPI, SEGMENT, PERIOD_END),
            )
            row = cur.fetchone()

        assert row is not None, (
            f"revpar Q1 2026 not found in kpis after extraction. "
            f"Check that the PEB Q1 2026 8-K is indexed in doc_chunks."
        )
        extracted_value = float(row[0])
        assert extracted_value == pytest.approx(215.78, abs=0.01), (
            f"Expected revpar=215.78, got {extracted_value}"
        )
        assert row[1] == "high", f"Expected confidence='high', got {row[1]}"

    finally:
        # Restore: remove any newly-extracted version, re-insert original if it existed
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM kpis WHERE ticker=%s AND kpi=%s AND segment=%s AND period_end=%s",
                (TICKER, KPI, SEGMENT, PERIOD_END),
            )
        conn.commit()

        if original_row:
            _, orig_val, orig_unit, orig_conf, orig_acc, orig_form, orig_filed = original_row
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cik FROM companies WHERE ticker = %s", (TICKER,)
                )
                cik_row = cur.fetchone()
                cik = cik_row[0] if cik_row else ""
                cur.execute(
                    """INSERT INTO kpis
                       (ticker, cik, kpi, segment, period_end, fiscal_period,
                        value, unit, source_accession, source_form, source_filed_date, confidence)
                       VALUES (%s,%s,%s,%s,%s,'Q1',%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (ticker, kpi, segment, period_end, source_accession) DO NOTHING""",
                    (TICKER, cik, KPI, SEGMENT, PERIOD_END,
                     orig_val, orig_unit, orig_acc, orig_form, orig_filed, orig_conf),
                )
            conn.commit()
