# Data sources & rules of engagement

## EDGAR
- Free, no API key. Use `edgartools` + `data.sec.gov`.
- **Set a real descriptive User-Agent** (`set_identity("Name <email>")`) or EDGAR
  returns 403. This is the most common gotcha.
- Stay well under 10 req/sec; ingestion jobs sleep (~0.15s) between calls.
- Public data only in this build. RAG is framed as "point at your internal deal room
  later" — same engine, private corpus.

## Filing types → layers
- **10-K** — annual, full audited statements. *Understand the company.*
- **10-Q** — quarterly, unaudited. *Track quarterly performance.*
- **8-K** — current report; material events incl. earnings exhibits (semi-structured
  press-release text). *Monitor events.*
- Structured statements come from 10-K/10-Q XBRL (clean). Lodging KPIs come from 8-K
  exhibits via the Claude extraction pass (no XBRL tags for RevPAR/ADR).

## Peer set (verify CIKs against EDGAR before seeding)
PEB 1474098 (focus) · HST 1070750 · RLJ 1542684 · SHO 1295810 · DRH 1298946.

## Real anchor filings (PEB) for the seed
- 2026-04-28 8-K — Q1 2026 earnings: net loss $18.4M; Adj EBITDAre $73.3M; Adj FFO/sh
  $0.32; Same-Property RevPAR +11.8%; Same-Property Total Revenue $343.8M; +327bps margin.
- 2026-02-25 8-K — FY/Q4 2025 earnings: FY net loss $62.2M (incl. $48.9M impairments);
  2026 guidance Adj EBITDAre $325–339M, Adj FFO/sh $1.50–1.62; cash $196.2M; net
  debt/EBITDA 5.9x.
- 2026-02-13 8-K — debt refinancing ($450M unsecured term loan; revolver extension).

## Seed plan
PEB + 2 peers, last ~8 quarters. Every figure carries source_form / source_accession /
source_filed_date so every demo number is traceable. GAAP figures from XBRL; the two
anchor 8-Ks above give real non-GAAP KPI values to hand-seed first, before the live
extraction pass is wired.
