from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from app.models.paper import Paper, canonical_paper_key, normalize_title
from app.services.crossref_client import CrossrefClient
from app.services.http_client import ExternalApiError
from app.services.openalex_client import OpenAlexClient
from app.services.semantic_scholar_client import SemanticScholarClient


class PaperResolutionError(RuntimeError):
    pass


class PaperResolver:
    """Resolve and merge metadata with DOI > OpenAlex ID > S2 ID > normalized title dedupe."""

    def __init__(
        self,
        openalex: OpenAlexClient | None = None,
        semantic_scholar: SemanticScholarClient | None = None,
        crossref: CrossrefClient | None = None,
    ) -> None:
        self.openalex = openalex or OpenAlexClient()
        self.semantic_scholar = semantic_scholar or SemanticScholarClient()
        self.crossref = crossref or CrossrefClient()

    async def aclose(self) -> None:
        await asyncio.gather(
            self.openalex.aclose(),
            self.semantic_scholar.aclose(),
            self.crossref.aclose(),
            return_exceptions=True,
        )

    async def resolve_doi(self, doi: str) -> Paper:
        results = await asyncio.gather(
            self.openalex.resolve_by_doi(doi),
            self.semantic_scholar.resolve_by_doi(doi),
            self.crossref.resolve_by_doi(doi),
            return_exceptions=True,
        )
        papers: list[Paper] = []
        errors: list[str] = []
        for result in results:
            if isinstance(result, Paper):
                papers.append(result)
            elif isinstance(result, dict):
                papers.append(self.coerce_paper(result))
            elif isinstance(result, ExternalApiError):
                errors.append(str(result))
            elif isinstance(result, Exception):
                errors.append(str(result))

        if not papers:
            detail = "; ".join(errors) if errors else "No metadata found"
            raise PaperResolutionError(f"Could not resolve DOI {doi}: {detail}")
        return self.merge_papers(papers)

    async def enrich_if_possible(self, paper: Paper) -> Paper:
        if paper.doi:
            try:
                return await self.resolve_doi(paper.doi)
            except PaperResolutionError:
                return paper.with_identity()
        return paper.with_identity()

    def coerce_paper(self, value: Paper | dict[str, Any]) -> Paper:
        """Convert API-specific Paper dicts into the internal Paper model."""
        if isinstance(value, Paper):
            return value.with_identity()
        return Paper(**value).with_identity()

    def merge_papers(self, papers: Iterable[Paper]) -> Paper:
        ordered = sorted(
            [paper.with_identity() for paper in papers],
            key=lambda paper: {"openalex": 0, "semantic_scholar": 1, "crossref": 2}.get(paper.source_api, 9),
        )
        if not ordered:
            raise PaperResolutionError("Cannot merge an empty paper list")

        base = ordered[0].model_copy(deep=True)
        raw = dict(base.raw)
        fields = list(base.fields_of_study)
        keywords = list(base.keywords)

        for paper in ordered[1:]:
            base.doi = base.doi or paper.doi
            base.openalex_id = base.openalex_id or paper.openalex_id
            base.semantic_scholar_id = base.semantic_scholar_id or paper.semantic_scholar_id
            base.abstract = base.abstract or paper.abstract
            base.year = base.year or paper.year
            base.venue = base.venue or paper.venue
            base.url = base.url or paper.url
            base.pdf_url = base.pdf_url or paper.pdf_url
            base.reference_count = base.reference_count or paper.reference_count
            base.citation_count = max(
                [count for count in [base.citation_count, paper.citation_count] if count is not None],
                default=None,
            )
            if not base.authors and paper.authors:
                base.authors = paper.authors
            raw.update(paper.raw)
            fields.extend(paper.fields_of_study)
            keywords.extend(paper.keywords)

        base.fields_of_study = list(dict.fromkeys(fields))
        base.keywords = list(dict.fromkeys(keywords))
        base.normalized_title = normalize_title(base.title)
        base.raw = raw
        base.source_api = "merged"
        base.paper_key = canonical_paper_key(base)
        return base
