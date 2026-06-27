from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.models.paper import Paper
from app.models.paper_summary import PaperSummary
from app.services.llm_client import DummyLLMClient, LLMClient

logger = logging.getLogger(__name__)


class PaperSummarizer:
    """Create structured Paper Cards from metadata and abstract text."""

    SUMMARY_FIELDS = [
        "one_sentence_summary",
        "research_background",
        "research_problem",
        "objectives",
        "data_sources",
        "methods",
        "key_findings",
        "contributions",
        "limitations",
        "future_work",
        "relation_to_seed",
        "relevance_score",
        "summary_confidence",
    ]

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm_client = llm_client or DummyLLMClient()

    async def summarize_paper(self, seed_paper: Paper, target_paper: Paper) -> PaperSummary:
        """Summarize one paper without allowing malformed LLM output to abort the crawl."""
        seed = seed_paper.with_identity()
        target = target_paper.with_identity()
        summary_level = "abstract" if target.abstract else "metadata"
        raw_output: str | None = None

        if not target.abstract:
            return self.fallback_summary(
                seed,
                target,
                summary_level="metadata",
                raw_llm_output=None,
                confidence=0.25,
            )

        prompt = self._build_prompt(seed, target)
        try:
            raw_output = await self.llm_client.generate_json(prompt)
            parsed = self._parse_json(raw_output)
            return self._summary_from_payload(
                seed,
                target,
                parsed,
                summary_level=summary_level,
                raw_llm_output=raw_output,
            )
        except Exception as exc:
            logger.warning("Paper summary fallback used for %s: %s", target.paper_key, exc)
            return self.fallback_summary(
                seed,
                target,
                summary_level=summary_level,
                raw_llm_output=raw_output,
                confidence=0.35,
            )

    def fallback_summary(
        self,
        seed_paper: Paper,
        target_paper: Paper,
        *,
        summary_level: str,
        raw_llm_output: str | None,
        confidence: float,
    ) -> PaperSummary:
        """Create a conservative summary using only metadata and abstract text."""
        target = target_paper.with_identity()
        first_sentence = self._first_sentence(target.abstract) if target.abstract else None
        title = target.title or "Untitled paper"
        venue_year = " ".join(str(value) for value in [target.venue, target.year] if value)
        one_sentence = first_sentence or f"{title} ({venue_year or 'metadata only'}) lacks an available abstract."
        relation = self._relation_to_seed(seed_paper, target)
        relevance = self._relevance_score(seed_paper, target)

        return PaperSummary(
            paper_id=target.paper_key or "",
            one_sentence_summary=one_sentence,
            research_background=first_sentence or "Not available from metadata.",
            research_problem="Not available from metadata." if not target.abstract else "Not explicitly stated in the abstract.",
            objectives="Not available from metadata." if not target.abstract else "Not explicitly stated in the abstract.",
            data_sources="Not available from metadata." if not target.abstract else "Not explicitly stated in the abstract.",
            methods="Not available from metadata." if not target.abstract else "Not explicitly stated in the abstract.",
            key_findings=first_sentence or "Not available from metadata.",
            contributions="Not available from metadata." if not target.abstract else "Not explicitly stated in the abstract.",
            limitations="Not available from metadata." if not target.abstract else "Not explicitly stated in the abstract.",
            future_work="Not available from metadata." if not target.abstract else "Not explicitly stated in the abstract.",
            relation_to_seed=relation,
            relevance_score=relevance,
            summary_confidence=confidence,
            summary_level=summary_level,  # type: ignore[arg-type]
            raw_llm_output=raw_llm_output,
        )

    def _summary_from_payload(
        self,
        seed_paper: Paper,
        target_paper: Paper,
        payload: dict[str, Any],
        *,
        summary_level: str,
        raw_llm_output: str,
    ) -> PaperSummary:
        target = target_paper.with_identity()
        fallback = self.fallback_summary(
            seed_paper,
            target,
            summary_level=summary_level,
            raw_llm_output=raw_llm_output,
            confidence=0.5 if summary_level == "abstract" else 0.25,
        )
        data = fallback.model_dump(mode="json")
        for field in self.SUMMARY_FIELDS:
            value = payload.get(field)
            if value not in (None, ""):
                data[field] = value
        data["paper_id"] = target.paper_key
        data["summary_level"] = summary_level
        data["raw_llm_output"] = raw_llm_output
        data["relevance_score"] = self._clamp_float(data.get("relevance_score"), fallback.relevance_score)
        data["summary_confidence"] = self._clamp_float(data.get("summary_confidence"), fallback.summary_confidence)
        return PaperSummary(**data)

    def _build_prompt(self, seed_paper: Paper, target_paper: Paper) -> str:
        payload = {
            "instruction": "Return only JSON for a structured Paper Card. Do not invent facts absent from inputs.",
            "seed_paper": {
                "paper_id": seed_paper.paper_key,
                "title": seed_paper.title,
                "abstract": seed_paper.abstract,
                "year": seed_paper.year,
                "venue": seed_paper.venue,
            },
            "target_paper": {
                "paper_id": target_paper.paper_key,
                "title": target_paper.title,
                "abstract": target_paper.abstract,
                "year": target_paper.year,
                "venue": target_paper.venue,
            },
            "required_fields": self.SUMMARY_FIELDS,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _parse_json(self, raw_output: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_output)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw_output, flags=re.DOTALL)
            if not match:
                raise
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("LLM output must be a JSON object")
        return parsed

    def _first_sentence(self, abstract: str | None) -> str | None:
        if not abstract:
            return None
        parts = re.split(r"(?<=[.!?])\s+", abstract.strip())
        return parts[0][:500] if parts and parts[0] else abstract[:500]

    def _relation_to_seed(self, seed_paper: Paper, target_paper: Paper) -> str:
        if seed_paper.paper_key == target_paper.paper_key:
            return "Seed paper."
        shared_terms = set(seed_paper.fields_of_study) & set(target_paper.fields_of_study)
        if shared_terms:
            return "Shares field labels with the seed paper: " + ", ".join(sorted(shared_terms)[:5])
        return "Relation to seed is not explicit in available metadata."

    def _relevance_score(self, seed_paper: Paper, target_paper: Paper) -> float:
        seed_terms = self._terms(seed_paper.title) | set(term.lower() for term in seed_paper.fields_of_study)
        target_terms = self._terms(target_paper.title) | set(term.lower() for term in target_paper.fields_of_study)
        if not seed_terms or not target_terms:
            return 0.4 if seed_paper.paper_key == target_paper.paper_key else 0.2
        overlap = len(seed_terms & target_terms) / max(len(seed_terms | target_terms), 1)
        if seed_paper.paper_key == target_paper.paper_key:
            overlap = max(overlap, 1.0)
        return round(min(max(overlap, 0.0), 1.0), 3)

    def _terms(self, value: str | None) -> set[str]:
        if not value:
            return set()
        return {term for term in re.findall(r"[a-zA-Z0-9]+", value.lower()) if len(term) > 2}

    def _clamp_float(self, value: Any, fallback: float | None) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return round(min(max(number, 0.0), 1.0), 3)

