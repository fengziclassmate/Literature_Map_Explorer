from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.db.database import get_project, load_project_graph
from app.services.graph_analyzer import GraphAnalyzer
from app.services.graph_builder import GraphBuilder

router = APIRouter(prefix="/graph", tags=["graph"])


def _load_graph_or_404(project_id: str):
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    papers, citations = load_project_graph(project_id)
    graph = GraphBuilder().build_graph(papers, citations)
    return project, graph


@router.get("/{project_id}")
def get_graph(project_id: str) -> dict:
    project, graph = _load_graph_or_404(project_id)
    return {
        "project_id": project["project_id"],
        "edge_direction": "citing_paper_id -> cited_paper_id",
        "paper_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "elements": GraphBuilder().to_cytoscape(graph),
    }


@router.get("/{project_id}/analysis")
def get_analysis(project_id: str) -> dict:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project_id": project["project_id"],
        "analysis": GraphAnalyzer().analyze_project(project_id),
    }


@router.get("/{project_id}/review")
def get_review(project_id: str) -> Response:
    _, graph = _load_graph_or_404(project_id)
    markdown = GraphAnalyzer().generate_markdown_review(graph)
    return Response(content=markdown, media_type="text/markdown")


@router.get("/{project_id}/export")
def export_graph(
    project_id: str,
    fmt: str = Query(default="graphml", pattern="^(csv|bibtex|graphml|markdown)$"),
) -> Response:
    _, graph = _load_graph_or_404(project_id)
    builder = GraphBuilder()
    if fmt == "csv":
        return Response(content=builder.export_csv_bundle(graph), media_type="text/csv")
    if fmt == "bibtex":
        return Response(content=builder.export_bibtex(graph), media_type="application/x-bibtex")
    if fmt == "markdown":
        return Response(content=builder.export_markdown(graph), media_type="text/markdown")
    return Response(content=builder.export_graphml(graph), media_type="application/graphml+xml")
