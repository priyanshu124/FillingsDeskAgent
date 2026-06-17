-- db/migrations/001_universal.sql
-- Generalise from hotel-REIT to universal EDGAR finance agent.
-- Additive: all existing PEB/HST/SHO data untouched and still queryable.
-- Run: psql $DB_URL -f db/migrations/001_universal.sql

BEGIN;

-- ── 1. Dynamic company registry ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    ticker       TEXT        PRIMARY KEY,
    cik          TEXT        UNIQUE NOT NULL,
    name         TEXT,
    sic          TEXT,
    industry     TEXT,
    onboarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed with the three companies already in the DB
INSERT INTO companies (ticker, cik, name) VALUES
    ('PEB', '1474098', 'Pebblebrook Hotel Trust'),
    ('HST', '1070750', 'Host Hotels & Resorts'),
    ('SHO', '1295810', 'Sunstone Hotel Investors')
ON CONFLICT (ticker) DO NOTHING;

-- ── 2. Per-company data freshness tracking ────────────────────────────────────
CREATE TABLE IF NOT EXISTS company_data_status (
    ticker                  TEXT        PRIMARY KEY REFERENCES companies(ticker),
    xbrl_loaded_through     DATE,
    docs_indexed_through    DATE,
    form4_loaded_through    DATE,
    holdings_loaded_through DATE,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO company_data_status (ticker, xbrl_loaded_through, docs_indexed_through)
VALUES
    ('PEB', '2026-03-31', '2026-03-31'),
    ('HST', '2026-03-31', '2026-03-31'),
    ('SHO', '2026-03-31', '2026-03-31')
ON CONFLICT (ticker) DO NOTHING;

-- ── 3. Insider transactions (Form 4) ──────────────────────────────────────────
-- row_index = 0-based position of the transaction within the Form 4 XML table.
-- A single Form 4 commonly has multiple same-date same-code tranches at different
-- prices (e.g., PSU tax-withholding across grant lots). Keying on row_index
-- prevents ON CONFLICT DO NOTHING from silently dropping legitimate tranches.
CREATE TABLE IF NOT EXISTS insider_transactions (
    id                BIGSERIAL PRIMARY KEY,
    ticker            TEXT      NOT NULL,
    cik               TEXT      NOT NULL,
    filer_name        TEXT,
    filer_cik         TEXT,
    filer_title       TEXT,
    transaction_date  DATE,
    shares            NUMERIC,
    price_per_share   NUMERIC,
    transaction_value NUMERIC,
    transaction_code  TEXT,       -- P=purchase, S=sale, A=award, F=tax-withhold
    is_open_market    BOOLEAN,    -- TRUE only for code P with no compensatory footnote
    footnotes         TEXT,
    row_index         INT         NOT NULL DEFAULT 0,
    accession_no      TEXT,
    filed_date        DATE,
    UNIQUE (accession_no, filer_cik, row_index)
);
CREATE INDEX IF NOT EXISTS idx_insider_ticker_date
    ON insider_transactions (ticker, transaction_date DESC);

-- ── 4. Institutional holdings (13F-HR) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS institutional_holdings (
    id           BIGSERIAL PRIMARY KEY,
    ticker       TEXT      NOT NULL,
    cik          TEXT      NOT NULL,
    holder_name  TEXT,
    holder_cik   TEXT,
    report_date  DATE,
    shares       NUMERIC,
    value_usd    NUMERIC,
    accession_no TEXT,
    filed_date   DATE,
    UNIQUE (ticker, holder_cik, report_date)
);
CREATE INDEX IF NOT EXISTS idx_holdings_ticker_date
    ON institutional_holdings (ticker, report_date DESC);

-- ── 5. Generalise kpis table ──────────────────────────────────────────────────
-- Drop the two hotel-REIT-specific CHECK constraints by inspecting pg_constraint
-- rather than assuming Postgres auto-generated names (which vary by version).
-- Matches by constraint content: 'revpar' identifies the kpi CHECK;
-- 'urban' identifies the segment CHECK.
-- The confidence CHECK ('hand_seeded'|'high'|'medium'|'low') is intentionally kept.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'kpis'::regclass
          AND contype = 'c'
          AND (pg_get_constraintdef(oid) LIKE '%revpar%'
               OR pg_get_constraintdef(oid) LIKE '%urban%')
    LOOP
        EXECUTE 'ALTER TABLE kpis DROP CONSTRAINT ' || quote_ident(r.conname);
        RAISE NOTICE 'Dropped kpis constraint: %', r.conname;
    END LOOP;
END $$;

ALTER TABLE kpis ADD COLUMN IF NOT EXISTS kpi_category TEXT;
ALTER TABLE kpis ADD COLUMN IF NOT EXISTS industry     TEXT;

UPDATE kpis SET industry = 'hotel_reit' WHERE industry IS NULL;
UPDATE kpis SET kpi_category = 'operating'
    WHERE kpi IN ('revpar', 'adr', 'occupancy', 'total_revpar');
UPDATE kpis SET kpi_category = 'financial'
    WHERE kpi IN ('hotel_ebitdare', 'adj_ffo_per_share');

COMMIT;
