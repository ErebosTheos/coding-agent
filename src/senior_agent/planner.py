from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from senior_agent.models import ImplementationPlan
from self_healing_agent.llm_client import LLMClient

_JSON_OBJECT_PROMPT_SUFFIX: Final[str] = (
    "Return ONLY one JSON object. Do not include markdown fences or extra prose."
)


@dataclass(frozen=True)
class FeaturePlanner:
    """Create implementation plans by prompting an LLM for structured JSON output."""

    llm_client: LLMClient

    def plan_feature(self, requirement: str, codebase_summary: str) -> ImplementationPlan:
        """Generate an `ImplementationPlan` for a feature requirement."""

        requirement_clean = requirement.strip()
        summary_clean = codebase_summary.strip()
        if not requirement_clean:
            raise ValueError("requirement must not be empty.")
        if not summary_clean:
            raise ValueError("codebase_summary must not be empty.")

        prompt = self._build_prompt(
            requirement=requirement_clean,
            codebase_summary=summary_clean,
        )
        response = self.llm_client.generate_fix(prompt)
        return self._parse_plan_response(response)

    @staticmethod
    def _build_prompt(*, requirement: str, codebase_summary: str) -> str:
        return (
            "Role: Chief Architect for an autonomous software engineering agent.\n"
            "Task: Decompose the requested feature into an actionable implementation plan.\n"
            f"{_JSON_OBJECT_PROMPT_SUFFIX}\n\n"
            "JSON schema:\n"
            "{\n"
            '  "feature_name": "string",\n'
            '  "summary": "string",\n'
            '  "new_files": ["string"],\n'
            '  "modified_files": ["string"],\n'
            '  "steps": ["string"],\n'
            '  "design_guidance": "string"\n'
            "}\n\n"
            "Requirement:\n"
            f"{requirement}\n\n"
            "Codebase Summary:\n"
            f"{codebase_summary}\n"
        )

    @staticmethod
    def _parse_plan_response(response: str) -> ImplementationPlan:
        trimmed = response.strip()
        if not trimmed:
            raise ValueError("LLM returned an empty response while planning a feature.")
        try:
            payload = json.loads(trimmed)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "LLM returned invalid JSON for feature planning."
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("LLM feature plan response must be a JSON object.")
        return ImplementationPlan.from_dict(payload)
