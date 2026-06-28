from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.settings import get_settings
from app.db.database import get_paper_pdf, get_paper_summary, load_paper, save_paper_pdf, save_paper_summary
from app.models.paper import Paper, canonical_paper_key
from app.services.pdf_fetcher import MAX_PDF_BYTES, SafePdfFetcher
from app.services.summarizer import PaperSummarizer

router = APIRouter(prefix="/papers", tags=["papers"])


class PaperSummarizeRequest(BaseModel):
    seed_paper_id: str | None = None


def _load_paper_or_404(paper_id: str) -> tuple[Paper, str]:
    paper = load_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    paper = paper.with_identity()
    paper_key = paper.paper_key or canonical_paper_key(paper)
    return paper, paper_key


def _safe_pdf_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "paper"


def _append_query_param(url: str, key: str, value: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{key}={quote(value, safe='')}"


def _templated_or_appended_url(template: str, *, url: str | None = None, doi: str | None = None) -> str | None:
    if not template:
        return None
    if url and "{url}" in template:
        return template.replace("{url}", quote(url, safe=""))
    if url and "{url_raw}" in template:
        return template.replace("{url_raw}", url)
    if doi and "{doi}" in template:
        return template.replace("{doi}", quote(doi, safe=""))
    if doi and "{doi_raw}" in template:
        return template.replace("{doi_raw}", doi)
    if url:
        return f"{template}{quote(url, safe='')}"
    if doi:
        return _append_query_param(template, "doi", doi)
    return template


@router.get("/{paper_id:path}/pdf")
def get_pdf_status(paper_id: str) -> dict:
    _, paper_key = _load_paper_or_404(paper_id)
    return get_paper_pdf(paper_key) or {"paper_id": paper_key, "status": "not_requested"}


@router.post("/{paper_id:path}/pdf/download")
async def download_open_pdf(paper_id: str) -> dict:
    paper, _ = _load_paper_or_404(paper_id)

    fetcher = SafePdfFetcher()
    try:
        result = await fetcher.fetch_for_paper(paper)
    finally:
        await fetcher.aclose()
    return save_paper_pdf(**result.to_dict())


@router.get("/{paper_id:path}/pdf/file")
def get_pdf_file(paper_id: str) -> FileResponse:
    paper, paper_key = _load_paper_or_404(paper_id)
    status = get_paper_pdf(paper_key)
    if not status or status.get("status") != "downloaded" or not status.get("pdf_path"):
        raise HTTPException(status_code=404, detail="PDF file not downloaded")

    path = Path(status["pdf_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF file missing on disk")
    filename = f"{_safe_pdf_filename(paper.title[:80] or paper_key)}.pdf"
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get("/{paper_id:path}/access-links")
def get_access_links(paper_id: str) -> dict:
    """Return user-facing, lawful access options for a paper.

    The API deliberately returns links that require normal user authorization
    instead of automating restricted access or bypassing paywalls.
    """

    paper, paper_key = _load_paper_or_404(paper_id)
    settings = get_settings()
    institution = settings.institution_name or "机构"
    doi_url = f"https://doi.org/{paper.doi}" if paper.doi else None
    publisher_url = paper.url or doi_url

    links: list[dict[str, str]] = []
    if doi_url:
        links.append({"kind": "doi", "label": "打开 DOI 页面", "url": doi_url})
    if publisher_url and publisher_url != doi_url:
        links.append({"kind": "source", "label": "打开来源页面", "url": publisher_url})

    resolver_url = _templated_or_appended_url(settings.library_resolver_url, doi=paper.doi)
    if resolver_url:
        links.append({"kind": "library_resolver", "label": f"{institution} 图书馆解析", "url": resolver_url})

    ezproxy_url = _templated_or_appended_url(settings.ezproxy_url_prefix, url=publisher_url)
    if ezproxy_url:
        links.append({"kind": "ezproxy", "label": f"{institution} EZProxy 访问", "url": ezproxy_url})

    if settings.carsi_login_url:
        links.append({"kind": "carsi", "label": f"{institution} CARSI 登录", "url": settings.carsi_login_url})
    if settings.webvpn_url:
        links.append({"kind": "webvpn", "label": f"{institution} WebVPN 登录", "url": settings.webvpn_url})
    if settings.institution_login_url:
        links.append({"kind": "institution_login", "label": f"{institution} 浏览器登录", "url": settings.institution_login_url})

    return {
        "paper_id": paper_key,
        "links": links,
        "notes": [
            "仅提供开放获取和机构授权访问入口；不会接入 Sci-Hub、LibGen、Tor 或绕过访问限制的下载方式。",
            "通过学校、单位或图书馆合法获取 PDF 后，可以在 Paper Card 中上传到本地项目。",
        ],
    }


@router.post("/{paper_id:path}/pdf/upload")
async def upload_pdf(paper_id: str, request: Request) -> dict:
    """Store a PDF that the user has already obtained through authorized access."""

    _, paper_key = _load_paper_or_404(paper_id)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="Only application/pdf uploads are accepted")

    target_dir = Path(get_settings().pdf_download_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_safe_pdf_filename(paper_key)}.pdf"
    tmp_path = target.with_suffix(".pdf.uploading")

    total = 0
    try:
        with tmp_path.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise HTTPException(status_code=413, detail="PDF exceeds size limit")
                handle.write(chunk)

        if total == 0:
            raise HTTPException(status_code=400, detail="Uploaded PDF is empty")
        with tmp_path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF")

        tmp_path.replace(target)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded PDF: {exc}") from exc

    return save_paper_pdf(
        paper_id=paper_key,
        status="downloaded",
        source="user_upload",
        pdf_path=str(target),
        file_size_bytes=target.stat().st_size,
    )


@router.get("/{paper_id:path}/summary")
def get_summary(paper_id: str) -> dict:
    _, paper_key = _load_paper_or_404(paper_id)
    summary = get_paper_summary(paper_key)
    if not summary:
        raise HTTPException(status_code=404, detail="Paper summary not found")
    return summary.card_fields()


@router.post("/{paper_id:path}/summarize")
async def summarize_paper(paper_id: str, payload: PaperSummarizeRequest | None = None) -> dict:
    target = load_paper(paper_id)
    if not target:
        raise HTTPException(status_code=404, detail="Paper not found")
    seed = target
    if payload and payload.seed_paper_id:
        seed = load_paper(payload.seed_paper_id) or target

    summary = await PaperSummarizer().summarize_paper(seed, target)
    save_paper_summary(summary)
    return summary.card_fields()
