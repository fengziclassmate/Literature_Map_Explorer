from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.db.database import create_project, get_connection, load_project_graph, save_paper_summary
from app.models.citation import CitationEdge
from app.models.paper import Paper, canonical_paper_key
from app.services.metadata_enricher import MetadataEnricher
from app.services.paper_resolver import PaperResolver
from app.services.summarizer import PaperSummarizer

logger = logging.getLogger(__name__)

Direction = Literal["backward", "forward"]


@dataclass(frozen=True)
class CrawlResult:
    seed: Paper
    papers: list[Paper]
    citations: list[CitationEdge]
    truncated: bool
    summary: CrawlSummary | None = None


@dataclass
class CrawlSummary:
    project_id: str
    seed_paper_id: str
    new_papers_count: int = 0
    new_edges_count: int = 0
    failed_requests_count: int = 0
    skipped_papers_count: int = 0
    summarized_count: int = 0
    summary_failed_count: int = 0
    visited_papers_count: int = 0
    truncated: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CitationCrawler:
    """Database-backed BFS crawler for backward and forward citation expansion."""

    def __init__(
        self,
        resolver: PaperResolver | None = None,
        metadata_enricher: MetadataEnricher | None = None,
        summarizer: PaperSummarizer | None = None,
    ) -> None:
        self.resolver = resolver or PaperResolver()
        self.metadata_enricher = metadata_enricher or MetadataEnricher(
            openalex=self.resolver.openalex,
            semantic_scholar=self.resolver.semantic_scholar,
            crossref=self.resolver.crossref,
        )
        self.summarizer = summarizer or PaperSummarizer()

    async def aclose(self) -> None:
        await self.resolver.aclose()

    async def crawl(
        self,
        seed_paper_id: str,
        *,
        max_depth_backward: int = 1,
        max_depth_forward: int = 0,
        max_papers_total: int = 100,
        project_id: str | None = None,
        per_paper_limit: int = 50,
    ) -> CrawlSummary:
        """Read a seed paper from SQLite, BFS-expand citations, and persist changes."""
        seed = self._load_seed_paper(seed_paper_id)
        if seed is None:
            raise ValueError(f"Seed paper not found in database: {seed_paper_id}")

        project_id = project_id or self._find_or_create_project(seed)
        seed = await self._enrich_paper(seed, summary=None)
        self._ensure_project_paper(project_id, seed, depth=0, direction="seed")

        summary = CrawlSummary(project_id=project_id, seed_paper_id=seed.paper_key or canonical_paper_key(seed))
        await self._summarize_and_save(seed, seed, summary)
        queue: deque[tuple[Paper, Direction, int]] = deque()
        visited: set[tuple[str, Direction]] = set()
        enqueued: set[tuple[str, Direction]] = set()

        if max_depth_backward > 0:
            queue.append((seed, "backward", 0))
            enqueued.add((summary.seed_paper_id, "backward"))
        if max_depth_forward > 0:
            queue.append((seed, "forward", 0))
            enqueued.add((summary.seed_paper_id, "forward"))

        while queue:
            paper, direction, depth = queue.popleft()
            paper_key = paper.paper_key or canonical_paper_key(paper)
            state = (paper_key, direction)
            if state in visited:
                summary.skipped_papers_count += 1
                continue
            visited.add(state)
            summary.visited_papers_count += 1

            max_depth = max_depth_backward if direction == "backward" else max_depth_forward
            if depth >= max_depth:
                continue

            neighbors = await self._neighbors(paper, direction=direction, limit=per_paper_limit, summary=summary)
            for neighbor in neighbors:
                neighbor = await self._enrich_paper(neighbor, summary=summary)
                neighbor = neighbor.with_identity()
                neighbor_key = neighbor.paper_key or canonical_paper_key(neighbor)
                edge = self._edge_for_direction(
                    current_key=paper_key,
                    neighbor_key=neighbor_key,
                    direction=direction,
                    discovered_via=neighbor.source_api,
                )

                is_project_member = self._project_has_paper(project_id, neighbor_key)
                if not is_project_member and self._project_paper_count(project_id) >= max_papers_total:
                    summary.skipped_papers_count += 1
                    summary.truncated = True
                    continue

                if not is_project_member:
                    summary.new_papers_count += self._save_paper(project_id, neighbor, depth=depth + 1, direction=direction)
                else:
                    self._ensure_project_paper(project_id, neighbor, depth=depth + 1, direction=direction)

                await self._summarize_and_save(seed, neighbor, summary)
                summary.new_edges_count += self._save_edge(project_id, edge)

                next_state = (neighbor_key, direction)
                if depth + 1 < max_depth:
                    if next_state not in visited and next_state not in enqueued:
                        queue.append((neighbor, direction, depth + 1))
                        enqueued.add(next_state)
                    else:
                        summary.skipped_papers_count += 1

        self._refresh_project_counts(project_id)
        return summary

    async def crawl_from_doi(
        self,
        doi: str,
        *,
        max_depth_backward: int = 1,
        max_depth_forward: int = 0,
        max_papers_total: int = 100,
        per_paper_limit: int = 50,
        project_id: str | None = None,
    ) -> CrawlResult:
        """Compatibility wrapper that resolves a DOI and returns an in-memory crawl result."""
        seed = await self.resolver.resolve_doi(doi)
        project_id = project_id or create_project(
            name=f"DOI {seed.doi or doi}",
            seed_doi=seed.doi or doi,
            settings={
                "max_depth_backward": max_depth_backward,
                "max_depth_forward": max_depth_forward,
                "max_papers_total": max_papers_total,
                "per_paper_limit": per_paper_limit,
            },
            status="running",
        )
        self._save_paper(project_id, seed, depth=0, direction="seed")
        summary = await self.crawl(
            seed.paper_key or canonical_paper_key(seed),
            max_depth_backward=max_depth_backward,
            max_depth_forward=max_depth_forward,
            max_papers_total=max_papers_total,
            project_id=project_id,
            per_paper_limit=per_paper_limit,
        )
        papers, citations = load_project_graph(project_id)
        return CrawlResult(seed=seed, papers=papers, citations=citations, truncated=summary.truncated, summary=summary)

    async def _neighbors(
        self,
        paper: Paper,
        *,
        direction: Direction,
        limit: int,
        summary: CrawlSummary,
    ) -> list[Paper]:
        """Fetch references or citers from all available clients without aborting on errors."""
        clients = [self.resolver.openalex, self.resolver.semantic_scholar, self.resolver.crossref]
        results: list[Paper] = []
        for client in clients:
            remaining = max(limit - len(results), 0)
            if remaining <= 0:
                break

            getter = getattr(client, "get_reference_papers" if direction == "backward" else "get_citing_papers", None)
            if getter is None:
                continue

            try:
                fetched: list[Paper | dict[str, Any]] = await getter(paper, limit=remaining)
            except Exception as exc:
                summary.failed_requests_count += 1
                summary.errors.append(f"{client.__class__.__name__}.{direction}: {exc}")
                logger.exception(
                    "Citation fetch failed",
                    extra={"client": client.__class__.__name__, "direction": direction, "paper_key": paper.paper_key},
                )
                continue

            for candidate in fetched:
                try:
                    results.append(self.resolver.coerce_paper(candidate))
                except Exception as exc:
                    summary.skipped_papers_count += 1
                    logger.warning("Skipping invalid paper payload from %s: %s", client.__class__.__name__, exc)
        return results[:limit]

    async def _enrich_paper(self, paper: Paper, summary: CrawlSummary | None) -> Paper:
        try:
            return await self.metadata_enricher.enrich(paper)
        except Exception as exc:
            if summary is not None:
                summary.failed_requests_count += 1
                summary.errors.append(f"metadata_enrichment: {exc}")
            logger.exception("Metadata enrichment failed for %s", paper.paper_key)
            return paper.with_identity()

    async def _summarize_and_save(self, seed: Paper, paper: Paper, summary: CrawlSummary) -> None:
        try:
            paper_summary = await self.summarizer.summarize_paper(seed, paper)
            save_paper_summary(paper_summary)
            summary.summarized_count += 1
        except Exception as exc:
            summary.summary_failed_count += 1
            summary.errors.append(f"summary:{paper.paper_key}: {exc}")
            logger.exception("Paper summary failed for %s", paper.paper_key)

    def _load_seed_paper(self, seed_paper_id: str) -> Paper | None:
        """Load a seed by paper_key first, then by DOI or external IDs."""
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
                (seed_paper_id, seed_paper_id, seed_paper_id, seed_paper_id),
            ).fetchone()
        if not row:
            return None
        return Paper(**json.loads(row["raw_json"])).with_identity()

    def _find_or_create_project(self, seed: Paper) -> str:
        """Find an existing project containing the seed, or create a crawl project."""
        seed_key = seed.paper_key or canonical_paper_key(seed)
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT project_id
                FROM project_papers
                WHERE paper_key = ?
                ORDER BY project_id
                LIMIT 1
                """,
                (seed_key,),
            ).fetchone()
        if row:
            return str(row["project_id"])
        return create_project(
            name=f"Crawl {seed.title[:80]}",
            seed_doi=seed.doi or seed_key,
            settings={},
            status="running",
        )

    def _save_paper(self, project_id: str, paper: Paper, *, depth: int, direction: str) -> int:
        """Upsert a paper and attach it to a project; return 1 if new to project."""
        paper = paper.with_identity()
        project_had_paper = self._project_has_paper(project_id, paper.paper_key or canonical_paper_key(paper))
        raw_json = json.dumps(paper.model_dump(mode="json"), sort_keys=True)
        with get_connection() as conn:
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
                INSERT INTO project_papers(project_id, paper_key, depth, direction)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, paper_key) DO UPDATE SET
                    depth = CASE
                        WHEN project_papers.depth IS NULL THEN excluded.depth
                        WHEN excluded.depth IS NULL THEN project_papers.depth
                        ELSE MIN(project_papers.depth, excluded.depth)
                    END,
                    direction = COALESCE(project_papers.direction, excluded.direction)
                """,
                (project_id, paper.paper_key, depth, direction),
            )
        return 0 if project_had_paper else 1

    def _ensure_project_paper(self, project_id: str, paper: Paper, *, depth: int, direction: str) -> None:
        """Attach an existing seed/member paper to the project if needed."""
        self._save_paper(project_id, paper, depth=depth, direction=direction)

    def _save_edge(self, project_id: str, edge: CitationEdge) -> int:
        """Persist a citation edge; return 1 if inserted."""
        if edge.source_key == edge.target_key:
            return 0
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM citations
                WHERE project_id = ? AND source_key = ? AND target_key = ?
                """,
                (project_id, edge.source_key, edge.target_key),
            ).fetchone()
            if row:
                return 0
            conn.execute(
                """
                INSERT INTO citations(project_id, source_key, target_key, discovered_via, raw_json)
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
        return 1

    def _edge_for_direction(
        self,
        *,
        current_key: str,
        neighbor_key: str,
        direction: Direction,
        discovered_via: str,
    ) -> CitationEdge:
        """Create a directed edge as citing_paper_id -> cited_paper_id."""
        if direction == "backward":
            return CitationEdge(source_key=current_key, target_key=neighbor_key, discovered_via=discovered_via)
        return CitationEdge(source_key=neighbor_key, target_key=current_key, discovered_via=discovered_via)

    def _project_has_paper(self, project_id: str, paper_key: str) -> bool:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM project_papers WHERE project_id = ? AND paper_key = ?",
                (project_id, paper_key),
            ).fetchone()
        return row is not None

    def _project_paper_count(self, project_id: str) -> int:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM project_papers WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def _refresh_project_counts(self, project_id: str) -> None:
        """Sync denormalized project paper/edge counts after crawl writes."""
        with get_connection() as conn:
            paper_count = conn.execute(
                "SELECT COUNT(*) AS count FROM project_papers WHERE project_id = ?",
                (project_id,),
            ).fetchone()["count"]
            edge_count = conn.execute(
                "SELECT COUNT(*) AS count FROM citations WHERE project_id = ?",
                (project_id,),
            ).fetchone()["count"]
            conn.execute(
                """
                UPDATE projects
                SET paper_count = ?, edge_count = ?, updated_at = CURRENT_TIMESTAMP
                WHERE project_id = ?
                """,
                (paper_count, edge_count, project_id),
            )
