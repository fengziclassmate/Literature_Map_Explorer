from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.db.database import (
    create_crawl_run,
    create_project,
    get_paper_summary,
    get_latest_crawl_run,
    get_project_report,
    get_project,
    list_project_paper_cards,
    list_projects,
    load_project_graph,
    load_project_seed_paper,
    save_crawl_result,
    save_paper_summary,
    save_project_report,
    update_crawl_run,
    update_project_status,
    write_project_report_file,
)
from app.models.paper import normalize_doi
from app.services.crawler import CitationCrawler
from app.services.graph_analyzer import GraphAnalyzer
from app.services.paper_resolver import PaperResolutionError
from app.services.report_generator import ReportGenerator
from app.services.summarizer import PaperSummarizer

router = APIRouter(prefix="/projects", tags=["projects"])
logger = logging.getLogger(__name__)


class ProjectCreateRequest(BaseModel):
    name: str | None = None
    seed_doi: str
    max_depth_backward: int = Field(default=1, ge=0, le=5)
    max_depth_forward: int = Field(default=0, ge=0, le=5)
    max_papers_total: int = Field(default=100, ge=1, le=5000)
    per_paper_limit: int = Field(default=50, ge=1, le=200)

    @field_validator("seed_doi")
    @classmethod
    def validate_seed_doi(cls, value: str) -> str:
        normalized = normalize_doi(value)
        if not normalized:
            raise ValueError("seed_doi must be a valid DOI")
        return normalized


class ProjectSummarizeRequest(BaseModel):
    force: bool = False


def _project_payload(project: dict) -> dict:
    payload = dict(project)
    payload["settings"] = json.loads(payload.pop("settings_json", "{}") or "{}")
    return payload


def _initial_progress(payload: ProjectCreateRequest, *, stage: str = "queued") -> dict:
    return {
        "current_stage": stage,
        "current_paper_id": None,
        "current_paper_title": None,
        "discovered_papers_count": 0,
        "processed_papers_count": 0,
        "queued_papers_count": 0,
        "max_papers_total": payload.max_papers_total,
        "progress_percent": 0,
        "new_papers_count": 0,
        "new_edges_count": 0,
        "failed_requests_count": 0,
        "skipped_papers_count": 0,
        "summarized_count": 0,
        "summary_failed_count": 0,
        "visited_papers_count": 0,
        "truncated": False,
        "errors": [],
    }


async def _run_project_crawl(project_id: str, run_id: str, settings: dict) -> None:
    payload = ProjectCreateRequest(**settings)
    crawler = CitationCrawler()

    def record_progress(summary) -> None:
        update_crawl_run(run_id, status="running", stats=summary.to_dict())

    try:
        update_crawl_run(run_id, status="running", stats=_initial_progress(payload, stage="resolving_doi"))
        result = await crawler.crawl_from_doi(
            payload.seed_doi,
            max_depth_backward=payload.max_depth_backward,
            max_depth_forward=payload.max_depth_forward,
            max_papers_total=payload.max_papers_total,
            per_paper_limit=payload.per_paper_limit,
            project_id=project_id,
            progress_callback=record_progress,
        )
        save_crawl_result(project_id=project_id, papers=result.papers, citations=result.citations)
        update_project_status(
            project_id,
            status="complete",
            paper_count=len(result.papers),
            edge_count=len(result.citations),
        )
        final_stats = result.summary.to_dict() if result.summary else _initial_progress(payload, stage="complete")
        final_stats["current_stage"] = "complete"
        final_stats["progress_percent"] = 100
        update_crawl_run(run_id, status="complete", stats=final_stats, finished=True)
    except PaperResolutionError as exc:
        stats = _initial_progress(payload, stage="failed")
        stats["errors"] = [str(exc)]
        update_project_status(project_id, status="failed", error_message=str(exc))
        update_crawl_run(run_id, status="failed", stats=stats, error_message=str(exc), finished=True)
    except Exception as exc:
        logger.exception("Project crawl failed", extra={"project_id": project_id, "run_id": run_id})
        stats = _initial_progress(payload, stage="failed")
        stats["errors"] = [str(exc)]
        update_project_status(project_id, status="failed", error_message=str(exc))
        update_crawl_run(run_id, status="failed", stats=stats, error_message=str(exc), finished=True)
    finally:
        await crawler.aclose()


@router.post("/async")
async def start_project_from_doi(payload: ProjectCreateRequest, background_tasks: BackgroundTasks) -> dict:
    settings = payload.model_dump()
    name = payload.name or f"DOI {payload.seed_doi}"
    project_id = create_project(name=name, seed_doi=payload.seed_doi, settings=settings, status="running")
    run_id = create_crawl_run(project_id, status="queued", stats=_initial_progress(payload))
    background_tasks.add_task(_run_project_crawl, project_id, run_id, settings)
    return {
        "project_id": project_id,
        "run_id": run_id,
        "status": "running",
        "progress_url": f"/projects/{project_id}/status",
        "crawl_run": get_latest_crawl_run(project_id),
    }


@router.post("")
async def create_project_from_doi(payload: ProjectCreateRequest) -> dict:
    settings = payload.model_dump()
    name = payload.name or f"DOI {payload.seed_doi}"
    project_id = create_project(name=name, seed_doi=payload.seed_doi, settings=settings, status="running")

    crawler = CitationCrawler()
    try:
        result = await crawler.crawl_from_doi(
            payload.seed_doi,
            max_depth_backward=payload.max_depth_backward,
            max_depth_forward=payload.max_depth_forward,
            max_papers_total=payload.max_papers_total,
            per_paper_limit=payload.per_paper_limit,
            project_id=project_id,
        )
        save_crawl_result(project_id=project_id, papers=result.papers, citations=result.citations)
        update_project_status(
            project_id,
            status="complete",
            paper_count=len(result.papers),
            edge_count=len(result.citations),
        )
    except PaperResolutionError as exc:
        update_project_status(project_id, status="failed", error_message=str(exc))
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        update_project_status(project_id, status="failed", error_message=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await crawler.aclose()

    return {
        "project_id": project_id,
        "status": "complete",
        "seed": result.seed.model_dump(mode="json"),
        "paper_count": len(result.papers),
        "edge_count": len(result.citations),
        "truncated": result.truncated,
        "crawl_summary": result.summary.to_dict() if result.summary else None,
    }


@router.get("")
def get_projects() -> list[dict]:
    projects = list_projects()
    return [_project_payload(project) for project in projects]


@router.get("/{project_id}")
def get_project_detail(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_payload(project)


@router.get("/{project_id}/status")
def get_project_status(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project": _project_payload(project),
        "crawl_run": get_latest_crawl_run(project_id),
    }


@router.get("/{project_id}/paper-cards")
def get_project_paper_cards(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    cards = list_project_paper_cards(project_id)
    return {"project_id": project_id, "paper_cards": cards}


@router.post("/{project_id}/summarize")
async def summarize_project(project_id: str, payload: ProjectSummarizeRequest | None = None) -> dict:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    seed = load_project_seed_paper(project_id)
    if not seed:
        raise HTTPException(status_code=404, detail="Project seed paper not found")

    force = bool(payload.force) if payload else False
    papers, _ = load_project_graph(project_id)
    summarizer = PaperSummarizer()
    summarized_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []

    for paper in papers:
        paper_key = paper.paper_key or ""
        if not force and get_paper_summary(paper_key):
            skipped_count += 1
            continue
        try:
            summary = await summarizer.summarize_paper(seed, paper)
            save_paper_summary(summary)
            summarized_count += 1
        except Exception as exc:
            failed_count += 1
            errors.append(f"{paper_key}: {exc}")

    return {
        "project_id": project_id,
        "summarized_count": summarized_count,
        "skipped_count": skipped_count,
        "summary_failed_count": failed_count,
        "errors": errors,
    }


@router.post("/{project_id}/report")
def generate_project_report(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    seed = load_project_seed_paper(project_id)
    if not seed:
        raise HTTPException(status_code=404, detail="Project seed paper not found")

    paper_cards = list_project_paper_cards(project_id)
    graph_metrics = GraphAnalyzer().analyze_project(project_id)
    markdown = ReportGenerator().generate(
        seed_paper=seed,
        paper_cards=paper_cards,
        graph_metrics=graph_metrics,
    )
    report_path = write_project_report_file(project_id, markdown)
    save_project_report(project_id, markdown, str(report_path))
    return {
        "project_id": project_id,
        "report_path": str(report_path),
        "markdown": markdown,
    }


@router.get("/{project_id}/report")
def get_project_literature_report(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    report = get_project_report(project_id)
    if not report:
        raise HTTPException(status_code=404, detail="Project report not found")
    return report
