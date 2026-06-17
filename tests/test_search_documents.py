"""
Tests for search_documents against the indexed seed corpus.
Requires DB_URL and VOYAGE_API_KEY in environment, and doc_chunks populated
by scripts/index_documents.py.
"""

from __future__ import annotations

import os

import psycopg2
import pytest
from dotenv import load_dotenv

from tools.search_documents import search_documents

load_dotenv()


@pytest.fixture(scope="module")
def conn():
    db_url = os.environ.get("DB_URL")
    if not db_url:
        pytest.skip("DB_URL not set")
    c = psycopg2.connect(db_url)
    yield c
    c.close()


@pytest.fixture(scope="module")
def voyage_client():
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        pytest.skip("VOYAGE_API_KEY not set")
    import voyageai
    return voyageai.Client(api_key=api_key)


@pytest.fixture(scope="module")
def chunks_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM doc_chunks WHERE embedding IS NOT NULL")
        count = cur.fetchone()[0]
    if count == 0:
        pytest.skip("doc_chunks is empty — run scripts/index_documents.py first")
    return count


def test_search_returns_peb_chunks(conn, voyage_client, chunks_exist):
    rows = search_documents(conn, voyage_client, query="PEB net income Q1 2026")
    assert len(rows) > 0
    for r in rows:
        assert r["text"] is not None and len(r["text"]) > 0
        assert 0.0 <= float(r["score"]) <= 1.0
        assert r["ticker"] is not None
        assert r["form"] is not None
        assert r["filed_date"] is not None


def test_form_filter_10q(conn, voyage_client, chunks_exist):
    rows = search_documents(
        conn, voyage_client,
        query="quarterly financial results",
        forms=["10-Q"],
    )
    assert len(rows) > 0
    for r in rows:
        assert r["form"] == "10-Q", f"Expected 10-Q, got {r['form']!r}"


def test_form_filter_8k(conn, voyage_client, chunks_exist):
    rows = search_documents(
        conn, voyage_client,
        query="RevPAR hotel EBITDAre adjusted FFO",
        forms=["8-K"],
    )
    assert len(rows) > 0
    for r in rows:
        assert r["form"] == "8-K"


def test_k_limits_results(conn, voyage_client, chunks_exist):
    rows = search_documents(conn, voyage_client, query="hotel revenue", k=3)
    assert len(rows) <= 3


def test_scores_descending(conn, voyage_client, chunks_exist):
    rows = search_documents(conn, voyage_client, query="adjusted EBITDAre margin", k=5)
    scores = [float(r["score"]) for r in rows]
    assert scores == sorted(scores, reverse=True), "Results should be ordered by score desc"
