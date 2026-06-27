from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.models.paper import Paper
from app.services.crossref_client import CrossrefClient
from app.services.openalex_client import OpenAlexClient
from app.services.semantic_scholar_client import SemanticScholarClient

logger = logging.getLogger(__name__)


class MetadataEnricher:
    """Fill missing paper metadata from OpenAlex, Semantic Scholar, and Crossref."""

    def __init__(
        self,
        *,
        openalex: OpenAlexClient | None = None,
        semantic_scholar: SemanticScholarClient | None = None,
        crossref: CrossrefClient | None = None,
        owns_clients: bool = False,
    ) -> None:
        self.openalex = openalex or OpenAlexClient()
        self.semantic_scholar = semantic_scholar or SemanticScholarClient()
        self.crossref = crossref or CrossrefClient()
        self.owns_clients = owns_clients or not (openalex or semantic_scholar or crossref)

    async def aclose(self) -> None:
        if not self.owns_clients:
            return
        await asyncio.gather(
            self.openalex.aclose(),
            self.semantic_scholar.aclose(),
            self.crossref.aclose(),
            return_exceptions=True,
        )

    async def enrich(self, paper: Paper) -> Paper:
        candidates = [paper.with_identity()]
        sources: list[str] = [paper.source_api]

        for source_name, fetcher in self._fetchers(paper):
            try:
                fetched = await fetcher()
            except Exception as exc:
                logger.warning("Metadata enrichment failed via %s for %s: %s", source_name, paper.paper_key, exc)
                continue
            if not fetched:
                continue
            if isinstance(fetched, list):
                items = fetched[:1]
            else:
                items = [fetched]
            for item in items:
                try:
                    candidates.append(self._coerce_paper(item))
                    sources.append(source_name)
                except Exception as exc:
                    logger.warning("Invalid enrichment payload via %s: %s", source_name, exc)

        enriched = self._merge(candidates)
        raw = dict(enriched.raw)
        raw["metadata_sources"] = sorted(set(source for source in sources if source))
        enriched.raw = raw
        enriched.source_api = "merged" if len(set(sources)) > 1 else enriched.source_api
        return enriched.with_identity()

    def _fetchers(self, paper: Paper):
        if paper.doi:
            yield "openalex", lambda: self.openalex.resolve_by_doi(paper.doi or "")
            yield "semantic_scholar", lambda: self.semantic_scholar.resolve_by_doi(paper.doi or "")
            yield "crossref", lambda: self.crossref.resolve_by_doi(paper.doi or "")
        if paper.openalex_id:
            yield "openalex", lambda: self.openalex.get_work_by_openalex_id(paper.openalex_id or "")
        if paper.semantic_scholar_id:
            yield "semantic_scholar", lambda: self.semantic_scholar.get_paper(paper.semantic_scholar_id or "")
        if not paper.doi and paper.title:
            yield "openalex", lambda: self.openalex.search_by_title(paper.title, limit=1)
            yield "crossref", lambda: self.crossref.search_by_title(paper.title, limit=1)

    def _coerce_paper(self, value: Paper | dict[str, Any]) -> Paper:
        if isinstance(value, Paper):
            return value.with_identity()
        return Paper(**value).with_identity()

    def _merge(self, candidates: list[Paper]) -> Paper:
        base = candidates[0].model_copy(deep=True)
        raw = dict(base.raw)
        for candidate in candidates[1:]:
            base.doi = base.doi or candidate.doi
            base.openalex_id = base.openalex_id or candidate.openalex_id
            base.semantic_scholar_id = base.semantic_scholar_id or candidate.semantic_scholar_id
            base.abstract = base.abstract or candidate.abstract
            base.venue = base.venue or candidate.venue
            base.year = base.year or candidate.year
            base.url = base.url or candidate.url
            base.pdf_url = base.pdf_url or candidate.pdf_url
            base.reference_count = base.reference_count or candidate.reference_count
            if not base.authors and candidate.authors:
                base.authors = candidate.authors
            base.fields_of_study = list(dict.fromkeys(base.fields_of_study + candidate.fields_of_study))
            base.keywords = list(dict.fromkeys(base.keywords + candidate.keywords))
            base.citation_count = max(
                [count for count in [base.citation_count, candidate.citation_count] if count is not None],
                default=None,
            )
            raw.update(candidate.raw)
        base.raw = raw
        return base.with_identity()

