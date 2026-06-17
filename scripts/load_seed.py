"""
Seed the peerdesk database with GAAP statements and non-GAAP KPIs.

Stages:
  1. ingest_company — fetch companyfacts via edgartools → raw_facts → statements
                       upserts into companies + company_data_status
  2. seed_kpis      — hand-seed non-GAAP KPIs from anchor 8-Ks (PEB only)

The core per-company function ingest_company() is designed for re-use by
onboard_company (Slice 3): connection passed in, returns a structured result dict.

edgartools v5+ API used:
  - Company(ticker).get_facts() → EntityFacts
  - EntityFacts.get_all_facts() → list[FinancialFact]
  - FinancialFact fields: concept (namespace:Tag), taxonomy, form_type, accession,
    filing_date (date), period_start (date|None), period_end (date), fiscal_period,
    numeric_value, unit
"""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import time
from datetime import date
from decimal import Decimal

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from edgar import Company, set_identity

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_TICKERS: list[dict[str, str]] = [
    {"ticker": "PEB", "cik": "1474098"},
    {"ticker": "HST", "cik": "1070750"},
    {"ticker": "SHO", "cik": "1295810"},
]

EDGAR_SLEEP_S = 0.15   # stay well under 10 req/sec

# ── XBRL concept → (statement, canonical_line_item, line_order) ───────────────
# Key format: "{namespace}:{Tag}"  (matches FinancialFact.concept exactly)
# Synonyms: multiple concepts map to the same line_item; first writer wins after dedup.
# Unmapped concepts land in raw_facts but are not promoted to statements.
CONCEPT_MAP: dict[str, tuple[str, str, int]] = {
    # Income statement
    "us-gaap:Revenues":                                                        ("income", "revenues",                 10),
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax":             ("income", "revenues",                 10),
    "us-gaap:CostsAndExpenses":                                                ("income", "total_expenses",           30),
    "us-gaap:OperatingExpenses":                                               ("income", "total_expenses",           30),
    "us-gaap:DepreciationDepletionAndAmortization":                            ("income", "depreciation_amortization",40),
    "us-gaap:DepreciationAndAmortization":                                     ("income", "depreciation_amortization",40),
    "us-gaap:CostOfGoodsAndServicesSoldDepreciationAndAmortization":           ("income", "depreciation_amortization",40),
    "us-gaap:OperatingIncomeLoss":                                             ("income", "operating_income",         50),
    "us-gaap:InterestExpense":                                                 ("income", "interest_expense",         60),
    "us-gaap:InterestExpenseDebt":                                             ("income", "interest_expense",         60),
    "us-gaap:InterestExpenseNonoperating":                                     ("income", "interest_expense",         60),
    "us-gaap:NetIncomeLoss":                                                   ("income", "net_income",               90),
    # Balance sheet
    "us-gaap:Assets":                                                          ("balance", "total_assets",            10),
    "us-gaap:Liabilities":                                                     ("balance", "total_liabilities",       20),
    "us-gaap:StockholdersEquity":                                              ("balance", "total_equity",            30),
    "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest":
                                                                               ("balance", "total_equity",            30),
    "us-gaap:CashAndCashEquivalentsAtCarryingValue":                           ("balance", "cash",                    40),
    "us-gaap:Cash":                                                            ("balance", "cash",                    40),
    "us-gaap:LongTermDebt":                                                    ("balance", "long_term_debt",          50),
    "us-gaap:LongTermDebtNoncurrent":                                          ("balance", "long_term_debt",          50),
    "us-gaap:RealEstateInvestmentPropertyNet":                                 ("balance", "real_estate_net",         60),
    # Cash flow
    "us-gaap:NetCashProvidedByUsedInOperatingActivities":                      ("cashflow", "cfo",                    10),
    "us-gaap:NetCashProvidedByUsedInInvestingActivities":                      ("cashflow", "cfi",                    20),
    "us-gaap:NetCashProvidedByUsedInFinancingActivities":                      ("cashflow", "cff",                    30),
}


# ── Period defaults ────────────────────────────────────────────────────────────

def _default_period_end() -> date:
    """Most recent complete quarter end relative to today."""
    today = date.today()
    month = today.month
    if month < 4:
        return date(today.year - 1, 12, 31)
    elif month < 7:
        return date(today.year, 3, 31)
    elif month < 10:
        return date(today.year, 6, 30)
    else:
        return date(today.year, 9, 30)


def _default_period_start(period_end: date) -> date:
    """Start of the same quarter 2 years before period_end (≈ 8 quarters back)."""
    quarter_start_month = ((period_end.month - 1) // 3) * 3 + 1
    return date(period_end.year - 2, quarter_start_month, 1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lookup_accession(ticker: str, form: str, filed_on: date) -> str:
    """Return the accession number for a specific filing by date."""
    date_str = filed_on.strftime("%Y-%m-%d")
    filings = Company(ticker).get_filings(form=form).filter(date=f"{date_str}:{date_str}")
    if not filings:
        raise RuntimeError(f"No {form} filing found for {ticker} on {date_str}")
    return filings[0].accession_no


# ── XBRL ingest (internal) ────────────────────────────────────────────────────

def _ingest_xbrl_facts(
    conn: psycopg2.extensions.connection,
    ticker: str,
    cik: str,
    period_start: date,
    period_end: date,
    unmapped: set[str],
) -> tuple[int, int]:
    """
    Fetch all XBRL facts for one company, land in raw_facts, normalize to statements.
    Returns (raw_count, stmt_count).
    """
    logger.info("[%s] Fetching companyfacts from EDGAR …", ticker)
    entity_facts = Company(ticker).get_facts()
    all_facts    = entity_facts.get_all_facts()

    # Sanity-check that FinancialFact has the fields we depend on
    if all_facts:
        required = {"concept", "taxonomy", "form_type", "accession", "filing_date",
                    "period_start", "period_end", "fiscal_period", "numeric_value", "unit"}
        missing = required - set(dir(all_facts[0]))
        if missing:
            raise RuntimeError(
                f"edgartools FinancialFact missing expected fields: {missing}. "
                f"Available: {[a for a in dir(all_facts[0]) if not a.startswith('_')]}"
            )

    # Filter to periodic GAAP filings within the seed window
    filtered = [
        f for f in all_facts
        if f.form_type in ("10-K", "10-Q")
        and f.period_end is not None
        and period_start <= f.period_end <= period_end
        and f.numeric_value is not None
    ]
    logger.info("[%s] %d facts in window after filter (from %d total)", ticker, len(filtered), len(all_facts))

    if not filtered:
        logger.warning("[%s] No facts in seed window — check ticker/CIK", ticker)
        return 0, 0

    # ── Load raw_facts ────────────────────────────────────────────────────────
    raw_rows = []
    for f in filtered:
        accession = str(f.accession).replace("-", "")
        raw_rows.append((
            ticker,
            cik,
            str(f.taxonomy),
            str(f.concept),
            str(f.unit),
            f.period_start,
            f.period_end,
            str(f.fiscal_period),
            str(f.form_type),
            accession,
            f.filing_date,
            Decimal(str(f.numeric_value)),
        ))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO raw_facts
                (ticker, cik, namespace, concept, unit, period_start, period_end,
                 fiscal_period, form, accession_no, filed_date, value)
            VALUES %s
            ON CONFLICT (cik, namespace, concept, unit, period_end, accession_no)
            DO NOTHING
            """,
            raw_rows,
        )
    conn.commit()
    raw_count = len(raw_rows)
    logger.info("[%s] raw_facts: %d rows inserted (dupes skipped)", ticker, raw_count)

    # ── Normalize to statements ───────────────────────────────────────────────
    best: dict[tuple[str, date], object] = {}
    for f in filtered:
        key       = (str(f.concept), f.period_end)
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = f
        else:
            i_form  = str(incumbent.form_type)
            f_form  = str(f.form_type)
            i_filed = incumbent.filing_date
            f_filed = f.filing_date
            if (f_form == "10-K" and i_form != "10-K") or (f_form == i_form and f_filed < i_filed):
                best[key] = f

    stmt_rows = []
    for (concept, _), f in best.items():
        if concept not in CONCEPT_MAP:
            unmapped.add(concept)
            continue
        statement, line_item, line_order = CONCEPT_MAP[concept]
        accession = str(f.accession).replace("-", "")
        stmt_rows.append((
            ticker,
            cik,
            statement,
            line_item,
            line_order,
            f.period_end,
            str(f.fiscal_period),
            Decimal(str(f.numeric_value)),
            str(f.unit),
            concept,
            accession,
            str(f.form_type),
            f.filing_date,
        ))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO statements
                (ticker, cik, statement, line_item, line_order, period_end,
                 fiscal_period, value, unit, source_concept, source_accession,
                 source_form, source_filed_date)
            VALUES %s
            ON CONFLICT (ticker, statement, line_item, period_end, fiscal_period, source_accession)
            DO NOTHING
            """,
            stmt_rows,
        )
    conn.commit()
    stmt_count = len(stmt_rows)
    logger.info("[%s] statements: %d rows inserted", ticker, stmt_count)
    return raw_count, stmt_count


# ── Public ingest function (re-used by onboard_company in Slice 3) ─────────────

def ingest_company(
    conn: psycopg2.extensions.connection,
    ticker: str,
    cik: str,
    period_start: date,
    period_end: date,
) -> dict:
    """
    Fetch and seed XBRL data for one company.
    Upserts into companies and updates company_data_status.
    Returns {"raw_rows": int, "statement_rows": int, "warnings": list[str]}
    """
    unmapped: set[str] = set()
    raw_count, stmt_count = _ingest_xbrl_facts(conn, ticker, cik, period_start, period_end, unmapped)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO companies (ticker, cik) VALUES (%s, %s) ON CONFLICT (ticker) DO NOTHING",
            (ticker, cik),
        )
        cur.execute(
            """
            INSERT INTO company_data_status (ticker, xbrl_loaded_through, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (ticker) DO UPDATE
                SET xbrl_loaded_through = EXCLUDED.xbrl_loaded_through,
                    updated_at = NOW()
            """,
            (ticker, period_end),
        )
    conn.commit()

    return {
        "raw_rows":      raw_count,
        "statement_rows": stmt_count,
        "warnings":      sorted(unmapped),
    }


# ── Stage 2: hand-seed KPIs ───────────────────────────────────────────────────

def seed_kpis(conn: psycopg2.extensions.connection) -> int:
    """
    Hand-seed non-GAAP KPIs for PEB from the two anchor 8-Ks.
    Accession numbers are looked up at runtime so provenance is real.

    Seeded from 2026-04-28 8-K (Q1 2026):
      - hotel_ebitdare   = $73.3M  (Adj EBITDAre, confirmed absolute)
      - adj_ffo_per_share = $0.32  (confirmed absolute)

    NOT seeded:
      - RevPAR +11.8% YoY: growth only, no base-period absolute in anchor doc.
      - 2026-02-25 8-K (FY2025): shows only GAAP figures and forward guidance;
        no non-GAAP absolute KPI confirmed → skipped, logged.
    """
    logger.info("[PEB] Looking up anchor 8-K accession numbers …")

    acc_q1_2026 = _lookup_accession("PEB", "8-K", date(2026, 4, 28))
    logger.info("[PEB] Q1 2026 8-K accession: %s", acc_q1_2026)

    acc_fy2025 = _lookup_accession("PEB", "8-K", date(2026, 2, 25))
    logger.info("[PEB] FY2025 8-K accession: %s", acc_fy2025)

    kpi_rows = [
        ("PEB", "1474098", "hotel_ebitdare",    "total", "financial", "hotel_reit",
         date(2026, 3, 31), "Q1", Decimal("73300000"), "USD",
         acc_q1_2026, "8-K", date(2026, 4, 28), "hand_seeded"),
        ("PEB", "1474098", "adj_ffo_per_share", "total", "financial", "hotel_reit",
         date(2026, 3, 31), "Q1", Decimal("0.32"), "USD/share",
         acc_q1_2026, "8-K", date(2026, 4, 28), "hand_seeded"),
    ]

    logger.info(
        "[PEB] FY2025 8-K (%s): anchor doc shows GAAP net loss and 2026 guidance only — "
        "no non-GAAP absolute KPI confirmed; skipping to avoid fabrication.",
        acc_fy2025,
    )

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO kpis
                (ticker, cik, kpi, segment, kpi_category, industry,
                 period_end, fiscal_period, value, unit,
                 source_accession, source_form, source_filed_date, confidence)
            VALUES %s
            ON CONFLICT (ticker, kpi, segment, period_end, source_accession)
            DO NOTHING
            """,
            kpi_rows,
        )
    conn.commit()
    logger.info("[PEB] kpis: %d rows inserted", len(kpi_rows))
    return len(kpi_rows)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the peerdesk database")
    parser.add_argument(
        "--period-start",
        default=None,
        help="Start of ingest window, YYYY-MM-DD (default: 2 years before period-end)",
    )
    parser.add_argument(
        "--period-end",
        default=None,
        help="End of ingest window, YYYY-MM-DD (default: most recent complete quarter end)",
    )
    args = parser.parse_args()

    period_end   = date.fromisoformat(args.period_end)   if args.period_end   else _default_period_end()
    period_start = date.fromisoformat(args.period_start) if args.period_start else _default_period_start(period_end)

    edgar_identity = os.environ.get("EDGAR_IDENTITY")
    if not edgar_identity:
        raise RuntimeError("EDGAR_IDENTITY env var not set. See .env.example.")
    db_url = os.environ.get("DB_URL")
    if not db_url:
        raise RuntimeError("DB_URL env var not set. See .env.example.")

    set_identity(edgar_identity)

    conn = psycopg2.connect(db_url)
    logger.info("Connected to database. Window: %s → %s", period_start, period_end)

    total_raw  = 0
    total_stmt = 0
    all_warnings: list[str] = []

    for i, company in enumerate(DEFAULT_TICKERS):
        ticker = company["ticker"]
        cik    = company["cik"]
        result = ingest_company(conn, ticker, cik, period_start, period_end)
        total_raw  += result["raw_rows"]
        total_stmt += result["statement_rows"]
        all_warnings.extend(result["warnings"])
        if i < len(DEFAULT_TICKERS) - 1:
            time.sleep(EDGAR_SLEEP_S)

    kpi_count = seed_kpis(conn)

    from scripts.build_standalone import build_standalone
    standalone_count = build_standalone(conn)
    conn.close()

    logger.info("── Seed complete ─────────────────────────────────────────────")
    logger.info("  raw_facts rows       : %d", total_raw)
    logger.info("  statements rows      : %d", total_stmt)
    logger.info("  kpis rows            : %d", kpi_count)
    logger.info("  standalone rows built: %d", standalone_count)

    if all_warnings:
        logger.warning("Unmapped XBRL concepts (extend CONCEPT_MAP to capture these):")
        for k in sorted(set(all_warnings)):
            logger.warning("  %s", k)
    else:
        logger.info("  No unmapped concepts.")


if __name__ == "__main__":
    main()
