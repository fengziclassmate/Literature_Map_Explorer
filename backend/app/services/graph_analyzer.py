from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import networkx as nx

from app.db.database import load_project_graph
from app.models.citation import CitationEdge
from app.models.paper import Paper


class GraphAnalyzer:
    """Build and analyze directed citation graphs from SQLite-backed project data."""

    def analyze_project(self, project_id: str) -> dict[str, Any]:
        """Load project papers and citation edges from SQLite, then return graph analysis."""
        papers, citation_edges = load_project_graph(project_id)
        graph = self.build_graph(papers, citation_edges)
        analysis = self.analyze(graph)
        analysis["project_id"] = project_id
        return analysis

    def build_graph(self, papers: list[Paper], citation_edges: list[CitationEdge]) -> nx.DiGraph:
        """Build a directed graph where each edge is citing_paper_id -> cited_paper_id."""
        graph = nx.DiGraph()
        for paper in papers:
            keyed = paper.with_identity()
            graph.add_node(
                keyed.paper_key,
                title=keyed.title,
                doi=keyed.doi,
                openalex_id=keyed.openalex_id,
                semantic_scholar_id=keyed.semantic_scholar_id,
                year=keyed.year,
                venue=keyed.venue,
                citation_count=keyed.citation_count or 0,
                reference_count=keyed.reference_count or 0,
                fields_of_study=keyed.fields_of_study,
                source_api=keyed.source_api,
            )

        for edge in citation_edges:
            if edge.source_key == edge.target_key:
                continue
            if edge.source_key not in graph or edge.target_key not in graph:
                continue
            graph.add_edge(
                edge.source_key,
                edge.target_key,
                relation="cites",
                discovered_via=edge.discovered_via,
            )
        return graph

    def analyze(self, graph: nx.DiGraph) -> dict[str, Any]:
        """Compute centrality, communities, yearly counts, and Cytoscape elements."""
        in_degree = dict(graph.in_degree())
        out_degree = dict(graph.out_degree())
        pagerank = self._pagerank(graph)
        betweenness = (
            nx.betweenness_centrality(graph, normalized=True)
            if graph.number_of_nodes() > 1 and graph.number_of_edges() > 0
            else {node: 0.0 for node in graph.nodes}
        )
        communities = self._communities(graph)
        community_by_node = {
            paper_key: community["community_id"]
            for community in communities
            for paper_key in community["paper_keys"]
        }

        centrality = {
            node: {
                "in_degree": in_degree.get(node, 0),
                "out_degree": out_degree.get(node, 0),
                "pagerank": pagerank.get(node, 0.0),
                "betweenness": betweenness.get(node, 0.0),
                "community_id": community_by_node.get(node),
            }
            for node in graph.nodes
        }
        core_papers = self._core_papers(graph, centrality)
        papers_by_year = self._papers_by_year(graph)
        nodes, edges = self._cytoscape_elements(graph, centrality)

        return {
            "paper_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
            "edge_direction": "citing_paper_id -> cited_paper_id",
            "core_papers": core_papers,
            "key_papers": core_papers,
            "communities": communities,
            "clusters": communities,
            "papers_by_year": papers_by_year,
            "timeline": papers_by_year,
            "centrality": centrality,
            "nodes": nodes,
            "edges": edges,
            "elements": {"nodes": nodes, "edges": edges},
        }

    def generate_markdown_review(self, graph: nx.DiGraph) -> str:
        """Generate a lightweight Markdown review draft from graph analysis."""
        analysis = self.analyze(graph)
        lines = [
            "# Field Review Draft",
            "",
            "## Scope",
            "",
            f"This draft summarizes a citation graph with {graph.number_of_nodes()} papers and {graph.number_of_edges()} citation edges.",
            "",
            "## Key Papers",
            "",
        ]
        for paper in analysis["core_papers"][:10]:
            year = paper.get("year") or "n.d."
            lines.append(f"- {paper['title']} ({year}); PageRank={paper['pagerank']:.4f}")

        lines.extend(["", "## Topic Clusters", ""])
        for community in analysis["communities"]:
            label = ", ".join(community["top_terms"]) or "unlabeled"
            lines.append(f"- Community {community['community_id']}: {community['size']} papers; terms: {label}")

        lines.extend(["", "## Time Evolution", ""])
        for bucket in analysis["papers_by_year"]:
            lines.append(f"- {bucket['year']}: {bucket['paper_count']} papers")

        lines.extend(
            [
                "",
                "## Next Steps",
                "",
                "- Validate cluster labels against abstracts and keywords.",
                "- Expand forward citations for recent papers with low in-graph citation counts.",
                "- Replace this heuristic draft with an LLM-backed synthesis after review controls are added.",
            ]
        )
        return "\n".join(lines) + "\n"

    def _core_papers(self, graph: nx.DiGraph, centrality: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the top 20 papers ranked by PageRank, in-degree, then betweenness."""
        ranked = [
            {
                "paper_key": node,
                "title": attrs.get("title"),
                "doi": attrs.get("doi"),
                "year": attrs.get("year"),
                "venue": attrs.get("venue"),
                "citation_count": attrs.get("citation_count", 0),
                **centrality[node],
            }
            for node, attrs in graph.nodes(data=True)
        ]
        ranked.sort(
            key=lambda item: (
                item["pagerank"],
                item["in_degree"],
                item["betweenness"],
                item["citation_count"],
            ),
            reverse=True,
        )
        return ranked[:20]

    def _communities(self, graph: nx.DiGraph) -> list[dict[str, Any]]:
        """Detect communities on the undirected projection of the citation graph."""
        undirected = graph.to_undirected()
        if undirected.number_of_nodes() == 0:
            return []
        if undirected.number_of_edges() == 0:
            community_sets = [{node} for node in undirected.nodes]
        else:
            community_sets = list(nx.community.greedy_modularity_communities(undirected))

        communities: list[dict[str, Any]] = []
        for index, community in enumerate(community_sets):
            term_counter: Counter[str] = Counter()
            for node in community:
                fields = graph.nodes[node].get("fields_of_study") or []
                if isinstance(fields, str):
                    terms = [term.strip() for term in fields.split(";") if term.strip()]
                else:
                    terms = [str(term).strip() for term in fields if str(term).strip()]
                term_counter.update(terms)
            communities.append(
                {
                    "community_id": index,
                    "cluster_id": index,
                    "size": len(community),
                    "paper_keys": sorted(community),
                    "top_terms": [term for term, _ in term_counter.most_common(5)],
                }
            )
        communities.sort(key=lambda item: item["size"], reverse=True)
        return communities

    def _papers_by_year(self, graph: nx.DiGraph) -> list[dict[str, int]]:
        """Count papers by publication year."""
        counts: defaultdict[int, int] = defaultdict(int)
        for _, attrs in graph.nodes(data=True):
            year = attrs.get("year")
            if isinstance(year, int):
                counts[year] += 1
        return [{"year": year, "paper_count": count} for year, count in sorted(counts.items())]

    def _cytoscape_elements(
        self,
        graph: nx.DiGraph,
        centrality: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return Cytoscape.js-compatible nodes and directed edges."""
        nodes = [
            {
                "data": {
                    "id": node,
                    "label": attrs.get("title") or node,
                    "title": attrs.get("title"),
                    "doi": attrs.get("doi"),
                    "openalex_id": attrs.get("openalex_id"),
                    "semantic_scholar_id": attrs.get("semantic_scholar_id"),
                    "year": attrs.get("year"),
                    "venue": attrs.get("venue"),
                    "citation_count": attrs.get("citation_count", 0),
                    "reference_count": attrs.get("reference_count", 0),
                    **centrality.get(node, {}),
                },
                "classes": f"paper community-{centrality.get(node, {}).get('community_id')}",
            }
            for node, attrs in graph.nodes(data=True)
        ]
        edges = [
            {
                "data": {
                    "id": f"{source}->{target}",
                    "source": source,
                    "target": target,
                    "relation": attrs.get("relation", "cites"),
                    "discovered_via": attrs.get("discovered_via"),
                },
                "classes": "citation",
            }
            for source, target, attrs in graph.edges(data=True)
        ]
        return nodes, edges

    def _pagerank(
        self,
        graph: nx.DiGraph,
        *,
        damping: float = 0.85,
        max_iter: int = 100,
        tol: float = 1e-9,
    ) -> dict[str, float]:
        """Pure-Python PageRank to avoid adding scipy/numpy for the initial backend."""
        nodes = list(graph.nodes)
        if not nodes:
            return {}
        node_count = len(nodes)
        scores = {node: 1.0 / node_count for node in nodes}
        base = (1.0 - damping) / node_count

        for _ in range(max_iter):
            next_scores = {node: base for node in nodes}
            dangling_score = sum(scores[node] for node in nodes if graph.out_degree(node) == 0)
            dangling_share = damping * dangling_score / node_count

            for source in nodes:
                targets = list(graph.successors(source))
                if not targets:
                    continue
                contribution = damping * scores[source] / len(targets)
                for target in targets:
                    next_scores[target] += contribution

            for node in nodes:
                next_scores[node] += dangling_share

            delta = sum(abs(next_scores[node] - scores[node]) for node in nodes)
            scores = next_scores
            if delta < tol:
                break
        return scores
