from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.core.settings import get_settings
from app.models.paper import Paper, canonical_paper_key, normalize_doi
from app.services.http_client import CachingHttpClient
from app.services.openalex_client import OpenAlexClient
from app.services.semantic_scholar_client import SemanticScholarClient

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 80 * 1024 * 1024


@dataclass(frozen=True)
class PdfCandidate:
    source: str
    url: str


@dataclass(frozen=True)
class PdfFetchResult:
    paper_id: str
    status: str
    source: str | None = None
    pdf_url: str | None = None
    pdf_path: str | None = None
    file_size_bytes: int | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SafePdfFetcher:
    """Fetch open-access PDFs only.

    This intentionally mirrors the safe subset of scansci-pdf's source strategy:
    project metadata, OpenAlex OA, Semantic Scholar OA, and Unpaywall. It does
    not call Sci-Hub, LibGen, Tor, or automate restricted institutional login.
    """

    def __init__(
        self,
        *,
        openalex: OpenAlexClient | None = None,
        semantic_scholar: SemanticScholarClient | None = None,
    ) -> None:
        settings = get_settings()
        self.settings = settings
        self.openalex = openalex or OpenAlexClient()
        self.semantic_scholar = semantic_scholar or SemanticScholarClient()
        self.unpaywall = CachingHttpClient(
            service="unpaywall",
            base_url="https://api.unpaywall.org/v2/",
            rate_limit_per_second=1.0,
        )
        self.download_dir = Path(settings.pdf_download_dir)

    async def aclose(self) -> None:
        await self.openalex.aclose()
        await self.semantic_scholar.aclose()
        await self.unpaywall.aclose()

    async def fetch_for_paper(self, paper: Paper) -> PdfFetchResult:
        paper = paper.with_identity()
        paper_id = paper.paper_key or canonical_paper_key(paper)
        candidates = await self._collect_candidates(paper)
        if not candidates:
            return PdfFetchResult(
                paper_id=paper_id,
                status="not_found",
                error_message="No open-access PDF candidate found.",
            )

        errors: list[str] = []
        for candidate in candidates:
            try:
                path = await self._download_candidate(paper_id, candidate)
            except Exception as exc:
                errors.append(f"{candidate.source}: {exc}")
                logger.info("PDF candidate failed", extra={"source": candidate.source, "url": candidate.url})
                continue

            return PdfFetchResult(
                paper_id=paper_id,
                status="downloaded",
                source=candidate.source,
                pdf_url=candidate.url,
                pdf_path=str(path),
                file_size_bytes=path.stat().st_size,
            )

        return PdfFetchResult(
            paper_id=paper_id,
            status="not_found",
            error_message="; ".join(errors[-3:]) or "Open-access PDF candidates did not return valid PDFs.",
        )

    async def _collect_candidates(self, paper: Paper) -> list[PdfCandidate]:
        candidates: list[PdfCandidate] = []
        if paper.pdf_url:
            candidates.append(PdfCandidate("metadata", paper.pdf_url))

        doi = normalize_doi(paper.doi)
        if doi:
            candidates.extend(await self._openalex_candidates(doi))
            candidates.extend(await self._semantic_scholar_candidates(doi))
            candidates.extend(await self._unpaywall_candidates(doi))

        seen: set[str] = set()
        unique: list[PdfCandidate] = []
        for candidate in candidates:
            url = candidate.url.strip()
            if not url or not url.startswith(("http://", "https://")):
                continue
            normalized = url.rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(PdfCandidate(candidate.source, url))
        return unique

    async def _openalex_candidates(self, doi: str) -> list[PdfCandidate]:
        try:
            paper = await self.openalex.resolve_by_doi(doi)
        except Exception:
            logger.debug("OpenAlex PDF lookup failed", exc_info=True)
            return []
        url = (paper or {}).get("pdf_url")
        return [PdfCandidate("openalex_oa", url)] if url else []

    async def _semantic_scholar_candidates(self, doi: str) -> list[PdfCandidate]:
        try:
            paper = await self.semantic_scholar.resolve_by_doi(doi)
        except Exception:
            logger.debug("Semantic Scholar PDF lookup failed", exc_info=True)
            return []
        return [PdfCandidate("semantic_scholar_oa", paper.pdf_url)] if paper and paper.pdf_url else []

    async def _unpaywall_candidates(self, doi: str) -> list[PdfCandidate]:
        try:
            payload = await self.unpaywall.get_json(
                quote(doi, safe=""),
                params={"email": self.settings.unpaywall_email},
            )
        except Exception:
            logger.debug("Unpaywall PDF lookup failed", exc_info=True)
            return []

        urls: list[str] = []
        best = payload.get("best_oa_location") if isinstance(payload, dict) else None
        if isinstance(best, dict) and best.get("url_for_pdf"):
            urls.append(best["url_for_pdf"])
        for location in payload.get("oa_locations", []) if isinstance(payload, dict) else []:
            if isinstance(location, dict) and location.get("url_for_pdf"):
                urls.append(location["url_for_pdf"])
        return [PdfCandidate("unpaywall", url) for url in urls]

    async def _download_candidate(self, paper_id: str, candidate: PdfCandidate) -> Path:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        target = self.download_dir / f"{self._safe_filename(paper_id)}.pdf"
        if target.exists() and self._looks_like_pdf(target):
            return target

        tmp_path = target.with_suffix(".pdf.part")
        settings = get_settings()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=max(settings.request_timeout_seconds, 45.0),
                headers={"User-Agent": settings.user_agent},
            ) as client:
                async with client.stream("GET", candidate.url) as response:
                    if response.status_code >= 400:
                        raise RuntimeError(f"HTTP {response.status_code}")
                    content_type = response.headers.get("content-type", "").lower()
                    first_chunk = b""
                    total = 0
                    with tmp_path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            if not chunk:
                                continue
                            if not first_chunk:
                                first_chunk = chunk
                                if not (first_chunk.startswith(b"%PDF-") or "application/pdf" in content_type):
                                    raise RuntimeError("response is not a PDF")
                            total += len(chunk)
                            if total > MAX_PDF_BYTES:
                                raise RuntimeError("PDF exceeds size limit")
                            handle.write(chunk)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        if not self._looks_like_pdf(tmp_path):
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError("downloaded file failed PDF validation")
        tmp_path.replace(target)
        return target

    def _looks_like_pdf(self, path: Path) -> bool:
        try:
            if path.stat().st_size < 1000:
                return False
            with path.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False

    def _safe_filename(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "paper"
