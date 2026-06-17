# Data model

Land raw → normalize → serve. dbt-style staging → normalized → marts. One Postgres.

## `raw_facts` — landing, untouched
Everything from EDGAR `companyfacts`, as-is, for provenance and reprocessing.
`ticker, cik, namespace, concept, unit, period_start, period_end, fiscal_period,
form, accession_no, filed_date, value, loaded_at`

## `statements` — normalized GAAP
Raw facts mapped to canonical line items, ordered by statement.
`ticker, cik, statement (income|balance|cashflow), line_item (canonical), line_order,
period_end, fiscal_period, value, unit, source_concept, source_accession, source_form,
source_filed_date`

### XBRL normalization (the real engineering, not glue)
Full statements bring real-world mess a curated metric list hides. The mapping layer
must handle:
- **Tag drift** — companies change which us-gaap tags they use across periods.
- **Custom extension tags** — issuer-specific tags alongside standard ones.
- **Synonym labels** — same economic concept, different label/tag per company.
- **Overlapping filings** — same period tagged in multiple filings; dedupe to the
  authoritative one (prefer the original periodic filing; track `source_accession`).
Keep a canonical line-item dictionary mapping {company, raw concept} → canonical
line_item + statement + order. This dictionary is a maintained artifact.

## `kpis` — non-GAAP lodging metrics (Claude-extracted from 8-K exhibits, not in XBRL)
`ticker, cik, kpi (revpar|adr|occupancy|adj_ffo_per_share|hotel_ebitdare|total_revpar),
segment (total|urban|resort), period_end, fiscal_period, value, unit, source_accession,
source_form, source_filed_date, extracted_at, confidence`

## `documents` / `doc_chunks` — RAG corpus
- `documents`: `doc_id, ticker, cik, form, accession_no, filed_date, title, url`
- `doc_chunks`: `chunk_id, doc_id, chunk_index, text, embedding vector(N)`, plus
  denormalized `ticker, form, filed_date, fiscal_period` so retrieval can filter by
  metadata AND rank by similarity in one SQL query (pgvector + WHERE).

## Volume expectation
Full statements: ~200–400 concepts × ~56 periods × 5 cos ≈ 50–100k rows. Chunks:
~5 cos × ~56 docs × ~50 ≈ 14k. Laptop-scale; HNSW index is plenty.
