# Architecture

## What it is
Peer Desk removes a manual bottleneck: analysts hand-assemble peer financials, lodging
KPIs, and filing/transcript context from EDGAR and IR portals every quarter. One
natural-language question → a sourced answer in seconds, so time goes to judgment, not
data entry.

The agent *is* the architecture. Tools are the units of work. Control flow is inverted
from a pipeline: instead of hardcoded "for question X run query Y", the agent is given
well-described tools and plans which to call for a question it has never seen.

## Two retrieval modes, one engine
- **Structured** — GAAP statements from 10-K/10-Q XBRL + non-GAAP lodging KPIs (RevPAR,
  ADR, occupancy, Adjusted FFO, Hotel EBITDAre) extracted from 8-K earnings exhibits.
- **Unstructured** — RAG over 10-Ks, earnings exhibits, earnings-call transcripts.

Many real questions need both ("did margin move *and* what did management say about
why"). The agent composes tools to answer. Same engine, two triggers:
- *Event-driven* (earnings-day first read): new 8-K → extract KPIs → compare to
  prior/peers → pull supporting transcript quote → brief.
- *Ad-hoc* (document Q&A): analyst asks anytime → sourced answer with citations.

## Audience framing (who evaluates what)
- CFO / Co-President (Martz): business outcome — hours saved, faster turnaround.
- Controller / Finance (Dittamo, Gordon, Martin): accuracy, controls, provenance.
- Revenue Strategy (Burkett): RevPAR/ADR/occupancy peer comparisons.
- Asset Management (Randall, Latoff): document/property questions — the RAG path.
- Enterprise AI (Klein, Tran): architecture soundness, maintainability, true agency.

## Build order (vertical, verifiable slices — never layer-by-layer)
1. Spec lock: CLAUDE.md + `docs/tools.md` agreed. No feature code.
2. Schema + real seed (PEB + 2 peers, ~8 quarters, every figure sourced).
3. Walking skeleton: `query_financials` + minimal agent loop → one real sourced answer.
4. Add tools one at a time: `get_kpi` → `search_documents` → `compare_peers` →
   `fetch_latest_filing`. Each: contract → impl → tests → provenance → verified.
5. FastAPI wrap. 6. Thin Vue panel. 7. Tableau live connection.
