"""
Agent loop for Peer Desk.

Exposes all tool contracts from docs/tools.md as Claude tool_use tools.
Takes one question, lets the model plan and call tools, returns a final sourced answer.

Every tool call is logged: name, inputs, row count.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import anthropic
import psycopg2
from dotenv import load_dotenv

from tools.compare_peers import compare_peers
from tools.fetch_latest_filing import fetch_latest_filing
from tools.get_financial_trends import get_financial_trends
from tools.get_insider_activity import get_insider_activity
from tools.get_kpi import get_kpi
from tools.list_companies import list_companies
from tools.onboard_company import onboard_company
from tools.query_financials import query_financials
from tools.search_documents import search_documents

load_dotenv()

logger = logging.getLogger(__name__)

# Confirmed against anthropic 0.105.2 types/model_param.py
MODEL = "claude-sonnet-4-6"

MAX_TURNS = 10

VALIDATOR_MODEL = "claude-haiku-4-5-20251001"

VALIDATOR_SYSTEM = """\
You are a financial answer auditor for a universal SEC-filing analytics system.
You receive the actual tool result data alongside the answer. Cross-check every
specific figure in the answer against those results.

Flag any of these issues:

1. FABRICATED_SPECIFIC — a specific number, share count, name, date, or accession in
   the answer that does NOT appear in any tool result data_sample. Check every dollar
   amount, share count, percentage, and named figure. This is the highest-priority check.

2. UNSOURCED_NUMBER — a figure appears in the answer for a ticker/period where no tool
   returned any rows at all (not just missing from the sample — the tool had 0 rows).

3. DERIVED_AS_REPORTED — the agent computed a value (e.g. sum, difference, ratio of
   other figures) and presented it as if the company directly reported it, without
   labeling it as derived or calculated.

4. TRUNCATION_INVENTED — a tool result was truncated (data_sample ends with
   "[truncated]") and the answer presents specific figures for data that was cut off.

5. MISSING_SECTION — the answer lacks one of: a headline, a summary table, or a
   Sources section.

Be precise in your issues: name the specific figure and which check it failed.
Respond ONLY with valid JSON — no commentary:
  {"pass": true}
  {"pass": false, "issues": ["FABRICATED_SPECIFIC: 'Jon Bortz bought 12,500 shares' — share count not in any tool result"]}
"""


def _load_system() -> str:
    """Load SYSTEM prompt from agent/skills/*.md files at import time."""
    skills_dir = Path(__file__).parent / "skills"
    parts = []
    for fname in ["domain_context.md", "routing.md", "answer_format.md", "metric_catalog.md"]:
        p = skills_dir / fname
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


SYSTEM = _load_system()  # loaded once; restart server after editing skill files

# ── Tool schemas (match docs/tools.md exactly) ────────────────────────────────

QUERY_FINANCIALS_TOOL: dict[str, Any] = {
    "name": "query_financials",
    "description": (
        "Query normalized GAAP financial statements (income, balance sheet, cash flow) "
        "from the database. "
        "IMPORTANT: income and cashflow figures are already standalone quarterly values — "
        "Q2 is the actual Q2 figure (NOT H1 cumulative), Q3 is the actual Q3 figure "
        "(NOT 9-month cumulative). De-cumulation is done in the database. "
        "Do NOT mention derivation, subtraction, or YTD math in your answers — "
        "just report the values as returned. "
        "Returns rows with source_form, source_accession, and source_filed_date — "
        "cite these in your answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Ticker symbols, e.g. ["PEB", "HST"]',
            },
            "line_items": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Canonical line items: revenues, total_expenses, "
                    "depreciation_amortization, operating_income, interest_expense, "
                    "net_income, total_assets, total_liabilities, total_equity, "
                    "cash, long_term_debt, real_estate_net, cfo, cfi, cff. "
                    "Omit to return all."
                ),
            },
            "statement": {
                "type": "string",
                "enum": ["income", "balance", "cashflow"],
                "description": "Filter to one statement type. Omit for all.",
            },
            "period_start": {
                "type": "string",
                "description": "Inclusive lower bound on period_end, ISO date YYYY-MM-DD.",
            },
            "period_end": {
                "type": "string",
                "description": "Inclusive upper bound on period_end, ISO date YYYY-MM-DD.",
            },
        },
        "required": ["tickers"],
    },
}

GET_KPI_TOOL: dict[str, Any] = {
    "name": "get_kpi",
    "description": (
        "Query non-GAAP operating KPIs from the structured database. "
        "Any metric extracted from 8-K earnings releases is queryable by name "
        "(e.g. revpar, adr, occupancy, adj_ffo_per_share, hotel_ebitdare, "
        "same_store_sales_growth, adj_ebitda_margin). "
        "If this returns 0 rows, follow up with search_documents — "
        "KPI tables are in the indexed 8-K filing text. "
        "Returns rows with source_form, source_accession, source_filed_date — cite these."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Ticker symbols, e.g. ["PEB"]',
            },
            "kpis": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "KPI names to filter on (snake_case). "
                    "Omit to return all KPIs for the ticker."
                ),
            },
            "segment": {
                "type": "string",
                "description": "Segment filter, e.g. 'total', 'urban', 'resort'. Default: total.",
            },
            "period_start": {"type": "string", "description": "ISO date YYYY-MM-DD."},
            "period_end":   {"type": "string", "description": "ISO date YYYY-MM-DD."},
        },
        "required": ["tickers"],
    },
}

SEARCH_DOCUMENTS_TOOL: dict[str, Any] = {
    "name": "search_documents",
    "description": (
        "Semantic search over indexed SEC filing text (10-K, 10-Q, 8-K). "
        "Use this for: (1) qualitative questions — management commentary, MD&A, guidance, "
        "risk factors; (2) any KPI (RevPAR, same-store sales, EBITDA margin, etc.) "
        "when get_kpi returns 0 rows — earnings releases contain full KPI tables in text. "
        "Returns text chunks with filing metadata — cite ticker, form, filed_date."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query.",
            },
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter to specific tickers. Omit for all.",
            },
            "forms": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Filter to form types, e.g. ["10-Q", "8-K"].',
            },
            "period_start": {"type": "string", "description": "ISO date YYYY-MM-DD."},
            "period_end":   {"type": "string", "description": "ISO date YYYY-MM-DD."},
            "k": {
                "type": "integer",
                "description": "Number of chunks to return. Maximum 5 — values above 5 are capped. Default 5.",
            },
        },
        "required": ["query"],
    },
}

COMPARE_PEERS_TOOL: dict[str, Any] = {
    "name": "compare_peers",
    "description": (
        "Compare one metric across a set of tickers for a given period. "
        "Pass all relevant tickers explicitly based on the question. "
        "Returns rows sorted by value, with is_focus=true for focus_ticker. "
        "Use metric_kind='statement' for GAAP line items, 'kpi' for non-GAAP metrics. "
        "Provenance fields are included — cite them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'All tickers to compare, e.g. ["PEB", "HST", "SHO"].',
            },
            "focus_ticker": {
                "type": "string",
                "description": "The company of primary interest — flagged as is_focus=true in results.",
            },
            "metric": {
                "type": "string",
                "description": "Line item or KPI name, e.g. 'net_income' or 'revpar'.",
            },
            "metric_kind": {
                "type": "string",
                "enum": ["statement", "kpi"],
                "description": "'statement' for GAAP, 'kpi' for non-GAAP metrics.",
            },
            "period_end": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD — exact period_end to compare.",
            },
            "segment": {
                "type": "string",
                "description": "Segment filter (KPI only). Default: total.",
            },
        },
        "required": ["tickers", "focus_ticker", "metric", "metric_kind", "period_end"],
    },
}

FETCH_LATEST_FILING_TOOL: dict[str, Any] = {
    "name": "fetch_latest_filing",
    "description": (
        "Check EDGAR live for the most recent filings for given tickers. "
        "Use this to find new 8-Ks, 10-Qs, or 10-Ks filed since a given date. "
        "Returns accession_no, filed_date, url, and is_new flag."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Ticker symbols, e.g. ["PEB", "HST"]',
            },
            "forms": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Form types to check, e.g. ["8-K", "10-Q", "10-K"]. Default: all three.',
            },
            "since": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Only return filings on or after this date.",
            },
        },
        "required": ["tickers"],
    },
}

ONBOARD_COMPANY_TOOL: dict[str, Any] = {
    "name": "onboard_company",
    "description": (
        "Load all available data for a company not yet in the database. "
        "Fetches XBRL financials from EDGAR, indexes 8-K earnings releases and 10-Qs "
        "into the RAG corpus, and extracts non-GAAP KPIs. "
        "Takes 30–90 seconds for a new company; returns immediately for an already-loaded one. "
        "ONLY call this if the ticker is genuinely absent from the database — "
        "i.e., ALL data tools (query_financials, get_kpi, search_documents) returned "
        "0 rows AND you have not seen any data for this ticker in this session. "
        "Do NOT call this just because one specific metric was missing — a loaded company "
        "may simply not report that metric. "
        "If a previous call returned data for this ticker (any metric, any period), "
        "the company IS loaded — never call onboard_company for it again. "
        "Use force_refresh=true only for manual refresh; never set it by default. "
        "Returns {already_loaded: true, status: 'current'} if already up-to-date, "
        "{incremental: true} if new filings were ingested, or full onboard counts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Ticker symbol of the company to onboard, e.g. 'AAPL'.",
            },
            "periods_back": {
                "type": "integer",
                "description": (
                    "Quarters of history to load, counting back from the most recent "
                    "complete quarter end. Default: 8 (~2 years)."
                ),
            },
            "force_refresh": {
                "type": "boolean",
                "description": (
                    "Force a full re-ingest from EDGAR even if the company is already loaded. "
                    "Use only for manual refresh — never set this by default."
                ),
            },
        },
        "required": ["ticker"],
    },
}

GET_INSIDER_ACTIVITY_TOOL: dict[str, Any] = {
    "name": "get_insider_activity",
    "description": (
        "Fetch and query insider transactions (Form 4 filings) for a company. "
        "Use for questions about insider buying/selling, executive share purchases, "
        "'are insiders buying', 'insider ownership changes', 'who bought shares', "
        "'Form 4 activity'. "
        "Automatically loads Form 4 data from EDGAR if not yet cached. "
        "transaction_code: P=open-market purchase, S=sale, A=award/grant, "
        "F=tax-withholding, M=option exercise. "
        "is_open_market=true only for code P (definitional open-market purchase). "
        "Returns provenance: accession_no, filed_date per transaction."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Ticker symbol of the company, e.g. 'PEB'",
            },
            "since": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD; load/query transactions from this date. Default: 365 days ago.",
            },
            "limit": {
                "type": "integer",
                "description": "Max transactions to return. Default 50.",
            },
        },
        "required": ["ticker"],
    },
}

LIST_COMPANIES_TOOL: dict[str, Any] = {
    "name": "list_companies",
    "description": (
        "List all companies currently loaded in the database. "
        "Call this when the user asks which companies are available, "
        "'what can I ask about', 'what's loaded', 'which tickers do you have', "
        "or any similar inventory question. "
        "Returns ticker, name, industry, and data-through dates for each company."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

GET_FINANCIAL_TRENDS_TOOL: dict[str, Any] = {
    "name": "get_financial_trends",
    "description": (
        "Multi-period trend analysis for a single ticker. Retrieves the last N quarters "
        "for each requested metric (GAAP line items OR KPI names) and returns computed "
        "period-over-period and year-over-year % changes plus an anomaly signal. "
        "Use this for any question about trends, growth trajectory, historical performance, "
        "'how has X changed', 'over time', or time-series across multiple quarters. "
        "Prefer over query_financials + manual delta for trend questions. "
        "signal values: 'outlier' | 'reversal' | 'acceleration' | 'deceleration' | null. "
        "Returns full provenance per period."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Single ticker symbol, e.g. 'PEB'",
            },
            "metrics": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "GAAP line_item names (revenues, operating_income, net_income, "
                    "interest_expense, cfo, long_term_debt, …) OR KPI names "
                    "(revpar, adr, occupancy, hotel_ebitdare, adj_ffo_per_share, …). "
                    "Mix freely — the tool queries both tables."
                ),
            },
            "periods": {
                "type": "integer",
                "description": "Quarters to return per metric. Default 8 (~2 years).",
            },
        },
        "required": ["ticker", "metrics"],
    },
}

ALL_TOOLS = [
    GET_INSIDER_ACTIVITY_TOOL,
    GET_FINANCIAL_TRENDS_TOOL,
    QUERY_FINANCIALS_TOOL,
    GET_KPI_TOOL,
    SEARCH_DOCUMENTS_TOOL,
    COMPARE_PEERS_TOOL,
    FETCH_LATEST_FILING_TOOL,
    ONBOARD_COMPANY_TOOL,
    LIST_COMPANIES_TOOL,
]


# ── Agent loop ────────────────────────────────────────────────────────────────

def _dispatch(name: str, inputs: dict, conn, voyage_client) -> list:
    """Route a tool_use block to the correct function. Returns serializable rows."""
    if name == "get_insider_activity":
        return get_insider_activity(conn, **inputs)
    if name == "get_financial_trends":
        return get_financial_trends(conn, **inputs)
    if name == "query_financials":
        return query_financials(conn, **inputs)
    if name == "get_kpi":
        return get_kpi(conn, **inputs)
    if name == "search_documents":
        # Hard-cap k at 5 — enough to find the answer without TPM blowout.
        inputs = {**inputs, "k": min(int(inputs.get("k", 5)), 5)}
        return search_documents(conn, voyage_client, **inputs)
    if name == "compare_peers":
        return compare_peers(conn, **inputs)
    if name == "fetch_latest_filing":
        return fetch_latest_filing(**inputs)
    if name == "onboard_company":
        claude_client = anthropic.Anthropic()
        result = onboard_company(
            conn, voyage_client=voyage_client, claude_client=claude_client, **inputs
        )
        return [result]
    if name == "list_companies":
        return list_companies(conn)
    raise ValueError(f"Unknown tool: {name!r}")


def _get_voyage_client():
    """Lazy-instantiate the Voyage client — only when search_documents is actually called."""
    import voyageai
    return voyageai.Client()


def _extract_sources(rows: list, tool_name: str) -> list[dict]:
    """Pull deduplicated filing provenance out of tool result rows."""
    seen: set[tuple] = set()
    sources = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ticker    = r.get("ticker")
        form      = r.get("source_form") or r.get("form")
        accession = r.get("source_accession") or r.get("accession_no")
        filed     = r.get("source_filed_date") or r.get("filed_date")
        url       = r.get("url")
        if filed is not None:
            filed = str(filed)
        key = (ticker, form, accession)
        if key not in seen and any(key):
            seen.add(key)
            sources.append({"ticker": ticker, "form": form,
                            "accession": accession, "filed_date": filed, "url": url})
    return sources


def _validate(answer: str, trace: list[dict], client) -> tuple[bool, list[str]]:
    """
    Haiku second-pass audit. Returns (passed, issues).
    Non-fatal — any error returns (True, []) so transient failures never block answers.
    """
    tool_summary = json.dumps(
        [
            {
                "tool":        t["tool"],
                "rows":        t["rows_returned"],
                "data_sample": t.get("result_sample", ""),
            }
            for t in trace
        ],
        indent=2,
    )
    try:
        resp = client.messages.create(
            model=VALIDATOR_MODEL,
            max_tokens=768,
            system=VALIDATOR_SYSTEM,
            messages=[{"role": "user", "content":
                f"TOOL CALL TRACE (with data samples):\n{tool_summary}\n\nANSWER TO AUDIT:\n{answer}"}],
        )
        raw = resp.content[0].text.strip()
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if not m:
            logger.warning("Validator returned no JSON — treating as pass")
            return True, []
        data = json.loads(m.group())
        if data.get("pass"):
            return True, []
        return False, data.get("issues", [])
    except Exception as exc:
        logger.warning("Validator error (non-fatal): %s", exc)
        return True, []


def _generate_followups(question: str, answer: str, client) -> list[str]:
    """
    Post-validation Haiku call — returns 2-3 follow-up question strings.
    Completely separate from the answer pipeline; non-fatal on any error.
    """
    try:
        resp = client.messages.create(
            model=VALIDATOR_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content":
                f"Question asked: {question}\n\n"
                f"Answer (first 600 chars): {answer[:600]}\n\n"
                "Suggest 2-3 follow-up questions the user might ask next. "
                "Only suggest questions answerable from SEC filings: "
                "GAAP financials, trends, KPIs, management commentary, "
                "insider transactions (Form 4), peer comparisons. "
                "NEVER suggest: stock prices, analyst ratings, market cap, "
                "earnings estimates, or real-time news. "
                "Return ONLY a JSON array, no commentary: "
                '["question 1", "question 2", "question 3"]'
            }]
        )
        raw = resp.content[0].text.strip()
        import re as _re
        m = _re.search(r'\[.*?\]', raw, _re.DOTALL)
        if m:
            fups = json.loads(m.group())
            if isinstance(fups, list):
                return [str(q) for q in fups[:3]]
    except Exception as exc:
        logger.debug("follow-up generation failed (non-fatal): %s", exc)
    return []


def _log_query(
    conn,
    question: str,
    answer: str,
    trace: list[dict],
    validated: bool | None,
    val_issues: list[str],
) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO query_log
                       (question, answer, tool_calls, validated, val_issues)
                   VALUES (%s, %s, %s, %s, %s)""",
                (question, answer, json.dumps(trace), validated, val_issues or None),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("query_log insert failed (non-fatal): %s", exc)


def _run_loop(
    question: str,
    db_url: str,
    model: str,
    history: list[dict] | None = None,
    progress_cb=None,
) -> tuple[str, list[dict], list[dict], list[str]]:
    """
    Core agent loop. Returns (answer, trace, sources).

    history — prior conversation turns: [{"question": str, "answer": str}, ...]
    trace   — one dict per tool call: {tool, inputs, rows_returned, elapsed_ms}
    sources — deduplicated filing provenance across all tool results
    """
    import time as _time

    conn = psycopg2.connect(db_url)
    client = anthropic.Anthropic()
    voyage_client = None

    # Build message list — prepend prior turns so model has context
    messages: list[dict] = []
    for turn in (history or []):
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})
    trace: list[dict] = []
    all_sources: list[dict] = []

    _validated_once = False  # only allow one validator retry per question

    try:
        for _turn in range(MAX_TURNS):
            # When 3 turns remain, nudge the agent to stop calling tools and answer.
            # This prevents exhaustion on open-ended analysis questions.
            turns_left = MAX_TURNS - _turn
            if turns_left == 3 and len(trace) >= 3:
                messages.append({"role": "user", "content":
                    "You have gathered sufficient data. "
                    "Please now write your complete, sourced answer — do not call any more tools."
                })
                logger.info("Injected synthesize-now nudge at turn %d", _turn)

            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM,
                tools=ALL_TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                answer = "".join(
                    block.text for block in response.content if block.type == "text"
                )

                # Validator pass — one retry allowed per question; skip if turns are low.
                if not _validated_once and turns_left > 2:
                    validated, val_issues = _validate(answer, trace, client)
                    if not validated:
                        _validated_once = True
                        logger.warning("Validator flagged issues: %s", val_issues)
                        messages.append({"role": "assistant", "content": response.content})
                        messages.append({"role": "user", "content":
                            "Before delivering your answer, the following data issues were detected. "
                            "Write the final answer for the user now — start directly at the headline. "
                            "FORBIDDEN anywhere in the answer body: "
                            "'the auditor is correct', 'now I have', 'let me re-examine', "
                            "'let me re-present', 'let me re-verify', 'here is the fully sourced answer', "
                            "'as corrected', 'the previous answer', or any preamble that narrates "
                            "your process or implies a revision. The answer must read as the first and "
                            "only response the user has seen.\n\n"
                            "Data issues to address:\n" +
                            "\n".join(f"- {i}" for i in val_issues)
                        })
                        continue
                else:
                    validated, val_issues = True, []

                # Deduplicate sources across all calls
                seen: set[tuple] = set()
                unique_sources = []
                for s in all_sources:
                    key = (s["ticker"], s["form"], s["accession"])
                    if key not in seen:
                        seen.add(key)
                        unique_sources.append(s)

                _log_query(conn, question, answer, trace, validated, val_issues)
                follow_ups = _generate_followups(question, answer, client)
                return answer, trace, unique_sources, follow_ups

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    logger.info("tool_call | name=%s | inputs=%s", block.name, block.input)
                    if progress_cb:
                        progress_cb({"type": "tool_start", "tool": block.name, "inputs": block.input})
                    if block.name in ("search_documents", "onboard_company") and voyage_client is None:
                        voyage_client = _get_voyage_client()
                    t0 = _time.monotonic()
                    rows = _dispatch(block.name, block.input, conn, voyage_client)
                    elapsed_ms = int((_time.monotonic() - t0) * 1000)
                    logger.info("tool_result | name=%s | rows=%d", block.name, len(rows))
                    if progress_cb:
                        done_event: dict = {
                            "type": "tool_done",
                            "tool": block.name,
                            "rows": len(rows),
                            "elapsed_ms": elapsed_ms,
                        }
                        if block.name == "onboard_company" and rows:
                            r = rows[0]
                            done_event["already_loaded"] = r.get("already_loaded", False)
                            done_event["cached"]         = r.get("cached", False)
                            done_event["incremental"]    = r.get("incremental", False)
                        progress_cb(done_event)

                    # Cap content at 5 KB so multi-turn contexts don't exceed TPM limits.
                    content = json.dumps(rows, default=str)
                    truncated = len(content) > 5000
                    if truncated:
                        content = content[:5000] + '... [truncated for context length]"}'

                    # Store a sample for the validator to cross-check figures against.
                    _SAMPLE_CHARS = 2000
                    result_sample = content[:_SAMPLE_CHARS]
                    if truncated or len(content) > _SAMPLE_CHARS:
                        result_sample += " [truncated]"

                    trace.append({
                        "tool": block.name,
                        "inputs": block.input,
                        "rows_returned": len(rows),
                        "elapsed_ms": elapsed_ms,
                        "result_sample": result_sample,
                    })
                    all_sources.extend(_extract_sources(rows, block.name))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                    })
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            logger.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

        # Best-effort answer from the last end_turn text seen, or a clear error.
        raise RuntimeError(
            f"Agent loop exhausted {MAX_TURNS} turns without reaching end_turn."
        )

    finally:
        conn.close()


def ask(question: str, db_url: str, model: str = MODEL) -> str:
    """Returns the final text answer (CLI entry point)."""
    answer, *_ = _run_loop(question, db_url, model)
    return answer


def ask_traced(
    question: str,
    db_url: str,
    model: str = MODEL,
    history: list[dict] | None = None,
    progress_cb=None,
) -> tuple[str, list[dict], list[dict], list[str]]:
    """Returns (answer, trace, sources, follow_ups) — used by the FastAPI layer."""
    return _run_loop(question, db_url, model, history=history, progress_cb=progress_cb)


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    db_url = os.environ.get("DB_URL")
    if not db_url:
        sys.exit("DB_URL env var not set — check your .env file.")

    question = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "What was PEB's Hotel EBITDAre for Q1 2026?"
    )
    import sys
    answer = ask(question, db_url)
    sys.stdout.buffer.write(("\n" + answer + "\n").encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
