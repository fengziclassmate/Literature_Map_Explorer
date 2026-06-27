from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models.paper import Paper


class ReportGenerator:
    """Generate a conservative literature review from existing Paper Cards only."""

    SECTION_TITLES = [
        "\u9886\u57df\u603b\u4f53\u4ecb\u7ecd",
        "\u7814\u7a76\u95ee\u9898\u6f14\u5316",
        "\u4e3b\u8981\u7814\u7a76\u4e3b\u9898",
        "\u6838\u5fc3\u7406\u8bba\u57fa\u7840",
        "\u65b9\u6cd5\u6f14\u5316\u8109\u7edc",
        "\u6570\u636e\u6765\u6e90\u6f14\u5316",
        "\u4e3b\u8981\u5171\u8bc6",
        "\u4e3b\u8981\u4e89\u8bae",
        "\u7814\u7a76\u4e0d\u8db3",
        "\u672a\u6765\u65b9\u5411",
        "\u63a8\u8350\u9605\u8bfb\u8def\u5f84",
    ]

    def generate(
        self,
        *,
        seed_paper: Paper,
        paper_cards: list[dict[str, Any]],
        graph_metrics: dict[str, Any],
    ) -> str:
        lines = [
            "# Literature Review",
            "",
            f"Seed paper: {seed_paper.title} [{seed_paper.paper_key}]",
            "",
            f"Graph scope: {graph_metrics.get('paper_count', 0)} papers, {graph_metrics.get('edge_count', 0)} citation edges.",
            "",
        ]
        for title in self.SECTION_TITLES:
            lines.extend([f"## {title}", ""])
            lines.extend(self._section_lines(title, paper_cards, graph_metrics))
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def write_literature_review(self, output_dir: str | Path, markdown: str) -> Path:
        path = Path(output_dir) / "literature_review.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return path

    def _section_lines(
        self,
        section_title: str,
        paper_cards: list[dict[str, Any]],
        graph_metrics: dict[str, Any],
    ) -> list[str]:
        summaries = [card for card in paper_cards if card.get("summary")]
        if not summaries:
            return ["No Paper Cards are available for this section."]

        if section_title == "\u9886\u57df\u603b\u4f53\u4ecb\u7ecd":
            return self._bullets(summaries, "one_sentence_summary")
        if section_title == "\u7814\u7a76\u95ee\u9898\u6f14\u5316":
            return self._year_bullets(summaries, "research_problem")
        if section_title == "\u4e3b\u8981\u7814\u7a76\u4e3b\u9898":
            communities = graph_metrics.get("communities") or graph_metrics.get("clusters") or []
            if communities:
                return [
                    f"- Community {item.get('community_id', item.get('cluster_id'))}: {', '.join(item.get('top_terms') or []) or 'No explicit terms'}; papers: {', '.join(item.get('paper_keys', [])[:5])}"
                    for item in communities[:10]
                ]
            return self._bullets(summaries, "research_background")
        if section_title == "\u6838\u5fc3\u7406\u8bba\u57fa\u7840":
            return self._bullets(summaries, "research_background")
        if section_title == "\u65b9\u6cd5\u6f14\u5316\u8109\u7edc":
            return self._year_bullets(summaries, "methods")
        if section_title == "\u6570\u636e\u6765\u6e90\u6f14\u5316":
            return self._year_bullets(summaries, "data_sources")
        if section_title == "\u4e3b\u8981\u5171\u8bc6":
            return self._bullets(summaries, "key_findings")
        if section_title == "\u4e3b\u8981\u4e89\u8bae":
            return ["- Paper Cards do not explicitly record controversies unless listed below."] + self._bullets(
                summaries,
                "limitations",
            )
        if section_title == "\u7814\u7a76\u4e0d\u8db3":
            return self._bullets(summaries, "limitations")
        if section_title == "\u672a\u6765\u65b9\u5411":
            return self._bullets(summaries, "future_work")
        if section_title == "\u63a8\u8350\u9605\u8bfb\u8def\u5f84":
            core = graph_metrics.get("core_papers") or graph_metrics.get("key_papers") or []
            if core:
                return [
                    f"- {item.get('paper_key')}: {item.get('title')} (PageRank={item.get('pagerank', 0):.4f})"
                    for item in core[:10]
                ]
            return self._bullets(summaries, "relation_to_seed")
        return ["No evidence available in Paper Cards."]

    def _bullets(self, paper_cards: list[dict[str, Any]], field: str) -> list[str]:
        lines: list[str] = []
        for card in paper_cards:
            summary = card.get("summary") or {}
            paper = card.get("paper") or {}
            value = summary.get(field)
            if value and not str(value).startswith("Not available"):
                lines.append(f"- {summary.get('paper_id') or paper.get('paper_key')}: {value}")
        return lines or ["- No explicit evidence in available Paper Cards."]

    def _year_bullets(self, paper_cards: list[dict[str, Any]], field: str) -> list[str]:
        sorted_cards = sorted(
            paper_cards,
            key=lambda card: ((card.get("paper") or {}).get("year") or 999999, (card.get("paper") or {}).get("title") or ""),
        )
        lines = []
        for card in sorted_cards:
            summary = card.get("summary") or {}
            paper = card.get("paper") or {}
            value = summary.get(field)
            if value and not str(value).startswith("Not available"):
                lines.append(f"- {paper.get('year') or 'n.d.'} - {summary.get('paper_id') or paper.get('paper_key')}: {value}")
        return lines or ["- No explicit temporal evidence in available Paper Cards."]
