from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import quote

import httpx

from app.core.settings import get_settings
from app.db.database import sqlite_path_from_url
from app.models.paper import Paper, normalize_doi, normalize_external_id


class AuthorDict(TypedDict, total=False):
    name: str
    openalex_id: str | None
    orcid: str | None


class PaperDict(TypedDict, total=False):
    paper_key: str
    doi: str | None
    openalex_id: str | None
    semantic_scholar_id: str | None
    normalized_title: str | None
    title: str
    abstract: str | None
    year: int | None
    authors: list[AuthorDict]
    venue: str | None
    url: str | None
    pdf_url: str | None
    reference_count: int | None
    citation_count: int | None
    fields_of_study: list[str]
    keywords: list[str]
    source_api: str


class NormalizedOpenAlexWork(TypedDict, total=False):
    id: str | None
    openalex_id: str | None
    doi: str | None
    title: str
    abstract: str | None
    authors: list[AuthorDict]
    year: int | None
    venue: str | None
    citation_count: int | None
    referenced_works: list[str]
    cited_by_api_url: str | None
    source: str


class OpenAlexError(RuntimeError):
    """Raised when OpenAlex cannot complete a request after retries."""


class OpenAlexClient:
    """Small async OpenAlex client with retry, rate limiting, and SQLite cache."""

    BASE_URL = "https://api.openalex.org/"
    RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        cache_path: str | Path | None = None,
        max_retries: int | None = None,
        cache_ttl_seconds: int | None = None,
        rate_limit_per_second: float | None = None,
    ) -> None:
        """Create an OpenAlex client using project settings as defaults."""
        settings = get_settings()
        self.contact_email = settings.contact_email
        self.max_retries = max_retries if max_retries is not None else settings.api_max_retries
        self.cache_ttl_seconds = (
            cache_ttl_seconds if cache_ttl_seconds is not None else settings.api_cache_ttl_seconds
        )
        self.min_request_interval = 0.0
        if rate_limit_per_second is None:
            rate_limit_per_second = settings.openalex_rate_limit_per_second
        if rate_limit_per_second > 0:
            self.min_request_interval = 1.0 / rate_limit_per_second

        self._last_request_at = 0.0
        self._rate_limit_lock = asyncio.Lock()
        self._cache_path = self._resolve_cache_path(cache_path)
        self._init_cache()
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )

    async def aclose(self) -> None:
        """Close the underlying httpx connection pool."""
        await self._client.aclose()

    async def resolve_by_doi(self, doi: str) -> PaperDict | None:
        """Fetch one OpenAlex work by DOI and return a normalized Paper dict."""
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        data = await self._get_work_raw_by_doi(normalized)
        return self._to_paper_dict(data) if data else None

    async def get_work_by_openalex_id(self, openalex_id: str) -> PaperDict | None:
        """Fetch one OpenAlex work by Work ID or OpenAlex URL."""
        clean_id = normalize_external_id(openalex_id)
        if not clean_id:
            return None
        data = await self._get_work_raw_by_id(clean_id)
        return self._to_paper_dict(data) if data else None

    async def get_referenced_works(
        self,
        paper: Paper | PaperDict | str,
        *,
        limit: int = 50,
    ) -> list[PaperDict]:
        """Fetch normalized Paper dicts for works referenced by the given paper."""
        work = await self._get_work_raw_for_paper(paper)
        if not work:
            return []

        papers: list[PaperDict] = []
        for referenced_work_id in work.get("referenced_works", [])[:limit]:
            fetched = await self.get_work_by_openalex_id(str(referenced_work_id))
            if fetched:
                papers.append(fetched)
        return papers

    async def get_reference_papers(
        self,
        paper: Paper | PaperDict | str,
        *,
        limit: int = 50,
    ) -> list[PaperDict]:
        """Compatibility alias for crawler code that calls reference papers."""
        return await self.get_referenced_works(paper, limit=limit)

    async def get_citing_works(
        self,
        paper: Paper | PaperDict | str,
        *,
        limit: int = 50,
    ) -> list[PaperDict]:
        """Fetch normalized Paper dicts for works that cite the given paper."""
        clean_id = self._extract_openalex_id(paper)
        if not clean_id:
            return []
        data = await self._request_json(
            "works",
            params={"filter": f"cites:{clean_id}", "per-page": min(limit, 200)},
        )
        return [
            self._to_paper_dict(item)
            for item in data.get("results", [])
            if isinstance(item, dict) and item.get("title")
        ]

    async def get_citing_papers(
        self,
        paper: Paper | PaperDict | str,
        *,
        limit: int = 50,
    ) -> list[PaperDict]:
        """Compatibility alias for crawler code that calls citing papers."""
        return await self.get_citing_works(paper, limit=limit)

    async def search_by_title(self, title: str, *, limit: int = 5) -> list[PaperDict]:
        """Search OpenAlex works by title and return normalized Paper dicts."""
        data = await self._request_json(
            "works",
            params={"search": title, "per-page": min(limit, 25)},
        )
        return [
            self._to_paper_dict(item)
            for item in data.get("results", [])
            if isinstance(item, dict) and item.get("title")
        ]

    async def _get_work_raw_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Fetch raw OpenAlex JSON internally for a DOI lookup."""
        return await self._request_json("works/" + quote(f"https://doi.org/{doi}", safe=""))

    async def _get_work_raw_by_id(self, openalex_id: str) -> dict[str, Any] | None:
        """Fetch raw OpenAlex JSON internally for a Work ID lookup."""
        return await self._request_json(f"works/{normalize_external_id(openalex_id)}")

    async def _get_work_raw_for_paper(self, paper: Paper | PaperDict | str) -> dict[str, Any] | None:
        """Resolve any accepted paper input to the raw OpenAlex work JSON."""
        clean_id = self._extract_openalex_id(paper)
        if clean_id:
            return await self._get_work_raw_by_id(clean_id)

        doi = self._extract_doi(paper)
        if doi:
            return await self._get_work_raw_by_doi(doi)
        return None

    async def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET JSON from OpenAlex with cache lookup and retry/backoff."""
        merged_params = self._params(params)
        cache_key = self._cache_key(path, merged_params)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            response: httpx.Response | None = None
            await self._respect_rate_limit()
            try:
                response = await self._client.get(path, params=merged_params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            else:
                if response.status_code == 404:
                    return {}
                if 200 <= response.status_code < 300:
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise OpenAlexError("OpenAlex returned non-object JSON")
                    self._cache_set(cache_key, payload)
                    return payload
                if response.status_code not in self.RETRYABLE_STATUS_CODES:
                    raise OpenAlexError(self._response_error_message(response))
                last_error = OpenAlexError(self._response_error_message(response))

            if attempt < self.max_retries:
                await asyncio.sleep(self._retry_delay(attempt, response))

        raise OpenAlexError(f"OpenAlex request failed: {last_error}")

    async def _respect_rate_limit(self) -> None:
        """Serialize requests enough to satisfy the configured local rate limit."""
        if self.min_request_interval <= 0:
            return
        async with self._rate_limit_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait_for = self.min_request_interval - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_at = time.monotonic()

    def _to_paper_dict(self, data: dict[str, Any]) -> PaperDict:
        """Map OpenAlex JSON into the project's stable Paper dict shape."""
        normalized = self.normalize_work(data)
        fields = self._field_names(data)
        primary_location = data.get("primary_location") or {}

        paper = Paper(
            doi=normalized.get("doi"),
            openalex_id=normalized.get("openalex_id"),
            title=normalized.get("title") or "Untitled",
            abstract=normalized.get("abstract"),
            year=normalized.get("year"),
            authors=normalized.get("authors") or [],
            venue=normalized.get("venue"),
            url=data.get("id") or normalized.get("cited_by_api_url"),
            pdf_url=primary_location.get("pdf_url"),
            reference_count=data.get("referenced_works_count"),
            citation_count=normalized.get("citation_count"),
            fields_of_study=fields,
            keywords=[],
            source_api="openalex",
        ).with_identity()
        return paper.model_dump(mode="json", exclude={"raw"})  # type: ignore[return-value]

    def normalize_work(self, data: dict[str, Any]) -> NormalizedOpenAlexWork:
        """Normalize an OpenAlex work into a source-specific flat dictionary."""
        primary_location = data.get("primary_location") or {}
        source = primary_location.get("source") or {}
        authors: list[AuthorDict] = [
            {
                "name": authorship.get("author", {}).get("display_name") or "Unknown",
                "openalex_id": normalize_external_id(authorship.get("author", {}).get("id")),
                "orcid": authorship.get("author", {}).get("orcid"),
            }
            for authorship in data.get("authorships", [])
            if isinstance(authorship, dict)
        ]
        openalex_id = normalize_external_id(data.get("id"))
        return {
            "id": openalex_id,
            "openalex_id": openalex_id,
            "doi": normalize_doi(data.get("doi")),
            "title": data.get("title") or data.get("display_name") or "Untitled",
            "abstract": self._abstract_from_inverted_index(data.get("abstract_inverted_index")),
            "authors": authors,
            "year": data.get("publication_year"),
            "venue": source.get("display_name"),
            "citation_count": data.get("cited_by_count"),
            "referenced_works": [str(item) for item in data.get("referenced_works", [])],
            "cited_by_api_url": data.get("cited_by_api_url"),
            "source": "openalex",
        }

    def _field_names(self, data: dict[str, Any]) -> list[str]:
        """Extract topic or concept labels without exposing OpenAlex objects."""
        topics = [
            topic.get("display_name")
            for topic in data.get("topics", [])
            if isinstance(topic, dict) and topic.get("display_name")
        ]
        concepts = [
            concept.get("display_name")
            for concept in data.get("concepts", [])
            if isinstance(concept, dict) and concept.get("display_name")
        ]
        return list(dict.fromkeys([*topics, *concepts]))

    def _abstract_from_inverted_index(self, inverted_index: dict[str, list[int]] | None) -> str | None:
        """Rebuild OpenAlex's inverted-index abstract into readable text."""
        if not inverted_index:
            return None
        positions: list[tuple[int, str]] = []
        for word, indexes in inverted_index.items():
            positions.extend((index, word) for index in indexes)
        return " ".join(word for _, word in sorted(positions)) or None

    def _extract_openalex_id(self, paper: Paper | PaperDict | str) -> str | None:
        """Read an OpenAlex Work ID from a Paper model, Paper dict, or raw ID."""
        if isinstance(paper, Paper):
            return normalize_external_id(paper.openalex_id)
        if isinstance(paper, str):
            return normalize_external_id(paper)
        return normalize_external_id(paper.get("openalex_id"))

    def _extract_doi(self, paper: Paper | PaperDict | str) -> str | None:
        """Read a DOI from a Paper model, Paper dict, or DOI string."""
        if isinstance(paper, Paper):
            return normalize_doi(paper.doi)
        if isinstance(paper, str):
            return normalize_doi(paper)
        return normalize_doi(paper.get("doi"))

    def _params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Add polite-pool contact metadata to every OpenAlex request."""
        merged = dict(params or {})
        if self.contact_email:
            merged["mailto"] = self.contact_email
        return merged

    def _cache_key(self, path: str, params: dict[str, Any]) -> str:
        """Create a stable cache key from request path and query params."""
        payload = json.dumps({"path": path, "params": params}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _resolve_cache_path(self, cache_path: str | Path | None) -> Path | str:
        """Resolve the SQLite cache path from explicit input or app settings."""
        if cache_path is not None:
            return Path(cache_path)
        return sqlite_path_from_url(get_settings().database_url)

    def _init_cache(self) -> None:
        """Create the local cache table if it does not exist."""
        with self._connect_cache() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS openalex_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_body TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_openalex_cache_expires_at ON openalex_cache(expires_at)")

    def _connect_cache(self) -> sqlite3.Connection:
        """Open a SQLite connection for the OpenAlex cache."""
        if isinstance(self._cache_path, Path):
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            return sqlite3.connect(str(self._cache_path))
        return sqlite3.connect(self._cache_path)

    def _cache_get(self, cache_key: str) -> dict[str, Any] | None:
        """Return cached JSON if present and unexpired."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect_cache() as conn:
            row = conn.execute(
                "SELECT response_body, expires_at FROM openalex_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            response_body, expires_at = row
            if expires_at <= now:
                conn.execute("DELETE FROM openalex_cache WHERE cache_key = ?", (cache_key,))
                return None
            payload = json.loads(response_body)
            return payload if isinstance(payload, dict) else None

    def _cache_set(self, cache_key: str, payload: dict[str, Any]) -> None:
        """Store JSON in the SQLite cache with the configured TTL."""
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.cache_ttl_seconds)).isoformat()
        with self._connect_cache() as conn:
            conn.execute(
                """
                INSERT INTO openalex_cache(cache_key, response_body, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response_body = excluded.response_body,
                    expires_at = excluded.expires_at,
                    created_at = CURRENT_TIMESTAMP
                """,
                (cache_key, json.dumps(payload), expires_at),
            )

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        """Compute exponential backoff delay with light jitter."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), 60.0)
        return min(2**attempt, 30.0) + random.uniform(0.0, 0.25)

    def _response_error_message(self, response: httpx.Response) -> str:
        """Build a compact error message from an OpenAlex HTTP response."""
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = response.text
        return f"OpenAlex HTTP {response.status_code}: {str(payload)[:500]}"
