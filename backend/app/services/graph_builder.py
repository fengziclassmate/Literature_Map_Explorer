from __future__ import annotations

import csv
import io
import re
from typing import Any

import networkx as nx

from app.db.database import get_paper_summaries
from app.models.citation import CitationEdge
from app.models.paper_summary import PaperSummary
from app.models.paper import Paper


class GraphBuilder:
    def build_graph(self, papers: list[Paper], citations: list[CitationEdge]) -> nx.DiGraph:
        graph = nx.DiGraph()
        for paper in papers:
            keyed = paper.with_identity()
            graph.add_node(
                keyed.paper_key,
                title=keyed.title,
                doi=keyed.doi,
                year=keyed.year,
                venue=keyed.venue,
                citation_count=keyed.citation_count or 0,
                reference_count=keyed.reference_count or 0,
                fields_of_study="; ".join(keyed.fields_of_study),
                source_api=keyed.source_api,
            )
        for edge in citations:
            if edge.source_key in graph and edge.target_key in graph and edge.source_key != edge.target_key:
                graph.add_edge(edge.source_key, edge.target_key, relation="cites", discovered_via=edge.discovered_via)
        return graph

    def to_cytoscape(self, graph: nx.DiGraph, summaries: dict[str, PaperSummary] | None = None) -> dict[str, Any]:
        summaries = summaries if summaries is not None else get_paper_summaries(str(node_id) for node_id in graph.nodes)
        nodes = [
            {
                "data": {
                    "id": node_id,
                    **attrs,
                    **self._summary_node_data(summaries.get(str(node_id))),
                },
                "classes": "paper",
            }
            for node_id, attrs in graph.nodes(data=True)
        ]
        edges = [
            {
                "data": {
                    "id": f"{source}->{target}",
                    "source": source,
                    "target": target,
                    **attrs,
                },
                "classes": "citation",
            }
            for source, target, attrs in graph.edges(data=True)
        ]
        return {"nodes": nodes, "edges": edges}

    def _summary_node_data(self, summary: PaperSummary | None) -> dict[str, Any]:
        if summary is None:
            return {
                "summary": None,
                "relevance_score": None,
                "relation_to_seed": None,
                "summary_confidence": None,
                "summary_level": None,
            }
        return {
            "summary": summary.one_sentence_summary,
            "one_sentence_summary": summary.one_sentence_summary,
            "research_problem": summary.research_problem,
            "data_sources": summary.data_sources,
            "methods": summary.methods,
            "key_findings": summary.key_findings,
            "contributions": summary.contributions,
            "limitations": summary.limitations,
            "future_work": summary.future_work,
            "relation_to_seed": summary.relation_to_seed,
            "relevance_score": summary.relevance_score,
            "summary_confidence": summary.summary_confidence,
            "summary_level": summary.summary_level,
        }

    def export_csv_bundle(self, graph: nx.DiGraph) -> str:
        nodes_io = io.StringIO()
        edges_io = io.StringIO()

        node_writer = csv.DictWriter(
            nodes_io,
            fieldnames=["paper_key", "title", "doi", "year", "venue", "citation_count", "fields_of_study"],
        )
        node_writer.writeheader()
        for paper_key, attrs in graph.nodes(data=True):
            node_writer.writerow({"paper_key": paper_key, **{key: attrs.get(key) for key in node_writer.fieldnames[1:]}})

        edge_writer = csv.DictWriter(edges_io, fieldnames=["source_key", "target_key", "relation", "discovered_via"])
        edge_writer.writeheader()
        for source, target, attrs in graph.edges(data=True):
            edge_writer.writerow(
                {
                    "source_key": source,
                    "target_key": target,
                    "relation": attrs.get("relation", "cites"),
                    "discovered_via": attrs.get("discovered_via"),
                }
            )

        return "# nodes.csv\n" + nodes_io.getvalue() + "\n# edges.csv\n" + edges_io.getvalue()

    def export_bibtex(self, graph: nx.DiGraph) -> str:
        entries: list[str] = []
        for paper_key, attrs in graph.nodes(data=True):
            citation_key = self._bibtex_key(attrs.get("title") or paper_key, attrs.get("year"))
            fields = {
                "title": attrs.get("title"),
                "year": attrs.get("year"),
                "journal": attrs.get("venue"),
                "doi": attrs.get("doi"),
                "url": attrs.get("doi") and f"https://doi.org/{attrs.get('doi')}",
            }
            body = "\n".join(
                f"  {key} = {{{self._escape_bibtex(str(value))}}},"
                for key, value in fields.items()
                if value
            )
            entries.append(f"@article{{{citation_key},\n{body}\n}}")
        return "\n\n".join(entries)

    def export_graphml(self, graph: nx.DiGraph) -> str:
        buffer = io.BytesIO()
        nx.write_graphml(graph, buffer)
        return buffer.getvalue().decode("utf-8")

    def export_markdown(self, graph: nx.DiGraph) -> str:
        lines = [
            "# Literature Map",
            "",
            f"- Papers: {graph.number_of_nodes()}",
            f"- Citation edges: {graph.number_of_edges()}",
            "- Edge direction: source cites target",
            "",
            "## Papers",
            "",
        ]
        for paper_key, attrs in graph.nodes(data=True):
            year = attrs.get("year") or "n.d."
            doi = attrs.get("doi") or "no DOI"
            lines.append(f"- {attrs.get('title')} ({year}) - {doi} [{paper_key}]")
        return "\n".join(lines) + "\n"

    def _bibtex_key(self, title: str, year: int | None) -> str:
        words = re.findall(r"[A-Za-z0-9]+", title)
        base = "".join(words[:3]) or "paper"
        return f"{base}{year or ''}"

    def _escape_bibtex(self, value: str) -> str:
        return value.replace("{", "\\{").replace("}", "\\}")
