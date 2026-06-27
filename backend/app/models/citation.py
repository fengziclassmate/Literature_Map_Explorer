from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CitationEdge(BaseModel):
    """Directed edge where source cites target."""

    model_config = ConfigDict(extra="allow")

    source_key: str
    target_key: str
    relation: Literal["cites"] = "cites"
    discovered_via: Literal["openalex", "semantic_scholar", "crossref", "merged", "manual"] = "manual"
    raw: dict[str, Any] = Field(default_factory=dict)

