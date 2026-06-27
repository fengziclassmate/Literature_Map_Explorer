from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PaperSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    paper_id: str
    one_sentence_summary: str
    research_background: str | None = None
    research_problem: str | None = None
    objectives: str | None = None
    data_sources: str | None = None
    methods: str | None = None
    key_findings: str | None = None
    contributions: str | None = None
    limitations: str | None = None
    future_work: str | None = None
    relation_to_seed: str | None = None
    relevance_score: float | None = None
    summary_confidence: float | None = None
    summary_level: Literal["metadata", "abstract"] = "metadata"
    raw_llm_output: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def card_fields(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

