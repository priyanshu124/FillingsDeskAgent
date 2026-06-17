"""
Peer Desk MCP Server — stdio transport for Claude Desktop.

Exposes one tool: ask_peer_desk(question) → answer text.
All agent logic stays in the existing FastAPI server at localhost:8000.
This file is only a thin wrapper that relays questions over HTTP.

Usage (Claude Desktop registers this via claude_desktop_config.json):
  python mcp_server.py
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error

from mcp.server.fastmcp import FastMCP

FASTAPI_URL = "http://localhost:8000/ask"

mcp = FastMCP("Peer Desk")


@mcp.tool()
def ask_peer_desk(question: str) -> str:
    """
    Ask the Peer Desk hotel-REIT financial analyst a question.

    Covers Pebblebrook Hotel Trust (PEB) and peers HST and SHO.
    Returns a sourced answer with GAAP financials, lodging KPIs (RevPAR, ADR,
    occupancy, Hotel EBITDAre), and management commentary from SEC filings.

    Examples:
    - "What was PEB's RevPAR and occupancy for Q1 2026 vs Q1 2025?"
    - "Compare PEB and HST total assets as of Q1 2026."
    - "What did PEB management say about 2026 demand outlook?"
    - "Analyze PEB's debt levels and leverage trend."
    """
    if not question or not question.strip():
        return "Error: question must not be empty."

    payload = json.dumps({"question": question.strip(), "history": []}).encode()
    req = urllib.request.Request(
        FASTAPI_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
            return data.get("answer", "(no answer returned)")
    except urllib.error.URLError as exc:
        return (
            f"Error: Could not reach Peer Desk server at {FASTAPI_URL}. "
            f"Make sure the FastAPI server is running: "
            f"uvicorn api.main:app --port 8000\n\nDetail: {exc.reason}"
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        return f"Error: Peer Desk server returned HTTP {exc.code}.\n\nDetail: {body}"
    except Exception as exc:
        return f"Error: Unexpected failure calling Peer Desk.\n\nDetail: {exc}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
