"""
tools/get_insider_activity.py — Insider transaction fetch + query (Form 4).

Lazy-loads Form 4 filings from EDGAR when form4_loaded_through is absent or
stale, then queries insider_transactions. Returns rows with full provenance.

Ingestion design:
  - One set of non-derivative transactions per Form 4 filing (not per-owner).
  - Primary reporting owner (owners[0]) is used for filer attribution.
  - Multiple transactions in one filing get row_index 0,1,2… via enumerate()
    so the unique constraint (accession_no, filer_cik, row_index) prevents
    duplicate inserts without dropping legitimate tranches.
  - is_open_market: transaction_code 'P' ("Open Market Purchase") is
    authoritative. A/S/F/M/X are definitionally not open-market purchases.
  - Never raises — per-filing parse failures skip with a warning.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

import psycopg2
from edgar import Company, set_identity

logger = logging.getLogger(__name__)

Row = dict[str, Any]

_DEFAULT_LOOKBACK_DAYS = 365
_INGEST_SLEEP_S        = 0.1


def _edgar_url(cik: str, accession: str) -> str:
    try:
        cik_int = str(int(cik))
    except (ValueError, TypeError):
        cik_int = cik or "0"
    acc_nodash = (accession or "").replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"


def _loaded_through(conn, ticker: str) -> date | None:
    """Return form4_loaded_through for ticker from company_data_status, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT form4_loaded_through FROM company_data_status WHERE ticker = %s",
            (ticker,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _update_loaded_through(conn, ticker: str, through: date) -> None:
    """Upsert form4_loaded_through. Best-effort — does not abort on FK miss."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO company_data_status (ticker, form4_loaded_through, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (ticker) DO UPDATE
                    SET form4_loaded_through = GREATEST(
                            company_data_status.form4_loaded_through,
                            EXCLUDED.form4_loaded_through
                        ),
                        updated_at = NOW()
                """,
                (ticker, through),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("[%s] Could not update form4_loaded_through: %s", ticker, exc)
        try:
            conn.rollback()
        except Exception:
            pass


def _ingest_form4s(
    conn,
    ticker: str,
    since_date: date,
    until_date: date,
) -> int:
    """
    Fetch Form 4 filings from EDGAR in [since_date, until_date], parse, upsert.

    Returns total rows inserted. Per-filing errors are logged and skipped.
    Exposed (not private) so tests can call it directly with mocked Company.
    """
    # safe_numeric may not be available in all edgartools versions — try import
    try:
        from edgar.ownership.core import safe_numeric as _safe_numeric
    except ImportError:
        def _safe_numeric(v):  # type: ignore[misc]
            try:
                return float(str(v).split("[")[0]) if v is not None and str(v).strip() else None
            except (ValueError, TypeError):
                return None

    identity = os.environ.get("EDGAR_IDENTITY", "")
    if identity:
        set_identity(identity)

    since_str = since_date.isoformat()
    until_str = until_date.isoformat()

    try:
        raw_filings = Company(ticker).get_filings(form="4").filter(
            date=f"{since_str}:{until_str}"
        )
        filings = list(raw_filings)
    except Exception as exc:
        logger.warning("[%s] Failed to fetch Form 4 list: %s", ticker, exc)
        return 0

    logger.info(
        "[%s] %d Form 4 filing(s) in %s – %s", ticker, len(filings), since_str, until_str
    )
    total_inserted = 0

    for filing in filings:
        accession_no = str(getattr(filing, "accession_no", "") or "")

        # Filed date
        try:
            raw_fd = getattr(filing, "filing_date", None) or getattr(filing, "filed", None)
            filed_date: date = (
                date.fromisoformat(raw_fd) if isinstance(raw_fd, str) else (raw_fd or date.today())
            )
        except Exception:
            filed_date = date.today()

        # Parse Form4 object
        try:
            form4 = filing.obj()
        except Exception as exc:
            logger.warning("[%s] filing.obj() failed for %s: %s", ticker, accession_no, exc)
            continue

        # Issuer CIK
        try:
            issuer_cik = str(form4.issuer.cik or "").lstrip("0")
        except Exception:
            issuer_cik = ""

        # Reporting owners — use primary only (no fan-out)
        try:
            owners = list(getattr(form4.reporting_owners, "owners", None) or [])
        except Exception:
            owners = []

        if not owners:
            logger.warning("[%s] Form 4 %s: no reporting owners — skipping", ticker, accession_no)
            continue

        if len(owners) > 1:
            logger.warning(
                "[%s] Form 4 %s has %d reporting owners — attributing to primary (%s)",
                ticker, accession_no, len(owners), getattr(owners[0], "name", "?"),
            )

        owner       = owners[0]
        filer_cik   = str(getattr(owner, "cik",  None) or "")
        filer_name  = str(getattr(owner, "name", "") or "")
        filer_title = (
            getattr(owner, "officer_title", None)
            or ("Director"  if getattr(owner, "is_director",      False) else
                "10% Owner" if getattr(owner, "is_ten_pct_owner", False) else
                "Officer"   if getattr(owner, "is_officer",       False) else None)
        )

        # Non-derivative transactions DataFrame
        try:
            df = form4.non_derivative_table.transactions.data
        except Exception:
            df = None

        try:
            import pandas as pd
            empty_df = pd.DataFrame()
        except ImportError:
            empty_df = None  # type: ignore[assignment]

        if df is None or (hasattr(df, "empty") and df.empty):
            logger.info("[%s] Form 4 %s: no non-derivative transactions", ticker, accession_no)
            continue

        inserted = 0
        for row_index, (_, row) in enumerate(df.iterrows()):
            code   = str(row.get("Code") or "").strip()
            shares = _safe_numeric(row.get("Shares"))
            price  = _safe_numeric(row.get("Price"))
            txdate = row.get("Date")

            # Resolve footnote IDs → text
            fid_str = str(row.get("footnotes") or "")
            footnote_texts: list[str] = []
            for fid in fid_str.split("\n"):
                fid = fid.strip()
                if fid:
                    try:
                        t = form4.footnotes.get(fid)
                        if t:
                            footnote_texts.append(t)
                    except Exception:
                        pass
            footnotes_str = " | ".join(footnote_texts) or None

            # 'P' = "Open Market Purchase" — code is authoritative, no footnote override
            is_open_market = (code == "P")

            tx_value = (
                float(shares) * float(price)
                if shares is not None and price is not None else None
            )

            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO insider_transactions (
                            ticker, cik, filer_name, filer_cik, filer_title,
                            transaction_date, shares, price_per_share, transaction_value,
                            transaction_code, is_open_market, footnotes,
                            row_index, accession_no, filed_date
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (accession_no, filer_cik, row_index) DO NOTHING
                        """,
                        (
                            ticker, issuer_cik,
                            filer_name, filer_cik, filer_title,
                            txdate,
                            float(shares) if shares is not None else None,
                            float(price)  if price  is not None else None,
                            tx_value,
                            code or None, is_open_market, footnotes_str,
                            row_index, accession_no, filed_date,
                        ),
                    )
                    inserted += cur.rowcount
            except Exception as exc:
                logger.warning(
                    "[%s] Insert failed for %s row %d: %s", ticker, accession_no, row_index, exc
                )
                try:
                    conn.rollback()
                except Exception:
                    pass
                continue

        conn.commit()
        total_inserted += inserted
        logger.info("[%s] Form 4 %s: %d row(s) inserted", ticker, accession_no, inserted)

    return total_inserted


def get_insider_activity(
    conn,
    ticker: str,
    since: str | None = None,
    limit: int = 50,
) -> list[Row]:
    """
    Fetch and query insider transactions (Form 4) for a ticker.

    Lazy-loads EDGAR Form 4 data when form4_loaded_through is absent or before
    the requested since date. Returns rows ordered by transaction_date DESC.

    Returns [] and logs on any top-level failure — never raises.
    """
    ticker = ticker.upper().strip()

    try:
        since_date: date = (
            date.fromisoformat(since)
            if since
            else date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        )
        until_date = date.today()

        # Lazy-load: ingest if form4_loaded_through is absent or before since_date
        loaded = _loaded_through(conn, ticker)
        if loaded is None or loaded < since_date:
            _ingest_form4s(conn, ticker, since_date, until_date)
            _update_loaded_through(conn, ticker, until_date)

        # Query
        since_param = since_date.isoformat()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, cik, filer_name, filer_title, transaction_date,
                       shares, price_per_share, transaction_value,
                       transaction_code, is_open_market, footnotes,
                       accession_no, filed_date
                FROM insider_transactions
                WHERE ticker = %s
                  AND (%s IS NULL OR transaction_date >= %s::date)
                ORDER BY transaction_date DESC
                LIMIT %s
                """,
                (ticker, since_param, since_param, limit),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        for r in rows:
            for date_field in ("transaction_date", "filed_date"):
                if r.get(date_field) is not None:
                    r[date_field] = str(r[date_field])
            for num_field in ("shares", "price_per_share", "transaction_value"):
                if r.get(num_field) is not None:
                    r[num_field] = float(r[num_field])
            r["url"] = _edgar_url(r.get("cik") or "", r.get("accession_no") or "")

        return rows

    except Exception as exc:
        logger.error("[%s] get_insider_activity failed: %s", ticker, exc)
        return []
