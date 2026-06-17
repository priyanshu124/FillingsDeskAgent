"""
End-to-end eval tests against the live agent.

Requires DB_URL + ANTHROPIC_API_KEY env vars (both loaded from .env).
Skipped automatically in CI if either is absent.

Each test submits a golden question through ask_traced() and asserts that
every must_contain fragment appears (case-insensitive) in the answer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from agent.loop import ask_traced

load_dotenv()

GOLDEN = Path(__file__).parent / "evals" / "golden.jsonl"


def _cases() -> list[dict]:
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def db_url() -> str:
    url = os.environ.get("DB_URL")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not url or not key:
        pytest.skip("DB_URL or ANTHROPIC_API_KEY not set — live agent tests skipped")
    return url


@pytest.mark.parametrize(
    "case",
    _cases(),
    ids=[c["question"][:60] for c in _cases()],
)
def test_golden(case: dict, db_url: str) -> None:
    answer, trace, sources = ask_traced(case["question"], db_url)
    answer_lower = answer.lower()
    missing = [f for f in case["must_contain"] if f.lower() not in answer_lower]
    assert not missing, (
        f"Missing fragments: {missing}\n\n"
        f"Answer (first 800 chars):\n{answer[:800]}"
    )
