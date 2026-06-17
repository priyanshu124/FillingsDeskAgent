# CLAUDE.md — FilingsDesk

Agentic finance analyst for any publicly traded company. An analyst asks one
natural-language question; the agent plans tool calls and returns a **sourced,
verifiable** answer. Planner + tools — not a dashboard with an LLM bolted on.

Works for any SEC-registered company. `onboard_company` loads any ticker from EDGAR
on demand; the three-state guard (not loaded / current / stale) ensures repeat queries
return immediately from cache.

## Hard rules (never violate)
1. **Grounded only** — never assert a number a tool didn't return. No figures from memory.
2. **Provenance always** — every figure carries form, filed_date, accession, URL.
3. **Inspectable** — log every tool call (name, inputs, outputs).
4. **Tools are contracts** — typed schema + description; build one fully before the next.
5. **Augment, never replace.**

## Stack (match the team's env; don't substitute without noting here)
Python 3.11+ · FastAPI · MCP tools + Claude API · **PostgreSQL single store** ·
`pgvector` (HNSW, hybrid metadata+semantic in one query) · `edgartools` ingestion ·
Tableau (live to Postgres) · Vue.js (thin panel) · pytest.

One Postgres holds structured data AND embeddings. Never split vectors into a separate
store.

## Conventions
Type hints everywhere · tool logic = pure functions (unit-testable without the agent) ·
parameterized SQL only (never string-built from model output) · provenance fields
travel through every transform · secrets in `.env`, never committed · every tool ships
with pytest before it's "done".

## Deeper docs — read the one the task needs, not all of them
- Architecture, two retrieval modes, build order → `docs/architecture.md`
- Full schema + XBRL normalization rules → `docs/data-model.md`
- Tool contracts (I/O schemas) → `docs/tools.md`
- EDGAR rules, filing types, seed plan → `docs/data-sources.md`
- Run/setup commands → `docs/runbook.md`

## Working agreement (token economy)
Point edits at named files; don't crawl to "find" things. Plan → approve → edit for
multi-file changes. `/clear` between unrelated tasks. Self-verify via seed DB + pytest.
One tool slice at a time, then stop.
