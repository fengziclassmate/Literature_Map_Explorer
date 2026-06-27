from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.core.settings import get_settings
from app.models.paper import Author, Paper, normalize_doi
from app.services.http_client import CachingHttpClient


class SemanticScholarClient:
    BASE_URL = "https://api.semanticscholar.org/graph/v1/"
    FIELDS = ",".join(
        [
            "paperId",
            "externalIds",
            "url",
            "title",
            "abstract",
            "year",
            "authors",
            "venue",
            "publicationVenue",
            "referenceCount",
            "citationCount",
            "fieldsOfStudy",
            "s2FieldsOfStudy",
            "openAccessPdf",
        ]
    )

    def __init__(self) -> None:
        settings = get_settings()
        headers = {}
        if settings.semantic_scholar_api_key:
            headers["x-api-key"] = settings.semantic_scholar_api_key
        self.http = CachingHttpClient(
            service="semantic_scholar",
            base_url=self.BASE_URL,
            rate_limit_per_second=settings.semantic_scholar_rate_limit_per_second,
            headers=headers,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def resolve_by_doi(self, doi: str) -> Paper | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        data = await self.http.get_json(
            "paper/" + quote(f"DOI:{normalized}", safe=":"),
            params={"fields": self.FIELDS},
        )
        return self._to_paper(data) if data and data.get("title") else None

    async def get_paper(self, paper_id: str) -> Paper | None:
        data = await self.http.get_json(
            "paper/" + quote(paper_id, safe=":"),
            params={"fields": self.FIELDS},
        )
        return self._to_paper(data) if data and data.get("title") else None

    async def get_reference_papers(self, paper: Paper, *, limit: int = 50) -> list[Paper]:
        if not paper.semantic_scholar_id:
            return []
        data = await self.http.get_json(
            "paper/" + quote(paper.semantic_scholar_id, safe=":") + "/references",
            params={"fields": self.FIELDS, "limit": min(limit, 1000)},
        )
        return [
            self._to_paper(item["citedPaper"])
            for item in data.get("data", [])
            if item.get("citedPaper", {}).get("title")
        ]

    async def get_citing_papers(self, paper: Paper, *, limit: int = 50) -> list[Paper]:
        if not paper.semantic_scholar_id:
            return []
        data = await self.http.get_json(
            "paper/" + quote(paper.semantic_scholar_id, safe=":") + "/citations",
            params={"fields": self.FIELDS, "limit": min(limit, 1000)},
        )
        return [
            self._to_paper(item["citingPaper"])
            for item in data.get("data", [])
            if item.get("citingPaper", {}).get("title")
        ]

    def _to_paper(self, data: dict[str, Any]) -> Paper:
        external_ids = data.get("externalIds") or {}
        authors = [
            Author(name=author.get("name") or "Unknown", semantic_scholar_id=author.get("authorId"))
            for author in data.get("authors", [])
        ]
        s2_fields = [
            field.get("category")
            for field in data.get("s2FieldsOfStudy", [])
            if field.get("category")
        ]
        venue = data.get("venue") or (data.get("publicationVenue") or {}).get("name")
        pdf = data.get("openAccessPdf") or {}
        return Paper(
            doi=normalize_doi(external_ids.get("DOI")),
            semantic_scholar_id=data.get("paperId"),
            title=data.get("title") or "Untitled",
            abstract=data.get("abstract"),
            year=data.get("year"),
            authors=authors,
            venue=venue,
            url=data.get("url"),
            pdf_url=pdf.get("url"),
            reference_count=data.get("referenceCount"),
            citation_count=data.get("citationCount"),
            fields_of_study=list(dict.fromkeys((data.get("fieldsOfStudy") or []) + s2_fields)),
            source_api="semantic_scholar",
            raw={"semantic_scholar": data},
        ).with_identity()

