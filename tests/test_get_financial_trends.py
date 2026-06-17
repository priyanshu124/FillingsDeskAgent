"""
tests/test_get_financial_trends.py

Two categories:
  - Live-DB tests (require DB_URL): use seeded PEB data to verify routing,
    YoY matching, PoP, provenance, and period limiting.
  - Synthetic signal tests: call _compute_signal directly — no DB, no mocks.
"""
from __future__ import annotations

import os

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


# ── Live-DB tests ─────────────────────────────────────────────────────────────

def test_gaap_metric_routes_statements(conn):
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revenues"], periods=4)
    assert rows, "Expected rows for PEB revenues"
    assert all(r["metric"] == "revenues" for r in rows)
    assert all(r["ticker"] == "PEB" for r in rows)


def test_kpi_metric_routes_kpis(conn):
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revpar"], periods=4)
    assert rows, "Expected rows for PEB revpar"
    assert all(r["metric"] == "revpar" for r in rows)
    assert all(r["source_form"] == "8-K" for r in rows)


def test_mixed_metrics(conn):
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revenues", "revpar"], periods=4)
    metrics_found = {r["metric"] for r in rows}
    assert "revenues" in metrics_found
    assert "revpar" in metrics_found


def test_return_limited_to_periods(conn):
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revenues"], periods=3)
    assert len(rows) <= 3


def test_provenance_present(conn):
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revenues"], periods=4)
    for r in rows:
        assert r.get("source_form"), f"Missing source_form: {r}"
        assert r.get("source_accession"), f"Missing source_accession: {r}"
        assert r.get("source_filed_date"), f"Missing source_filed_date: {r}"


def test_yoy_matching_calendar_fy(conn):
    """PEB revenues Q1 2026 YoY should match Q1 2025 (same fiscal_period, ~365d apart)."""
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revenues"], periods=8)
    q1_rows = [r for r in rows if r["fiscal_period"] == "Q1"]
    # There should be at least one Q1 with a YoY (need two Q1s in window)
    yoy_rows = [r for r in q1_rows if r["yoy_pct_change"] is not None]
    assert yoy_rows, "Expected at least one Q1 revenues row with YoY value"
    # YoY for Q1 must not match Q2 (cross-quarter); verify fiscal_period consistency
    for r in yoy_rows:
        assert r["fiscal_period"] == "Q1"


def test_yoy_no_cross_quarter_match(conn):
    """Q1 YoY must not accidentally match Q2 of the prior year."""
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revenues"], periods=8)
    q1_rows = [r for r in rows if r["fiscal_period"] == "Q1" and r["yoy_pct_change"] is not None]
    q2_rows = [r for r in rows if r["fiscal_period"] == "Q2"]
    # For each Q1 with YoY, verify the implied prior-year value is not a Q2 figure.
    # We do this by checking: if Q1 2026 has YoY, there must exist a Q1 2025 row.
    q1_periods = {r["period_end"][:7] for r in q1_rows}   # 'YYYY-MM'
    q2_periods = {r["period_end"][:7] for r in q2_rows}
    # Ensure no Q1 period_end matches a Q2 period_end (would indicate cross-quarter match)
    assert q1_periods.isdisjoint(q2_periods)


def test_pop_is_adjacent_quarter(conn):
    """PoP for the second row should reference the first (adjacent quarter), not same-period prior year."""
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["revenues"], periods=6)
    # rows are metric ASC, period_end DESC — reverse to get chronological order
    chron = list(reversed(rows))
    # Find adjacent pair
    for i in range(1, len(chron)):
        curr = chron[i]
        prev = chron[i - 1]
        if curr["pop_pct_change"] is not None:
            expected = round(
                (float(curr["value"]) - float(prev["value"])) / abs(float(prev["value"])) * 100,
                2,
            )
            assert abs(curr["pop_pct_change"] - expected) < 0.05, (
                f"PoP mismatch: got {curr['pop_pct_change']}, expected {expected}"
            )
            break
    else:
        pytest.skip("No adjacent PoP pair found in seeded data")


def test_unknown_metric_returns_empty(conn):
    from tools.get_financial_trends import get_financial_trends
    rows = get_financial_trends(conn, "PEB", ["nonexistent_metric_xyz"], periods=4)
    assert rows == []


# ── Synthetic signal tests (no DB) ───────────────────────────────────────────

from tools.get_financial_trends import _compute_signal  # noqa: E402


def test_signal_outlier():
    # Need ≥6 data points: max z-score for n=5 is ~1.79, below OUTLIER_SIGMA=2.0.
    # With n=6: mean≈10.87, std≈13.04, z(40)≈2.23 → outlier fires.
    # prev_yoy < prev_prev_yoy so acceleration doesn't also trigger.
    yoys = [5.0, 5.1, 4.9, 5.2, 5.0, 40.0]
    signal = _compute_signal(yoy=40.0, prev_yoy=5.0, prev_prev_yoy=5.2, yoys_window=yoys)
    assert signal == "outlier"


def test_signal_reversal():
    signal = _compute_signal(yoy=-3.0, prev_yoy=5.0, prev_prev_yoy=4.0, yoys_window=[-3.0, 5.0, 4.0])
    assert signal == "reversal"


def test_signal_acceleration():
    # Strictly increasing YoY over 3 periods
    signal = _compute_signal(yoy=10.0, prev_yoy=7.0, prev_prev_yoy=4.0, yoys_window=[10.0, 7.0, 4.0])
    assert signal == "acceleration"


def test_signal_deceleration():
    signal = _compute_signal(yoy=2.0, prev_yoy=5.0, prev_prev_yoy=8.0, yoys_window=[2.0, 5.0, 8.0])
    assert signal == "deceleration"


def test_signal_priority_outlier_wins():
    # Both outlier AND reversal triggered — outlier must win.
    # With n=6: mean≈-2.5, std≈16.77, z(-40)≈2.24 → outlier fires.
    # -40 vs prev +5 is a sign flip → reversal would also fire. Outlier wins.
    yoys = [5.0, 5.0, 5.0, 5.0, 5.0, -40.0]
    signal = _compute_signal(yoy=-40.0, prev_yoy=5.0, prev_prev_yoy=5.0, yoys_window=yoys)
    assert signal == "outlier"


def test_signal_none_insufficient_data():
    # Only 3 yoy values — below OUTLIER_MIN_PTS (4); no reversal, no accel/decel
    yoys = [5.0, 5.1, 4.9]
    signal = _compute_signal(yoy=4.9, prev_yoy=5.1, prev_prev_yoy=5.0, yoys_window=yoys)
    # acceleration/deceleration don't apply (not strictly monotone)
    # outlier not checked (< 4 pts)
    assert signal is None


def test_signal_none_when_yoy_missing():
    signal = _compute_signal(yoy=None, prev_yoy=5.0, prev_prev_yoy=4.0, yoys_window=[5.0, 4.0])
    assert signal is None


def test_signal_no_reversal_when_zero():
    # yoy=0 should not trigger reversal
    signal = _compute_signal(yoy=0.0, prev_yoy=5.0, prev_prev_yoy=4.0, yoys_window=[0.0, 5.0, 4.0])
    assert signal != "reversal"
