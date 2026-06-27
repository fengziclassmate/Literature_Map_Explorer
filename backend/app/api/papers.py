from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.database import get_paper_summary, load_paper, save_paper_summary
from app.services.summarizer import PaperSummarizer

router = APIRouter(prefix="/papers", tags=["papers"])


class PaperSummarizeRequest(BaseModel):
    seed_paper_id: str | None = None


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

