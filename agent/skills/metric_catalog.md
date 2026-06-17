# Metric Catalog

> **Note:** This is an example mapping for hotel-REIT metrics — not exhaustive.
> Any metric returned by `get_kpi` can be queried by name. Use `search_documents`
> to discover what KPIs appear in a company's earnings releases.

<!-- HUMAN-MAINTAINED: Update this file when new KPIs or GAAP line items are added to the DB.
     Do not auto-generate mappings — every entry must be verified against actual DB values.
     Last verified: 2026-06-06 -->

## GAAP Line Items — query_financials → statements_standalone

| Business Term | DB `line_item` | Statement | Notes |
|---|---|---|---|
| Revenue / Total Revenue | `revenues` | income | |
| Operating Income / EBIT | `operating_income` | income | |
| Net Income / Net Loss | `net_income` | income | |
| Interest Expense | `interest_expense` | income | |
| D&A / Depreciation & Amortization | `depreciation_amortization` | income | |
| Total Expenses / Operating Expenses | `total_expenses` | income | |
| Total Assets | `total_assets` | balance | |
| Total Liabilities | `total_liabilities` | balance | |
| Total Equity / Book Value / NAV | `total_equity` | balance | Includes noncontrolling interests |
| Cash / Cash Equivalents | `cash` | balance | |
| Long-Term Debt / LTD / Total Debt | `long_term_debt` | balance | |
| Real Estate Net / Net PP&E / Gross Real Estate | `real_estate_net` | balance | |
| Operating Cash Flow / CFO / Cash from Operations | `cfo` | cashflow | |
| Investing Cash Flow / CFI / Capex | `cfi` | cashflow | |
| Financing Cash Flow / CFF | `cff` | cashflow | |

## Lodging KPIs — get_kpi → kpis table

| Business Term | DB `kpi` | Unit | Notes |
|---|---|---|---|
| RevPAR / Revenue Per Available Room | `revpar` | USD | Same-property basis |
| ADR / Average Daily Rate | `adr` | USD | Same-property basis |
| Occupancy / Occupancy Rate | `occupancy` | % | Stored as 68.5 for 68.5% |
| Hotel EBITDAre / Adjusted EBITDAre | `hotel_ebitdare` | USD | Absolute dollars (e.g. 73300000 = $73.3M) |
| Adj FFO / Adjusted FFO per diluted share | `adj_ffo_per_share` | USD/share | Per share value |
| Total RevPAR / Same-Property Total RevPAR | `total_revpar` | USD | Includes non-room revenue |
