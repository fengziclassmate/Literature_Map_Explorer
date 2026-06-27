from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db.database import get_paper_pdf, get_paper_summary, load_paper, save_paper_pdf, save_paper_summary
from app.models.paper import canonical_paper_key
from app.services.pdf_fetcher import SafePdfFetcher
from app.services.summarizer import PaperSummarizer

router = APIRouter(prefix="/papers", tags=["papers"])


class PaperSummarizeRequest(BaseModel):
    seed_paper_id: str | None = None


@router.get("/{paper_id:path}/pdf")
def get_pdf_status(paper_id: str) -> dict:
    paper = load_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    paper_id = paper.paper_key or canonical_paper_key(paper)
    return get_paper_pdf(paper_id) or {"paper_id": paper_id, "status": "not_requested"}


@router.post("/{paper_id:path}/pdf/download")
async def download_open_pdf(paper_id: str) -> dict:
    paper = load_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    fetcher = SafePdfFetcher()
    try:
        result = await fetcher.fetch_for_paper(paper)
    finally:
        await fetcher.aclose()
    return save_paper_pdf(**result.to_dict())


@router.get("/{paper_id:path}/pdf/file")
def get_pdf_file(paper_id: str) -> FileResponse:
    paper = load_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    paper_key = paper.paper_key or canonical_paper_key(paper)
    status = get_paper_pdf(paper_key)
    if not status or status.get("status") != "downloaded" or not status.get("pdf_path"):
        raise HTTPException(status_code=404, detail="PDF file not downloaded")

    path = Path(status["pdf_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF file missing on disk")
    filename = f"{paper.title[:80] or paper_key}.pdf"
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get("/{paper_id:path}/summary")
def get_summary(paper_id: str) -> dict:
    paper = load_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    summary = get_paper_summary(paper.paper_key or paper_id)
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
