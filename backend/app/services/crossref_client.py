from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.core.settings import get_settings
from app.models.paper import Author, Paper, normalize_doi
from app.services.http_client import CachingHttpClient


class CrossrefClient:
    BASE_URL = "https://api.crossref.org/"

    def __init__(self) -> None:
        settings = get_settings()
        self.http = CachingHttpClient(
            service="crossref",
            base_url=self.BASE_URL,
            rate_limit_per_second=settings.crossref_rate_limit_per_second,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def resolve_by_doi(self, doi: str) -> Paper | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        data = await self.http.get_json("works/" + quote(normalized, safe=""))
        message = data.get("message") or {}
        return self._to_paper(message) if message else None

    async def search_by_title(self, title: str, *, limit: int = 5) -> list[Paper]:
        data = await self.http.get_json(
            "works",
            params={"query.title": title, "rows": min(limit, 20)},
        )
        items = (data.get("message") or {}).get("items", [])
        return [self._to_paper(item) for item in items if item.get("title")]

    async def get_reference_papers(self, paper: Paper, *, limit: int = 50) -> list[Paper]:
        data = self._crossref_raw(paper)
        if data is None and paper.doi:
            fetched = await self.resolve_by_doi(paper.doi)
            data = self._crossref_raw(fetched) if fetched else None
        if not data:
            return []

        papers: list[Paper] = []
        for ref in data.get("reference", [])[:limit]:
            ref_doi = normalize_doi(ref.get("DOI") or ref.get("doi"))
            title = ref.get("article-title") or ref.get("series-title") or ref.get("volume-title")
            if not ref_doi and not title:
                continue
            if ref_doi:
                try:
                    resolved = await self.resolve_by_doi(ref_doi)
                except Exception:
                    resolved = None
                if resolved:
                    papers.append(resolved)
                    continue
            papers.append(
                Paper(
                    doi=ref_doi,
                    title=title or ref_doi or "Untitled reference",
                    year=self._year_from_reference(ref),
                    source_api="crossref",
                    raw={"crossref_reference": ref},
                ).with_identity()
            )
        return papers

    def _to_paper(self, data: dict[str, Any]) -> Paper:
        authors = [
            Author(name=" ".join(part for part in [author.get("given"), author.get("family")] if part))
            for author in data.get("author", [])
        ]
        return Paper(
            doi=normalize_doi(data.get("DOI")),
            title=self._first(data.get("title")) or "Untitled",
            abstract=data.get("abstract"),
            year=self._year_from_date_parts(data.get("published-print") or data.get("published-online") or data.get("issued")),
            authors=authors,
            venue=self._first(data.get("container-title")),
            url=data.get("URL"),
            reference_count=data.get("reference-count"),
            citation_count=data.get("is-referenced-by-count"),
            source_api="crossref",
            raw={"crossref": data},
        ).with_identity()

    def _crossref_raw(self, paper: Paper | None) -> dict[str, Any] | None:
        if not paper:
            return None
        value = paper.raw.get("crossref")
        return value if isinstance(value, dict) else None

    def _first(self, value: list[str] | None) -> str | None:
        return value[0] if value else None

    def _year_from_date_parts(self, value: dict[str, Any] | None) -> int | None:
        if not value:
            return None
        parts = value.get("date-parts") or []
        if parts and parts[0]:
            return parts[0][0]
        return None

    def _year_from_reference(self, ref: dict[str, Any]) -> int | None:
        raw_year = ref.get("year")
        try:
            return int(raw_year) if raw_year else None
        except (TypeError, ValueError):
            return None

