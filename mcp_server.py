"""
FilingsDesk MCP Server.

Exposes one tool: ask_peer_desk(question) → answer text.
Calls the agent loop directly — no HTTP relay needed.

Two modes:
  stdio  — Claude Desktop registers this via claude_desktop_config.json
             python mcp_server.py
  ASGI   — Mounted inside api/main.py at /mcp for remote access
             (e.g. OmniAgent pointing at https://fillingsdeskagent.onrender.com/mcp)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

_DB_URL = os.environ.get("DB_URL", "")

mcp = FastMCP("FilingsDesk")


@mcp.tool()
def ask_peer_desk(question: str) -> str:
    """
    Ask FilingsDesk a financial question about any publicly traded company.

    Works for any SEC-registered company. Loads new tickers from EDGAR on demand.
    Returns a sourced answer with GAAP financials, industry KPIs, insider
    transactions (Form 4), peer comparisons, and management commentary from
    SEC filings (10-K, 10-Q, 8-K). Every figure is cited to its filing.

    Examples:
    - "How has NVIDIA's revenue and gross margin trended over the last 8 quarters?"
    - "What is Salesforce's current debt, cash position, and buyback activity?"
    - "Are any Apple executives buying or selling shares recently?"
    - "Compare Microsoft and Google's revenue growth over the last four quarters."
    - "What does Intuit's latest 10-Q say about competition and macro risks?"
    """
    if not question or not question.strip():
        return "Error: question must not be empty."
    if not _DB_URL:
        return "Error: DB_URL environment variable is not configured."

    try:
        from agent.loop import ask
        return ask(question.strip(), _DB_URL)
    except Exception as exc:
        return f"Error: {exc}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
