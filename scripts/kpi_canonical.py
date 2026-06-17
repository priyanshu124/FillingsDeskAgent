"""
scripts/kpi_canonical.py

Human-owned canonical name mapping and metric-agnostic plausibility checking.

CANONICAL_NAMES:
  Maps reported-name variants to one canonical identifier so the same metric
  does not fragment across quarters. STARTS EMPTY. Add entries only when you
  observe real fragmentation — not speculatively.

  The LLM never writes to this dict. Only humans edit it.
  The pipeline produces correct results even with this dict completely empty.

check_plausibility:
  Flags a value 'low' confidence if it is wildly out of scale with the same
  metric's prior periods for this company. Industry-agnostic — no knowledge of
  what the metric is or what industry it belongs to. No hardcoded bounds.
"""
from __future__ import annotations

import statistics

import psycopg2

# Maps reported-name variants → canonical identifier.
# STARTS EMPTY — add entries only as real naming fragmentation is observed.
# The pipeline works correctly with this dict completely empty.
CANONICAL_NAMES: dict[str, str] = {
    # Added after observing 'same_property_revpar' appear across PEB filings
    # (Q2 2025, Q1 2026 8-Ks) while earlier filings used 'revpar'.
    "same_property_revpar": "revpar",
}


def _confidence_rank(confidence: str) -> int:
    """Higher integer = higher confidence. 'hand_seeded' rows are never overwritten."""
    return {"hand_seeded": 3, "high": 2, "medium": 1, "low": 0}.get(confidence, 0)


def _existing_values(
    conn: psycopg2.extensions.connection,
    ticker: str,
    canonical: str,
    segment: str,
) -> list[float]:
    """Return all stored values for (ticker, canonical, segment) from the kpis table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM kpis WHERE ticker = %s AND kpi = %s AND segment = %s",
            (ticker, canonical, segment),
        )
        return [float(r[0]) for r in cur.fetchall() if r[0] is not None]


def check_plausibility(
    conn: psycopg2.extensions.connection,
    ticker: str,
    canonical: str,
    segment: str,
    value: float,
) -> str:
    """
    Return 'low' if value is wildly out of scale with this metric's own prior periods.
    Return 'high' otherwise.

    Industry-agnostic: no hardcoded bounds, no knowledge of what the metric is.

    Cold-start: with < 2 prior periods there is nothing to compare against, so
    the value is accepted at 'high'. The guard strengthens as history accumulates.

    No sign assumptions: negative values are legitimate (FCF, margins, net income).
    Uses abs() — detects magnitude mismatches, not sign errors.
    """
    rows = _existing_values(conn, ticker, canonical, segment)
    if len(rows) < 2:
        return "high"  # cold-start — nothing to compare against

    abs_rows = [abs(v) for v in rows if v is not None]
    med = statistics.median(abs_rows)
    if med == 0:
        return "high"  # metric is zero-valued historically; can't judge scale

    ratio = abs(value) / med
    if ratio < 0.1 or ratio > 10:
        return "low"  # > 1 order of magnitude from siblings → likely misread

    return "high"
