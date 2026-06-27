from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable

from app.core.settings import get_settings
from app.models.citation import CitationEdge
from app.models.paper import Paper
from app.models.paper_summary import PaperSummary

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def sqlite_path_from_url(database_url: str | None = None) -> Path | str:
    url = database_url or get_settings().database_url
    if url == "sqlite:///:memory:" or url == ":memory:":
        return ":memory:"
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///"))
    if url.startswith("sqlite://"):
        return Path(url.removeprefix("sqlite://"))
    return Path(url)


def get_connection(database_url: str | None = None) -> sqlite3.Connection:
    db_path = sqlite_path_from_url(database_url)
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        database = str(db_path)
    else:
        database = db_path
    conn = sqlite3.connect(database, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(database_url: str | None = None) -> None:
    with get_connection(database_url) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def create_project(
    *,
    name: str,
    seed_doi: str,
    settings: dict,
    status: str = "created",
) -> str:
    project_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO projects(project_id, name, seed_doi, settings_json, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, name, seed_doi, json.dumps(settings, sort_keys=True), status),
        )
    return project_id


def update_project_status(
    project_id: str,
    *,
    status: str,
    paper_count: int | None = None,
    edge_count: int | None = None,
    error_message: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE projects
            SET status = ?,
                paper_count = COALESCE(?, paper_count),
                edge_count = COALESCE(?, edge_count),
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE project_id = ?
            """,
            (status, paper_count, edge_count, error_message, project_id),
        )


def create_crawl_run(project_id: str, *, status: str = "queued", stats: dict | None = None) -> str:
    run_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO crawl_runs(run_id, project_id, status, stats_json)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, project_id, status, json.dumps(stats or {}, sort_keys=True)),
        )
    return run_id


def update_crawl_run(
    run_id: str,
    *,
    status: str | None = None,
    stats: dict | None = None,
    error_message: str | None = None,
    finished: bool = False,
) -> None:
    finished_sql = "CURRENT_TIMESTAMP" if finished else "finished_at"
    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE crawl_runs
            SET status = COALESCE(?, status),
                stats_json = COALESCE(?, stats_json),
                error_message = ?,
                finished_at = {finished_sql}
            WHERE run_id = ?
            """,
            (
                status,
                json.dumps(stats, sort_keys=True) if stats is not None else None,
                error_message,
                run_id,
            ),
        )


def _crawl_run_from_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    stats_json = data.pop("stats_json") or "{}"
    try:
        data["stats"] = json.loads(stats_json)
    except json.JSONDecodeError:
        data["stats"] = {}
    return data


def get_latest_crawl_run(project_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT run_id, project_id, status, started_at, finished_at, stats_json, error_message
            FROM crawl_runs
            WHERE project_id = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    return _crawl_run_from_row(row)


def list_projects() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT project_id, name, seed_doi, settings_json, status, paper_count,
                   edge_count, error_message, created_at, updated_at
            FROM projects
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_project(project_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT project_id, name, seed_doi, settings_json, status, paper_count,
                   edge_count, error_message, created_at, updated_at
            FROM projects
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
    return dict(row) if row else None


def save_crawl_result(
    *,
    project_id: str,
    papers: Iterable[Paper],
    citations: Iterable[CitationEdge],
) -> None:
    paper_list = [paper.with_identity() for paper in papers]
    citation_list = list(citations)

    with get_connection() as conn:
        for paper in paper_list:
            raw_json = json.dumps(paper.model_dump(mode="json"), sort_keys=True)
            conn.execute(
                """
                INSERT INTO papers(
                    paper_key, doi, openalex_id, semantic_scholar_id, normalized_title,
                    title, abstract, year, venue, url, pdf_url,
                    reference_count, citation_count, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_key) DO UPDATE SET
                    doi = COALESCE(excluded.doi, papers.doi),
                    openalex_id = COALESCE(excluded.openalex_id, papers.openalex_id),
                    semantic_scholar_id = COALESCE(excluded.semantic_scholar_id, papers.semantic_scholar_id),
                    normalized_title = COALESCE(excluded.normalized_title, papers.normalized_title),
                    title = COALESCE(excluded.title, papers.title),
                    abstract = COALESCE(excluded.abstract, papers.abstract),
                    year = COALESCE(excluded.year, papers.year),
                    venue = COALESCE(excluded.venue, papers.venue),
                    url = COALESCE(excluded.url, papers.url),
                    pdf_url = COALESCE(excluded.pdf_url, papers.pdf_url),
                    reference_count = COALESCE(excluded.reference_count, papers.reference_count),
                    citation_count = COALESCE(excluded.citation_count, papers.citation_count),
                    raw_json = excluded.raw_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    paper.paper_key,
                    paper.doi,
                    paper.openalex_id,
                    paper.semantic_scholar_id,
                    paper.normalized_title,
                    paper.title,
                    paper.abstract,
                    paper.year,
                    paper.venue,
                    paper.url,
                    paper.pdf_url,
                    paper.reference_count,
                    paper.citation_count,
                    raw_json,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO project_papers(project_id, paper_key)
                VALUES (?, ?)
                """,
                (project_id, paper.paper_key),
            )

        for edge in citation_list:
            if edge.source_key == edge.target_key:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO citations(
                    project_id, source_key, target_key, discovered_via, raw_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    edge.source_key,
                    edge.target_key,
                    edge.discovered_via,
                    json.dumps(edge.raw, sort_keys=True),
                ),
            )

        conn.execute(
            """
            UPDATE projects
            SET paper_count = ?, edge_count = ?, updated_at = CURRENT_TIMESTAMP
            WHERE project_id = ?
            """,
            (len(paper_list), len(citation_list), project_id),
        )


def load_project_graph(project_id: str) -> tuple[list[Paper], list[CitationEdge]]:
    with get_connection() as conn:
        paper_rows = conn.execute(
            """
            SELECT p.raw_json
            FROM papers p
            JOIN project_papers pp ON pp.paper_key = p.paper_key
            WHERE pp.project_id = ?
            """,
            (project_id,),
        ).fetchall()
        edge_rows = conn.execute(
            """
            SELECT source_key, target_key, discovered_via, raw_json
            FROM citations
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchall()

    papers = [Paper(**json.loads(row["raw_json"])).with_identity() for row in paper_rows]
    edges = [
        CitationEdge(
            source_key=row["source_key"],
            target_key=row["target_key"],
            discovered_via=row["discovered_via"],
            raw=json.loads(row["raw_json"]),
        )
        for row in edge_rows
    ]
    return papers, edges


def load_paper(paper_id: str) -> Paper | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT raw_json
            FROM papers
            WHERE paper_key = ?
               OR doi = ?
               OR openalex_id = ?
               OR semantic_scholar_id = ?
            LIMIT 1
            """,
            (paper_id, paper_id, paper_id, paper_id),
        ).fetchone()
    return Paper(**json.loads(row["raw_json"])).with_identity() if row else None


def load_project_seed_paper(project_id: str) -> Paper | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.raw_json
            FROM papers p
            JOIN project_papers pp ON pp.paper_key = p.paper_key
            WHERE pp.project_id = ?
            ORDER BY
                CASE WHEN pp.direction = 'seed' THEN 0 ELSE 1 END,
                COALESCE(pp.depth, 999999),
                p.created_at
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    return Paper(**json.loads(row["raw_json"])).with_identity() if row else None


def save_paper_summary(summary: PaperSummary) -> None:
    payload = summary.model_dump(mode="json")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO paper_summaries(
                paper_id, one_sentence_summary, research_background, research_problem,
                objectives, data_sources, methods, key_findings, contributions,
                limitations, future_work, relation_to_seed, relevance_score,
                summary_confidence, summary_level, raw_llm_output
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id) DO UPDATE SET
                one_sentence_summary = excluded.one_sentence_summary,
                research_background = excluded.research_background,
                research_problem = excluded.research_problem,
                objectives = excluded.objectives,
                data_sources = excluded.data_sources,
                methods = excluded.methods,
                key_findings = excluded.key_findings,
                contributions = excluded.contributions,
                limitations = excluded.limitations,
                future_work = excluded.future_work,
                relation_to_seed = excluded.relation_to_seed,
                relevance_score = excluded.relevance_score,
                summary_confidence = excluded.summary_confidence,
                summary_level = excluded.summary_level,
                raw_llm_output = excluded.raw_llm_output,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                payload["paper_id"],
                payload["one_sentence_summary"],
                payload.get("research_background"),
                payload.get("research_problem"),
                payload.get("objectives"),
                payload.get("data_sources"),
                payload.get("methods"),
                payload.get("key_findings"),
                payload.get("contributions"),
                payload.get("limitations"),
                payload.get("future_work"),
                payload.get("relation_to_seed"),
                payload.get("relevance_score"),
                payload.get("summary_confidence"),
                payload["summary_level"],
                payload.get("raw_llm_output"),
            ),
        )


def get_paper_summary(paper_id: str) -> PaperSummary | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT paper_id, one_sentence_summary, research_background, research_problem,
                   objectives, data_sources, methods, key_findings, contributions,
                   limitations, future_work, relation_to_seed, relevance_score,
                   summary_confidence, summary_level, raw_llm_output, created_at, updated_at
            FROM paper_summaries
            WHERE paper_id = ?
            """,
            (paper_id,),
        ).fetchone()
    return PaperSummary(**dict(row)) if row else None


def get_paper_summaries(paper_ids: Iterable[str]) -> dict[str, PaperSummary]:
    ids = list(dict.fromkeys(paper_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT paper_id, one_sentence_summary, research_background, research_problem,
                   objectives, data_sources, methods, key_findings, contributions,
                   limitations, future_work, relation_to_seed, relevance_score,
                   summary_confidence, summary_level, raw_llm_output, created_at, updated_at
            FROM paper_summaries
            WHERE paper_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    return {row["paper_id"]: PaperSummary(**dict(row)) for row in rows}


def list_project_paper_cards(project_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.paper_key, p.title, p.doi, p.year, p.venue, p.citation_count,
                   p.raw_json, ps.paper_id, ps.one_sentence_summary,
                   ps.research_background, ps.research_problem, ps.objectives,
                   ps.data_sources, ps.methods, ps.key_findings, ps.contributions,
                   ps.limitations, ps.future_work, ps.relation_to_seed,
                   ps.relevance_score, ps.summary_confidence, ps.summary_level,
                   ps.raw_llm_output, ps.created_at AS summary_created_at,
                   ps.updated_at AS summary_updated_at
            FROM papers p
            JOIN project_papers pp ON pp.paper_key = p.paper_key
            LEFT JOIN paper_summaries ps ON ps.paper_id = p.paper_key
            WHERE pp.project_id = ?
            ORDER BY COALESCE(pp.depth, 999999), p.year, p.title
            """,
            (project_id,),
        ).fetchall()

    cards: list[dict] = []
    for row in rows:
        data = dict(row)
        raw_json = data.pop("raw_json")
        paper = Paper(**json.loads(raw_json)).with_identity().model_dump(mode="json")
        card = {
            "paper": paper,
            "summary": None,
        }
        if data.get("paper_id"):
            card["summary"] = {
                "paper_id": data["paper_id"],
                "one_sentence_summary": data.get("one_sentence_summary"),
                "research_background": data.get("research_background"),
                "research_problem": data.get("research_problem"),
                "objectives": data.get("objectives"),
                "data_sources": data.get("data_sources"),
                "methods": data.get("methods"),
                "key_findings": data.get("key_findings"),
                "contributions": data.get("contributions"),
                "limitations": data.get("limitations"),
                "future_work": data.get("future_work"),
                "relation_to_seed": data.get("relation_to_seed"),
                "relevance_score": data.get("relevance_score"),
                "summary_confidence": data.get("summary_confidence"),
                "summary_level": data.get("summary_level"),
                "raw_llm_output": data.get("raw_llm_output"),
                "created_at": data.get("summary_created_at"),
                "updated_at": data.get("summary_updated_at"),
            }
        cards.append(card)
    return cards


def save_project_report(project_id: str, markdown_content: str, report_path: str | None = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO project_reports(project_id, markdown_content, report_path)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                markdown_content = excluded.markdown_content,
                report_path = excluded.report_path,
                updated_at = CURRENT_TIMESTAMP
            """,
            (project_id, markdown_content, report_path),
        )


def get_project_report(project_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT project_id, markdown_content, report_path, created_at, updated_at
            FROM project_reports
            WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
    return dict(row) if row else None


def write_project_report_file(project_id: str, markdown_content: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / project_id / "literature_review.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown_content, encoding="utf-8")
    return report_path
