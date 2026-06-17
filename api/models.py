from __future__ import annotations

from pydantic import BaseModel


class PriorTurn(BaseModel):
    question: str
    answer: str


class AskRequest(BaseModel):
    question: str
    history: list[PriorTurn] = []


class ToolCallEntry(BaseModel):
    tool: str
    inputs: dict
    rows_returned: int
    elapsed_ms: int


class Source(BaseModel):
    ticker:     str | None = None
    form:       str | None = None
    accession:  str | None = None
    filed_date: str | None = None
    url:        str | None = None


class AskResponse(BaseModel):
    answer: str
    tool_calls: list[ToolCallEntry]
    sources: list[Source]
    follow_ups: list[str] = []
