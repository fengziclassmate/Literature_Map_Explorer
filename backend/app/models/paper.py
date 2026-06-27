from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    normalized = re.sub(r"^https?://(dx\.)?doi\.org/", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^doi:\s*", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.strip().strip(".")
    return normalized.lower() or None


def normalize_external_id(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().rstrip("/").split("/")[-1] or None


def normalize_title(value: str | None) -> str | None:
    if not value:
        return None
    text = unicodedata.normalize("NFKD", value).casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


class Author(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    orcid: str | None = None


class Paper(BaseModel):
    model_config = ConfigDict(extra="allow")

    paper_key: str | None = None
    doi: str | None = None
    openalex_id: str | None = None
    semantic_scholar_id: str | None = None
    normalized_title: str | None = None

    title: str
    abstract: str | None = None
    year: int | None = None
    authors: list[Author] = Field(default_factory=list)
    venue: str | None = None
    url: str | None = None
    pdf_url: str | None = None

    reference_count: int | None = None
    citation_count: int | None = None
    fields_of_study: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    source_api: Literal["openalex", "semantic_scholar", "crossref", "merged", "manual"] = "manual"
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("doi")
    @classmethod
    def normalize_doi_field(cls, value: str | None) -> str | None:
        return normalize_doi(value)

    @field_validator("openalex_id", "semantic_scholar_id")
    @classmethod
    def normalize_id_field(cls, value: str | None) -> str | None:
        return normalize_external_id(value)

    @field_validator("normalized_title")
    @classmethod
    def normalize_title_field(cls, value: str | None) -> str | None:
        return normalize_title(value)

    def with_identity(self) -> "Paper":
        paper = self.model_copy()
        paper.normalized_title = paper.normalized_title or normalize_title(paper.title)
        paper.paper_key = canonical_paper_key(paper)
        return paper


def canonical_paper_key(paper: Paper) -> str:
    """Return the dedupe key using DOI > OpenAlex ID > Semantic Scholar ID > normalized title."""
    doi = normalize_doi(paper.doi)
    if doi:
        return f"doi:{doi}"

    openalex_id = normalize_external_id(paper.openalex_id)
    if openalex_id:
        return f"openalex:{openalex_id}"

    semantic_scholar_id = normalize_external_id(paper.semantic_scholar_id)
    if semantic_scholar_id:
        return f"s2:{semantic_scholar_id}"

    title = paper.normalized_title or normalize_title(paper.title)
    if title:
        return f"title:{title}"

    raise ValueError("Paper cannot be keyed without DOI, external ID, or title.")

