# Tool Routing Rules

Use multiple tools per question. Follow this selection order:

0. **Trend / time-series questions** ("how has X trended", "growth over time",
   "history of", "trajectory", "X over the last N quarters", "change over time"):
   Use `get_financial_trends` — it returns pre-computed YoY/PoP % changes and
   anomaly signals (outlier / reversal / acceleration / deceleration) per period.
   Do NOT call `query_financials` + manually compute deltas for trend questions.

1. **GAAP metrics** (revenue, net income, assets, debt, CFO): `query_financials`.

2. **Operating KPIs** (RevPAR, ADR, occupancy, same-store sales, EBITDA margins, EPS, etc.):
   - Always call `get_kpi` first.
   - If `get_kpi` returns 0 rows, ALWAYS follow with `search_documents` — earnings releases
     contain full KPI tables in filing text even when not in the structured table.

3. **YoY or QoQ comparisons**: retrieve BOTH periods with separate tool calls, then compute
   the delta and % change yourself. Never skip the prior-period call.

4. **Market / segment breakdown, management commentary, guidance, risk factors**:
   always call `search_documents` in addition to structured tools.

5. **Peer benchmarking**: `compare_peers` — always pass `tickers` and `focus_ticker`
   explicitly based on the question. Do not assume a fixed peer set.

6. **New filings**: `fetch_latest_filing` to check EDGAR live.

For most operational questions, call 3–5 tools: structured data first, then document search
for context and commentary.

7. **Insider transactions**: "insider buying", "are executives buying",
   "insider activity", "Form 4", "who's buying shares", "insider ownership changes":
   use `get_insider_activity`. It auto-loads Form 4 data from EDGAR if needed.
   Interpret: is_open_market=true rows are definitional open-market purchases (code P).

**`search_documents` limit: at most 2 calls per question total.**
Write broad queries that cover multiple topics in one call rather than many narrow ones.
If you have already called `search_documents` and the results did not contain the specific
figure you need, do NOT re-search with reworded queries — semantically similar queries
return the same chunks. Instead: answer from what you have and clearly state the figure
isn't available in the retrieved filings, or try ONE structurally different approach
(e.g. `query_financials` for a GAAP value). Never call `search_documents` more than twice
for a single question.

**Onboard-on-miss:** Call `onboard_company(ticker)` ONLY when the ticker is genuinely
absent from the database — meaning ALL of `query_financials`, `get_kpi`, and
`search_documents` return 0 rows for that ticker AND no prior call this session returned
any data for it.

NEVER re-onboard a company because one specific metric was missing. A loaded company
may simply not report that metric. If ANY tool has returned rows for a ticker this
session, it is loaded — use the data tools directly and report what's available.

The database is the authority: `onboard_company` itself will return `{already_loaded: true,
status: "current"}` immediately if the company is already up-to-date, or run an incremental
top-up if new filings exist — but do not rely on this as an excuse to call it speculatively.
Only call it when you are confident the ticker has never been loaded.

`force_refresh=true` forces a full re-ingest regardless of current state. Use only when
explicitly asked for a manual refresh — never set it by default.
