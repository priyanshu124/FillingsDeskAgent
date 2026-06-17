from __future__ import annotations

import logging
import time
from typing import Any

from edgar import Company, set_identity

logger = logging.getLogger(__name__)

DEFAULT_FORMS = ["8-K", "10-Q", "10-K"]
EDGAR_SLEEP_S = 0.15

Row = dict[str, Any]


def fetch_latest_filing(
    tickers: list[str],
    forms: list[str] | None = None,
    since: str | None = None,
) -> list[Row]:
    """
    Check EDGAR live for the most recent filings for given tickers/forms.

    Returns filing metadata: ticker, form, filed_date, accession_no, url, is_new.
    is_new is True when filed_date >= since (and since is provided).
    No DB write — pure EDGAR lookup.
    """
    forms = forms or DEFAULT_FORMS
    rows: list[Row] = []

    for i, ticker in enumerate(tickers):
        for form in forms:
            try:
                filings = Company(ticker).get_filings(form=form)
                if since:
                    filings = filings.filter(date=f"{since}:9999-12-31")
                if not filings:
                    logger.debug("[%s] No %s filings found (since=%s)", ticker, form, since)
                    continue
                f = filings[0]
                filed_str = str(f.filed) if hasattr(f, "filed") else str(f.filing_date)
                rows.append({
                    "ticker":       ticker,
                    "form":         form,
                    "filed_date":   filed_str,
                    "accession_no": str(f.accession_no),
                    "url":          getattr(f, "filing_url", None),
                    "is_new":       since is not None and filed_str >= since,
                })
            except Exception as exc:
                logger.warning("[%s] %s lookup failed: %s", ticker, form, exc)

        if i < len(tickers) - 1:
            time.sleep(EDGAR_SLEEP_S)

    return rows
