from __future__ import annotations

from typing import Any, Literal

import psycopg2

from tools.get_kpi import get_kpi
from tools.query_financials import query_financials

Row = dict[str, Any]


def compare_peers(
    conn: psycopg2.extensions.connection,
    tickers: list[str],
    focus_ticker: str,
    metric: str,
    metric_kind: Literal["statement", "kpi"],
    period_end: str,
    segment: str = "total",
) -> list[Row]:
    """
    Compare one metric across a caller-supplied ticker set for a given period_end.

    Delegates to query_financials (GAAP) or get_kpi (non-GAAP KPIs).
    Adds is_focus flag and sorts by value descending.
    Provenance fields pass through from the underlying tool.
    """
    if metric_kind == "statement":
        rows = query_financials(
            conn,
            tickers=tickers,
            line_items=[metric],
            period_start=period_end,
            period_end=period_end,
        )
    elif metric_kind == "kpi":
        rows = get_kpi(
            conn,
            tickers=tickers,
            kpis=[metric],
            segment=segment,
            period_start=period_end,
            period_end=period_end,
        )
    else:
        raise ValueError(f"metric_kind must be 'statement' or 'kpi', got {metric_kind!r}")

    for row in rows:
        row["is_focus"] = row["ticker"] == focus_ticker

    rows.sort(key=lambda r: (r.get("value") is None, -(r.get("value") or 0)))
    return rows
