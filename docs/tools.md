# Tool contracts

Each tool is a discrete MCP tool with typed I/O. A tool returns **data + provenance,
never prose** — the agent composes prose from outputs. Build in listed order.

## 1. `query_financials`
Parameterized SQL over `statements`.
- in: `tickers: list[str]`, `line_items: list[str]`, `statement: str|None`,
  `period_start: str|None`, `period_end: str|None`
- out: rows of `{ticker, statement, line_item, period_end, fiscal_period, value, unit,
  source_form, source_accession, source_filed_date}`

## 2. `get_kpi`
Lodging KPIs from `kpis`.
- in: `tickers: list[str]`, `kpis: list[str]`, `segment: str = "total"`,
  `period_start: str|None`, `period_end: str|None`
- out: rows of `{ticker, kpi, segment, period_end, fiscal_period, value, unit,
  source_form, source_accession, source_filed_date}`

## 3. `search_documents`
Hybrid retrieval over `doc_chunks`: metadata filter + pgvector similarity, one query.
- in: `query: str`, `tickers: list[str]|None`, `forms: list[str]|None`,
  `period_start: str|None`, `period_end: str|None`, `k: int = 8`
- out: chunks of `{text, ticker, form, filed_date, fiscal_period, doc_title, url,
  score}`

## 4. `compare_peers`
Convenience wrapper over 1–2: one metric/KPI across the peer set for a period, PEB
flagged.
- in: `metric: str`, `metric_kind: "statement"|"kpi"`, `period_end: str`,
  `segment: str = "total"`
- out: rows of `{ticker, is_focus, value, unit, source_*}` sorted, PEB marked.

## 5. `fetch_latest_filing`
Live EDGAR check for new filings since last load (powers earnings-day first read).
- in: `tickers: list[str]`, `forms: list[str] = ["8-K","10-Q","10-K"]`,
  `since: str|None`
- out: `{ticker, form, filed_date, accession_no, url, is_new}`

## Agent guardrails
- If a tool returns empty, the agent says there's no data — it does not fill the gap
  from memory.
- The agent must cite provenance fields from tool outputs in its answer.
- Tool selection and inputs are logged for inspection.
