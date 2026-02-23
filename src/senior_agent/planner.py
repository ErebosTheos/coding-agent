from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

from senior_agent.llm_client import LLMClient
from senior_agent.models import ImplementationPlan

_JSON_OBJECT_PROMPT_SUFFIX: Final[str] = (
    "Return ONLY one JSON object. Do not include markdown fences or extra prose."
)
_MAX_PLANNED_FILE_CHANGES: Final[int] = 50
_MIN_ATOMIC_GRAPH_NODES: Final[int] = 10
_MAX_ATOMIC_GRAPH_NODES: Final[int] = 20
_LARGE_REQUEST_MIN_TOKENS: Final[int] = 45
_MIN_LARGE_REQUEST_HINT_MATCHES: Final[int] = 2
_LARGE_REQUEST_HINTS: Final[tuple[str, ...]] = (
    "large project",
    "massive project",
    "enterprise",
    "multi-service",
    "multi module",
    "parallel grid",
    "project brief",
)
_SUBTASK_CONTEXT_MARKERS: Final[tuple[str, ...]] = (
    "phase context:",
    "keep changes focused and small for this subtask only.",
)


@dataclass(frozen=True)
class FeaturePlanner:
    """Create implementation plans by prompting an LLM for structured JSON output."""

    llm_client: LLMClient
    enforce_atomic_node_window: bool = True

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
        return self._parse_plan_response(
            response,
            enforce_atomic_node_window=(
                self.enforce_atomic_node_window
                and self._should_enforce_atomic_node_window(
                    requirement=requirement_clean,
                    codebase_summary=summary_clean,
                )
            ),
        )

    @staticmethod
    def _build_prompt(*, requirement: str, codebase_summary: str) -> str:
        return (
            "Role: Chief Architect for an autonomous software engineering agent.\n"
            "Task: Decompose the requested feature into a graph-based implementation plan.\n"
            "Execution rules:\n"
            "- Prefer a dependency graph with 10-20 atomic nodes for large requests.\n"
            "- Mark API/interface changes as contract nodes.\n"
            "- Contract nodes must be parents of implementer nodes.\n"
            "- Nodes in parallel must not share write ownership of the same file.\n"
            "- Include shared_resources if a node uses singleton resources.\n"
            "- Provide global validation commands for transaction-level verification.\n"
            f"{_JSON_OBJECT_PROMPT_SUFFIX}\n\n"
            "JSON schema:\n"
            "{\n"
            '  "feature_name": "string",\n'
            '  "summary": "string",\n'
            '  "design_guidance": "string",\n'
            '  "validation_commands": ["string"],\n'
            '  "dependency_graph": {\n'
            '    "feature_name": "string",\n'
            '    "summary": "string",\n'
            '    "global_validation_commands": ["string"],\n'
            '    "nodes": [\n'
            "      {\n"
            '        "node_id": "string",\n'
            '        "title": "string",\n'
            '        "summary": "string",\n'
            '        "new_files": ["string"],\n'
            '        "modified_files": ["string"],\n'
            '        "steps": ["string"],\n'
            '        "validation_commands": ["string"],\n'
            '        "depends_on": ["node_id"],\n'
            '        "contract_node": false,\n'
            '        "shared_resources": ["string"]\n'
            "      }\n"
            "    ]\n"
            "  }\n"
            "}\n\n"
            "Backwards compatibility:\n"
            "- If graph decomposition is excessive for the task size, a flat plan "
            "with new_files/modified_files/steps is allowed.\n\n"
            "Requirement:\n"
            f"{requirement}\n\n"
            "Codebase Summary:\n"
            f"{codebase_summary}\n"
        )

    @staticmethod
    def _should_enforce_atomic_node_window(*, requirement: str, codebase_summary: str) -> bool:
        combined = f"{requirement}\n{codebase_summary}".lower()
        if any(marker in combined for marker in _SUBTASK_CONTEXT_MARKERS):
            return False
        if re.search(r"\bsubtask\s+\d+\s*/\s*\d+\b", combined):
            return False
        token_count = len(re.findall(r"[a-z0-9_]+", combined))
        if token_count >= _LARGE_REQUEST_MIN_TOKENS:
            return True
        hint_matches = sum(1 for hint in _LARGE_REQUEST_HINTS if hint in combined)
        return hint_matches >= _MIN_LARGE_REQUEST_HINT_MATCHES

    @staticmethod
    def _parse_plan_response(
        response: str,
        *,
        enforce_atomic_node_window: bool = False,
    ) -> ImplementationPlan:
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

        plan = ImplementationPlan.from_dict(payload)
        total_file_changes = len(set(plan.new_files)) + len(set(plan.modified_files))
        if total_file_changes > _MAX_PLANNED_FILE_CHANGES:
            raise ValueError(
                "LLM feature plan exceeds safe file-change limit: "
                f"{total_file_changes} files (max {_MAX_PLANNED_FILE_CHANGES})."
            )
        if enforce_atomic_node_window:
            dependency_graph = plan.dependency_graph
            if dependency_graph is None:
                raise ValueError(
                    "Large request planning requires a dependency_graph with 10-20 atomic nodes."
                )
            node_count = len(dependency_graph.nodes)
            if not (_MIN_ATOMIC_GRAPH_NODES <= node_count <= _MAX_ATOMIC_GRAPH_NODES):
                raise ValueError(
                    "Large request dependency graph must contain 10-20 atomic nodes: "
                    f"received {node_count}."
                )
        return plan
