from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod


class LLMClient(ABC):
    """Abstract interface for JSON-producing LLM calls."""

    @abstractmethod
    async def generate_json(self, prompt: str) -> str:
        """Return a JSON string that downstream services can parse."""


class DummyLLMClient(LLMClient):
    """Deterministic placeholder until a real model provider is connected."""

    async def generate_json(self, prompt: str) -> str:
        try:
            prompt_payload = json.loads(prompt)
        except json.JSONDecodeError:
            prompt_payload = {}
        target = prompt_payload.get("target_paper") or {}
        title = target.get("title") or "Untitled paper"
        abstract = target.get("abstract") or ""
        first_sentence = self._first_sentence(abstract)
        payload = {
            "one_sentence_summary": first_sentence or f"{title} is represented by metadata only.",
            "research_background": first_sentence or "Not stated in the available Paper Card inputs.",
            "research_problem": "Not explicitly stated in the available abstract.",
            "objectives": "Not explicitly stated in the available abstract.",
            "data_sources": "Not explicitly stated in the available abstract.",
            "methods": "Not explicitly stated in the available abstract.",
            "key_findings": first_sentence or "Not stated in the available Paper Card inputs.",
            "contributions": "Not explicitly stated in the available abstract.",
            "limitations": "Not explicitly stated in the available abstract.",
            "future_work": "Not explicitly stated in the available abstract.",
            "relation_to_seed": "Relation to the seed paper is inferred from citation graph context and metadata.",
            "relevance_score": 0.5,
            "summary_confidence": 0.65 if abstract else 0.25,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _first_sentence(self, value: str) -> str | None:
        if not value:
            return None
        parts = re.split(r"(?<=[.!?])\s+", value.strip())
        return parts[0][:500] if parts and parts[0] else value[:500]
