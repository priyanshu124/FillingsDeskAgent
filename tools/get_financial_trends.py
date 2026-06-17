"""
tools/get_financial_trends.py — Multi-period trend analysis.

Retrieves the last N quarters for each requested metric (GAAP line items from
statements_standalone OR KPI names from kpis) and computes:
  - period-over-period (PoP) % change vs the immediately preceding quarter
  - year-over-year (YoY) % change vs the same fiscal_period ~1 year prior
  - anomaly signal: outlier | reversal | acceleration | deceleration | None

Signal thresholds (deterministic, no LLM):
  OUTLIER_SIGMA   = 2.0   std deviations from mean YoY → 'outlier'
  OUTLIER_MIN_PTS = 4     minimum YoY data points for outlier detection
  ACCEL_WINDOW    = 2     consecutive increasing YoY periods → 'acceleration'
"""
from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any

import psycopg2

logger = logging.getLogger(__name__)

OUTLIER_SIGMA   = 2.0
OUTLIER_MIN_PTS = 4
ACCEL_WINDOW    = 2

YOY_DAY_TOLERANCE = 30   # ±days around 365 for same-fiscal-quarter match

# NOTE: outlier is computed against the YoY distribution within the requested
# window, so changing `periods` can change which rows flag as 'outlier'. This is
# intentional — the signal is retrospective within the requested window, not
# absolute. A flag at periods=6 may not appear at periods=8 if more data dilutes
# the deviation. Callers should treat the signal as window-relative.

Row = dict[str, Any]


def _edgar_url(cik: str, accession: str) -> str:
    cik_int    = str(int(cik))
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"


def _pct(new: float, old: float) -> float | None:
    if old is None or new is None or old == 0:
        return None
    return round((float(new) - float(old)) / abs(float(old)) * 100, 2)


def _compute_signal(
    yoy: float | None,
    prev_yoy: float | None,
    prev_prev_yoy: float | None,
    yoys_window: list[float],
) -> str | None:
    """
    Pure function — no DB, no LLM.

    Priority: outlier > reversal > acceleration > deceleration > None.
    """
    # --- outlier ---
    if yoy is not None and len(yoys_window) >= OUTLIER_MIN_PTS:
        mean = sum(yoys_window) / len(yoys_window)
        variance = sum((v - mean) ** 2 for v in yoys_window) / len(yoys_window)
        std = math.sqrt(variance)
        if std > 0 and abs(yoy - mean) > OUTLIER_SIGMA * std:
            return "outlier"

    # --- reversal ---
    if (
        yoy is not None
        and prev_yoy is not None
        and yoy != 0
        and prev_yoy != 0
        and math.copysign(1, yoy) != math.copysign(1, prev_yoy)
    ):
        return "reversal"

    # --- acceleration / deceleration ---
    if yoy is not None and prev_yoy is not None and prev_prev_yoy is not None:
        if yoy > prev_yoy > prev_prev_yoy:
            return "acceleration"
        if yoy < prev_yoy < prev_prev_yoy:
            return "deceleration"

    return None


def _fetch_statements(
    conn: psycopg2.extensions.connection,
    ticker: str,
    metrics: list[str],
    fetch_limit: int,
) -> list[Row]:
    sql = """
        SELECT ticker, cik, line_item AS metric, period_end, fiscal_period,
               value, unit, source_form, source_accession, source_filed_date
        FROM statements_standalone
        WHERE ticker = %s
          AND line_item = ANY(%s)
          AND fiscal_period IN ('Q1','Q2','Q3','Q4')
        ORDER BY line_item, period_end DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ticker, metrics, fetch_limit))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_kpis(
    conn: psycopg2.extensions.connection,
    ticker: str,
    metrics: list[str],
    fetch_limit: int,
) -> list[Row]:
    sql = """
        SELECT ticker, cik, kpi AS metric, period_end, fiscal_period,
               value, unit, source_form, source_accession, source_filed_date
        FROM kpis
        WHERE ticker = %s
          AND kpi = ANY(%s)
          AND segment = 'total'
          AND fiscal_period IN ('Q1','Q2','Q3','Q4')
        ORDER BY kpi, period_end DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ticker, metrics, fetch_limit))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_financial_trends(
    conn: psycopg2.extensions.connection,
    ticker: str,
    metrics: list[str],
    periods: int = 8,
) -> list[Row]:
    """
    Multi-period trend for one ticker.

    Queries both statements_standalone (GAAP) and kpis (non-GAAP) for the
    requested metric names and merges results. Each metric appears in only one
    table; duplicates are dropped by (metric, period_end, fiscal_period).

    Returns the most recent `periods` rows per metric, ordered metric ASC /
    period_end DESC. YoY prior-year rows are fetched internally but not emitted.
    """
    ticker  = ticker.upper().strip()
    metrics = [m.lower().strip() for m in metrics]

    fetch_limit = len(metrics) * periods * 2  # headroom for prior-year lookups

    raw: list[Row] = []
    raw.extend(_fetch_statements(conn, ticker, metrics, fetch_limit))
    raw.extend(_fetch_kpis(conn, ticker, metrics, fetch_limit))

    # Guard: if a metric name appears in both tables, prefer statements_standalone.
    # In practice GAAP line_item names and KPI names don't overlap, but if they
    # ever do (e.g. a custom KPI named 'revenues') the GAAP row wins and we warn.
    stmt_metrics: set[str] = {r["metric"] for r in raw if r.get("source_form") in ("10-K", "10-Q")}
    kpi_metrics:  set[str] = {r["metric"] for r in raw if r.get("source_form") == "8-K"}
    overlap = stmt_metrics & kpi_metrics
    if overlap:
        for m in overlap:
            logger.warning(
                "[get_financial_trends] metric %r found in both statements_standalone "
                "and kpis for %s — using statements_standalone rows, dropping kpis rows",
                m, ticker,
            )

    # Deduplicate by (metric, period_end, fiscal_period).
    # For overlapping metrics, statements_standalone rows (non-8-K source) win.
    seen: dict[tuple, Row] = {}
    for r in raw:
        key = (r["metric"], r["period_end"], r["fiscal_period"])
        if key not in seen:
            seen[key] = r
        elif r["metric"] in overlap and r.get("source_form") in ("10-K", "10-Q"):
            seen[key] = r   # replace kpis row with statements row

    rows_all: list[Row] = sorted(
        seen.values(),
        key=lambda r: (r["metric"], r["period_end"]),
    )

    # Group by metric; build output
    metrics_map: dict[str, list[Row]] = {}
    for r in rows_all:
        metrics_map.setdefault(r["metric"], []).append(r)

    output: list[Row] = []

    for metric in sorted(metrics_map):
        m_rows = sorted(metrics_map[metric], key=lambda r: r["period_end"])
        # working window: up to periods*2 most recent rows (for YoY lookups)
        m_rows = m_rows[-(periods * 2):]

        # Collect all YoY values for outlier detection
        yoy_map: dict[int, float | None] = {}
        for i, r in enumerate(m_rows):
            pe: date = r["period_end"]
            fp: str  = r["fiscal_period"]
            # find prior-year match
            yoy_row = next(
                (
                    c for c in m_rows
                    if c["fiscal_period"] == fp
                    and 365 - YOY_DAY_TOLERANCE
                       <= (pe - c["period_end"]).days
                       <= 365 + YOY_DAY_TOLERANCE
                ),
                None,
            )
            yoy_map[i] = _pct(r["value"], yoy_row["value"]) if yoy_row else None

        yoys_window = [v for v in yoy_map.values() if v is not None]

        # Emit only the most recent `periods` rows
        emit_rows = m_rows[-periods:]
        emit_start_idx = len(m_rows) - len(emit_rows)

        for local_i, r in enumerate(emit_rows):
            global_i = emit_start_idx + local_i

            # PoP: immediately preceding row in sequence
            prev_row   = emit_rows[local_i - 1] if local_i > 0 else (
                m_rows[emit_start_idx - 1] if emit_start_idx > 0 else None
            )
            pop = _pct(r["value"], prev_row["value"]) if prev_row else None

            yoy = yoy_map[global_i]

            # Prior yoy values for signal (look back in full working window)
            prev_yoy      = yoy_map.get(global_i - 1)
            prev_prev_yoy = yoy_map.get(global_i - 2)

            signal = _compute_signal(yoy, prev_yoy, prev_prev_yoy, yoys_window)

            pe: date = r["period_end"]
            sfd = r["source_filed_date"]
            output.append({
                "ticker":            ticker,
                "metric":            r["metric"],
                "period_end":        pe.isoformat() if isinstance(pe, date) else str(pe),
                "fiscal_period":     r["fiscal_period"],
                "value":             float(r["value"]),
                "unit":              r["unit"],
                "pop_pct_change":    pop,
                "yoy_pct_change":    yoy,
                "signal":            signal,
                "source_form":       r["source_form"],
                "source_accession":  r["source_accession"],
                "source_filed_date": sfd.isoformat() if isinstance(sfd, date) else str(sfd),
                "url":               _edgar_url(str(r["cik"]), r["source_accession"]),
            })

    # Final order: metric ASC, period_end DESC (most recent first per metric)
    output.sort(key=lambda r: (r["metric"], r["period_end"]), reverse=False)
    # Within each metric, reverse so most recent comes first
    from itertools import groupby
    final: list[Row] = []
    for _, grp in groupby(output, key=lambda r: r["metric"]):
        final.extend(sorted(grp, key=lambda r: r["period_end"], reverse=True))

    return final
