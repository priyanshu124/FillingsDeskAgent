CREATE EXTENSION IF NOT EXISTS vector;

-- STEP-4 DECISION: change the value below to match the Voyage model chosen in scripts/index_documents.py
-- voyage-finance-2 → 1024 (recommended; domain-specialized for financial text)
-- voyage-3 / voyage-3-large default → 1024   |   voyage-3-lite → 512
\set EMBEDDING_DIM 1024

-- ── raw_facts ─────────────────────────────────────────────────────────────────
-- Landing table: EDGAR companyfacts as-is. Never mutated after insert.
-- Unique on (cik, namespace, concept, unit, period_end, accession_no):
--   - accession_no already identifies the filing, so form is redundant here.
--   - period_start is excluded: NULL period_start (balance-sheet instant facts) would
--     not enforce uniqueness in Postgres unique constraints (NULL != NULL), and
--     duration facts (income/cashflow) differ by period_end anyway.
CREATE TABLE raw_facts (
    id            BIGSERIAL    PRIMARY KEY,
    ticker        TEXT         NOT NULL,
    cik           TEXT         NOT NULL,
    namespace     TEXT         NOT NULL,
    concept       TEXT         NOT NULL,
    unit          TEXT         NOT NULL,
    period_start  DATE,
    period_end    DATE         NOT NULL,
    fiscal_period TEXT,
    form          TEXT,
    accession_no  TEXT,
    filed_date    DATE,
    value         NUMERIC      NOT NULL,
    loaded_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (cik, namespace, concept, unit, period_end, accession_no)
);

-- ── statements ────────────────────────────────────────────────────────────────
-- Normalized GAAP. One row per canonical line item per period per company.
-- source_* fields carry provenance through every transform.
CREATE TABLE statements (
    id                BIGSERIAL  PRIMARY KEY,
    ticker            TEXT       NOT NULL,
    cik               TEXT       NOT NULL,
    statement         TEXT       NOT NULL CHECK (statement IN ('income', 'balance', 'cashflow')),
    line_item         TEXT       NOT NULL,
    line_order        INT        NOT NULL,
    period_end        DATE       NOT NULL,
    fiscal_period     TEXT,
    value             NUMERIC    NOT NULL,
    unit              TEXT       NOT NULL DEFAULT 'USD',
    source_concept    TEXT       NOT NULL,
    source_accession  TEXT       NOT NULL,
    source_form       TEXT       NOT NULL,
    source_filed_date DATE       NOT NULL,
    UNIQUE (ticker, statement, line_item, period_end, fiscal_period, source_accession)
);

-- ── statements_standalone ────────────────────────────────────────────────────
-- De-cumulated standalone quarterly figures derived from statements.
-- income/cashflow: Q2/Q3/Q4 derived via LAG (is_derived=TRUE), Q1/FY pass through as-is.
-- balance: always point-in-time, passes through unchanged (is_derived=FALSE).
-- Query target for all trend and comparison queries. statements is the raw anchor.
CREATE TABLE statements_standalone (
    id                     BIGSERIAL  PRIMARY KEY,
    ticker                 TEXT       NOT NULL,
    cik                    TEXT       NOT NULL,
    statement              TEXT       NOT NULL CHECK (statement IN ('income', 'balance', 'cashflow')),
    line_item              TEXT       NOT NULL,
    line_order             INT        NOT NULL,
    period_end             DATE       NOT NULL,
    fiscal_period          TEXT,
    value                  NUMERIC    NOT NULL,
    unit                   TEXT       NOT NULL DEFAULT 'USD',
    source_concept         TEXT       NOT NULL,
    source_accession       TEXT       NOT NULL,
    source_form            TEXT       NOT NULL,
    source_filed_date      DATE       NOT NULL,
    is_derived             BOOLEAN    NOT NULL DEFAULT FALSE,
    source_accession_prior TEXT,
    UNIQUE (ticker, statement, line_item, period_end, fiscal_period)
);

CREATE INDEX idx_standalone_item_period ON statements_standalone (ticker, line_item, period_end);
CREATE INDEX idx_standalone_period      ON statements_standalone (period_end, ticker);

-- ── kpis ──────────────────────────────────────────────────────────────────────
-- Non-GAAP operating metrics extracted from 8-K exhibits (any industry).
-- kpi and segment are open TEXT — no fixed enum. kpi_category and industry tag
-- the metric domain. confidence='hand_seeded' means sourced from anchor 8-K prose.
CREATE TABLE kpis (
    id                BIGSERIAL    PRIMARY KEY,
    ticker            TEXT         NOT NULL,
    cik               TEXT         NOT NULL,
    kpi               TEXT         NOT NULL,
    segment           TEXT         NOT NULL DEFAULT 'total',
    kpi_category      TEXT,                  -- 'operating', 'financial', 'market'
    industry          TEXT,                  -- 'hotel_reit', 'beauty', 'tech', etc.
    period_end        DATE         NOT NULL,
    fiscal_period     TEXT,
    value             NUMERIC      NOT NULL,
    unit              TEXT         NOT NULL,
    source_accession  TEXT         NOT NULL,
    source_form       TEXT         NOT NULL,
    source_filed_date DATE         NOT NULL,
    extracted_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    confidence        TEXT         NOT NULL DEFAULT 'hand_seeded'
                          CHECK (confidence IN ('hand_seeded', 'high', 'medium', 'low')),
    UNIQUE (ticker, kpi, segment, period_end, source_accession)
);

-- ── documents ─────────────────────────────────────────────────────────────────
-- RAG corpus metadata. doc_id = '{accession_no}/{form}' — stable and derivable.
CREATE TABLE documents (
    doc_id       TEXT  PRIMARY KEY,
    ticker       TEXT  NOT NULL,
    cik          TEXT  NOT NULL,
    form         TEXT  NOT NULL,
    accession_no TEXT  NOT NULL,
    filed_date   DATE  NOT NULL,
    title        TEXT,
    url          TEXT
);

-- ── doc_chunks ────────────────────────────────────────────────────────────────
-- RAG corpus chunks with pgvector embeddings.
-- chunk_id = '{doc_id}/{chunk_index}'.
-- embedding is nullable: column exists now; scripts/index_documents fills it in step 4.
-- Metadata columns (ticker, form, filed_date, fiscal_period) are denormalized so
-- retrieval can filter by metadata AND rank by similarity in a single SQL query.
CREATE TABLE doc_chunks (
    chunk_id      TEXT     PRIMARY KEY,
    doc_id        TEXT     NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    chunk_index   INT      NOT NULL,
    text          TEXT     NOT NULL,
    embedding     vector(:EMBEDDING_DIM),
    ticker        TEXT     NOT NULL,
    form          TEXT     NOT NULL,
    filed_date    DATE     NOT NULL,
    fiscal_period TEXT,
    UNIQUE (doc_id, chunk_index)
);

-- ── indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX idx_raw_facts_lookup   ON raw_facts  (ticker, concept, period_end);
CREATE INDEX idx_stmts_item_period  ON statements (ticker, line_item, period_end);
CREATE INDEX idx_stmts_period       ON statements (period_end, ticker);
CREATE INDEX idx_kpis_lookup        ON kpis       (ticker, kpi, period_end);
CREATE INDEX idx_chunks_metadata    ON doc_chunks (ticker, form, filed_date);

-- HNSW index deferred until step 4 when embeddings are populated.
-- Run after scripts/index_documents completes:
--   CREATE INDEX idx_chunks_embedding ON doc_chunks
--       USING hnsw (embedding vector_cosine_ops)
--       WITH (m = 16, ef_construction = 64);

-- ── query_log ─────────────────────────────────────────────────────────────────
-- Every question/answer pair with validator result. Enables passive correction harvesting.
CREATE TABLE query_log (
    id          BIGSERIAL    PRIMARY KEY,
    asked_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    question    TEXT         NOT NULL,
    answer      TEXT         NOT NULL,
    tool_calls  JSONB        NOT NULL DEFAULT '[]',
    validated   BOOLEAN,       -- NULL = validator not run, TRUE = passed, FALSE = flagged
    val_issues  TEXT[]         -- validator issue list; NULL when validated=TRUE
);

CREATE INDEX idx_query_log_asked ON query_log (asked_at DESC);

-- ── companies ─────────────────────────────────────────────────────────────────
-- Dynamic company registry. Any ticker can be onboarded on demand.
CREATE TABLE companies (
    ticker       TEXT        PRIMARY KEY,
    cik          TEXT        UNIQUE NOT NULL,
    name         TEXT,
    sic          TEXT,
    industry     TEXT,
    onboarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── company_data_status ───────────────────────────────────────────────────────
-- Tracks which data layers have been loaded and through what date.
CREATE TABLE company_data_status (
    ticker                  TEXT        PRIMARY KEY REFERENCES companies(ticker),
    xbrl_loaded_through     DATE,
    docs_indexed_through    DATE,
    form4_loaded_through    DATE,
    holdings_loaded_through DATE,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── insider_transactions ──────────────────────────────────────────────────────
-- Form 4 transaction rows. row_index = 0-based position within the filing's
-- transaction table, used as part of the unique key to handle multi-tranche
-- same-date same-code rows (e.g., PSU tax-withholding across grant lots).
CREATE TABLE insider_transactions (
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
CREATE INDEX idx_insider_ticker_date ON insider_transactions (ticker, transaction_date DESC);

-- ── institutional_holdings ────────────────────────────────────────────────────
-- Top institutional holders from 13F-HR filings.
CREATE TABLE institutional_holdings (
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
CREATE INDEX idx_holdings_ticker_date ON institutional_holdings (ticker, report_date DESC);
