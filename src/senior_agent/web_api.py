from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import logging
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

from senior_agent.engine import create_default_senior_agent
from senior_agent.llm_client import CodexCLIClient, GeminiCLIClient, LLMClient, LLMClientError
from senior_agent.orchestrator import MultiAgentOrchestrator
from senior_agent.patterns import CODE_FENCE_PATTERN
from senior_agent.planner import FeaturePlanner
from senior_agent.utils import is_within_workspace

logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE = "."
_DEFAULT_PROVIDER = "gemini"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_API_KEY_ENV_NAME = "SENIOR_AGENT_API_KEY"
_MAX_JOBS = 200
_MAX_STATUS_JOBS = 25
_MAX_PROGRAM_PHASES = 12
_DEFAULT_PROGRAM_PHASES = 6
_DEFAULT_MAX_SUBTASKS_PER_PHASE = 6
_MAX_SUBTASKS_PER_PHASE = 20
_MAX_PHASE_REQUIREMENT_CHARS = 12000
_REPORTS_DIR_NAME = "AgentReports"
_PROJECTS_DIR_NAME = "Projects"
_PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECTION_HEADER_PATTERN = re.compile(r"(?m)^\s*(\d{1,2})\.\s+([^\n]+?)\s*$")
_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)(.+\S)\s*$")
_EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
}
_ARTIFACT_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".mermaid",
    ".py",
    ".ts",
    ".js",
    ".jsx",
    ".tsx",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".log",
}


class ExecuteRequest(BaseModel):
    requirement: str
    workspace: str | None = None
    codebase_summary: str | None = None


class ProgramExecuteRequest(BaseModel):
    requirement: str
    workspace: str | None = None
    codebase_summary: str | None = None
    max_phases: int = Field(default=_DEFAULT_PROGRAM_PHASES, ge=1, le=_MAX_PROGRAM_PHASES)
    max_subtasks_per_phase: int = Field(
        default=_DEFAULT_MAX_SUBTASKS_PER_PHASE,
        ge=1,
        le=_MAX_SUBTASKS_PER_PHASE,
    )


class OpenProjectRequest(BaseModel):
    workspace: str | None = None


class HealRequest(BaseModel):
    command: str
    workspace: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=20)
    validation_commands: list[str] | None = None


class CreateProjectRequest(BaseModel):
    project_name: str


@dataclass
class ExecutionJob:
    job_id: str
    job_type: str
    workspace: Path
    payload: dict[str, Any]
    status: str = "queued"
    success: bool | None = None
    mermaid_path: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    cancel_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result_payload = self.result if isinstance(self.result, dict) else {}
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "status": self.status,
            "success": self.success,
            "workspace": str(self.workspace),
            "payload": self.payload,
            "mermaid_path": self.mermaid_path,
            "error": self.error,
            "result": self.result,
            "progress": _compute_job_progress(self),
            "created_files": _collect_created_files(result_payload),
            "cancel_requested": self.cancel_requested,
            "cancel_reason": self.cancel_reason,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _collect_created_files(result_payload: dict[str, Any]) -> list[str]:
    created: set[str] = set()

    def collect(candidate: Any) -> None:
        if not isinstance(candidate, str):
            return
        normalized = candidate.strip()
        if not normalized:
            return
        created.add(normalized)

    for key in (
        "reports_dir",
        "master_requirement_file",
        "workspace_summary_file",
        "product_spec_file",
        "task_plan_file",
        "summary_file",
        "mermaid_path",
    ):
        collect(result_payload.get(key))

    phase_results = result_payload.get("phase_results")
    if isinstance(phase_results, list):
        for item in phase_results:
            if not isinstance(item, dict):
                continue
            for key in (
                "mermaid_path",
                "requirement_file",
                "result_file",
                "review_file",
                "subtask_plan_prompt_file",
                "subtask_plan_response_file",
            ):
                collect(item.get(key))
            prompt_files = item.get("subtask_prompt_files")
            if isinstance(prompt_files, list):
                for prompt_file in prompt_files:
                    collect(prompt_file)

    post_self_heal = result_payload.get("post_self_heal")
    if isinstance(post_self_heal, dict):
        for key in ("request_file", "report_file", "review_file"):
            collect(post_self_heal.get(key))

    return sorted(created)


def _compute_job_progress(job: ExecutionJob) -> dict[str, Any]:
    if job.status == "cancelled":
        return {
            "percent": 100,
            "steps_completed": 1,
            "steps_total": 1,
            "active_hook": "Cancelled",
            "next_hook": "Stopped by user request",
        }
    if job.status == "queued":
        return {
            "percent": 0,
            "steps_completed": 0,
            "steps_total": 1,
            "active_hook": "Queued",
            "next_hook": "Waiting for worker",
        }
    if job.status == "waiting":
        return {
            "percent": 2,
            "steps_completed": 0,
            "steps_total": 1,
            "active_hook": "Waiting",
            "next_hook": "Running soon",
        }

    result_payload = job.result if isinstance(job.result, dict) else {}

    if job.job_type == "execute_program":
        phase_total = int(result_payload.get("phase_total") or 0)
        if phase_total <= 0:
            phase_total = max(1, int(job.payload.get("max_phases", 1)))

        steps_total = phase_total + 1  # Includes post-run self-heal
        phase_results = result_payload.get("phase_results")
        completed_phases = len(phase_results) if isinstance(phase_results, list) else 0

        stage = str(result_payload.get("stage") or "").strip().lower()
        active_hook = "Planning"
        next_hook = f"Phase 1/{phase_total}"
        steps_completed = min(completed_phases, phase_total)

        if stage == "self_heal":
            active_hook = "Post-run self-heal"
            next_hook = "Finalize program summary"
            steps_completed = phase_total
        else:
            phase_current = int(result_payload.get("phase_current") or 0)
            subtask_current = int(result_payload.get("subtask_current") or 0)
            subtask_total = int(result_payload.get("subtask_total") or 0)
            if phase_current > 0 and phase_total > 0:
                if subtask_total > 0:
                    current = subtask_current if subtask_current > 0 else 0
                    active_hook = (
                        f"Phase {phase_current}/{phase_total} "
                        f"- Subtask {current}/{subtask_total}"
                    )
                else:
                    active_hook = f"Phase {phase_current}/{phase_total}"
                next_phase = min(phase_total, phase_current + 1)
                next_hook = (
                    "Post-run self-heal"
                    if phase_current >= phase_total
                    else f"Phase {next_phase}/{phase_total}"
                )
                steps_completed = max(0, min(phase_current - 1, phase_total))
            elif completed_phases > 0:
                active_hook = f"Phase {min(completed_phases, phase_total)}/{phase_total} completed"
                next_hook = (
                    "Post-run self-heal"
                    if completed_phases >= phase_total
                    else f"Phase {completed_phases + 1}/{phase_total}"
                )
                steps_completed = min(completed_phases, phase_total)

        if result_payload.get("post_self_heal") is not None:
            steps_completed = steps_total
            active_hook = "Completed"
            next_hook = "Done"

        if job.status in {"succeeded", "failed"}:
            steps_completed = steps_total
            active_hook = "Completed" if job.status == "succeeded" else "Failed"
            next_hook = "Done"

        percent = int(round((steps_completed / max(1, steps_total)) * 100))
        if job.status == "running" and percent < 5:
            percent = 5
        return {
            "percent": max(0, min(100, percent)),
            "steps_completed": steps_completed,
            "steps_total": steps_total,
            "active_hook": active_hook,
            "next_hook": next_hook,
        }

    if job.job_type == "execute_feature":
        if job.status == "running":
            return {
                "percent": 66,
                "steps_completed": 2,
                "steps_total": 3,
                "active_hook": "Orchestrator execution",
                "next_hook": "Finalize and report",
            }
        if job.status in {"succeeded", "failed"}:
            return {
                "percent": 100,
                "steps_completed": 3,
                "steps_total": 3,
                "active_hook": "Completed" if job.status == "succeeded" else "Failed",
                "next_hook": "Done",
            }
        return {
            "percent": 10,
            "steps_completed": 0,
            "steps_total": 3,
            "active_hook": "Preparing execution",
            "next_hook": "Run orchestrator",
        }

    if job.job_type == "self_heal":
        max_attempts = max(1, int(job.payload.get("max_attempts", 1)))
        if job.status == "running":
            return {
                "percent": 50,
                "steps_completed": max_attempts // 2,
                "steps_total": max_attempts,
                "active_hook": "Applying fixes",
                "next_hook": "Re-run validation command",
            }
        if job.status in {"succeeded", "failed"}:
            return {
                "percent": 100,
                "steps_completed": max_attempts,
                "steps_total": max_attempts,
                "active_hook": "Completed" if job.status == "succeeded" else "Failed",
                "next_hook": "Done",
            }
        return {
            "percent": 5,
            "steps_completed": 0,
            "steps_total": max_attempts,
            "active_hook": "Preparing self-heal",
            "next_hook": "Execute command",
        }

    if job.status in {"succeeded", "failed"}:
        return {
            "percent": 100,
            "steps_completed": 1,
            "steps_total": 1,
            "active_hook": "Completed" if job.status == "succeeded" else "Failed",
            "next_hook": "Done",
        }
    return {
        "percent": 35 if job.status == "running" else 5,
        "steps_completed": 0,
        "steps_total": 1,
        "active_hook": "Running" if job.status == "running" else "Waiting",
        "next_hook": "Done",
    }


def _workspace_display_name(*, base_workspace: Path, workspace: Path) -> str:
    if workspace == base_workspace:
        return "Repository Root"
    try:
        relative = workspace.relative_to(base_workspace)
    except ValueError:
        return workspace.name or str(workspace)

    parts = relative.parts
    if len(parts) >= 2 and parts[0] == _PROJECTS_DIR_NAME:
        return parts[1]
    return parts[-1] if parts else workspace.name


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_llm_client(*, provider: str, workspace: Path) -> LLMClient:
    provider_normalized = provider.lower()
    if provider_normalized == "codex":
        return CodexCLIClient(workspace=workspace)
    if provider_normalized == "gemini":
        return GeminiCLIClient(workspace=workspace)
    raise ValueError(f"Unsupported provider: {provider}")


def _resolve_role_provider_map(preferred_provider: str) -> dict[str, str]:
    available = {
        "gemini": shutil.which("gemini") is not None,
        "codex": shutil.which("codex") is not None,
    }
    missing = [provider for provider, is_ready in available.items() if not is_ready]
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(
            "Dual-agent mode requires both CLI providers. "
            f"Missing binaries: {missing_list}. "
            "Install both `gemini` and `codex`, then restart the server."
        )

    return {
        "architect": "gemini",
        "developer": "codex",
    }


def _is_local_bind_host(host: str) -> bool:
    normalized = host.strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def _require_api_key(request: Request, app: FastAPI) -> None:
    expected_api_key = getattr(app.state, "api_key", None)
    if not expected_api_key:
        return
    provided_api_key = request.headers.get("x-api-key", "")
    if provided_api_key != expected_api_key:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: provide a valid X-API-Key header.",
        )


def _build_orchestrator(*, workspace: Path, role_provider_map: dict[str, str]) -> MultiAgentOrchestrator:
    architect_provider = role_provider_map.get("architect", "gemini")
    developer_provider = role_provider_map.get("developer", "codex")

    architect_client = _build_llm_client(provider=architect_provider, workspace=workspace)
    developer_client = _build_llm_client(provider=developer_provider, workspace=workspace)
    planner = FeaturePlanner(llm_client=architect_client)
    return MultiAgentOrchestrator(
        llm_client=developer_client,
        planner=planner,
        architect_llm_client=architect_client,
        reviewer_llm_client=architect_client,
    )


def _format_role_provider_label(role_provider_map: dict[str, str]) -> str:
    architect = role_provider_map.get("architect", "gemini")
    developer = role_provider_map.get("developer", "codex")
    return f"dual | architect={architect} | developer={developer}"


def _iter_workspace_files(workspace: Path, max_files: int = 1500) -> tuple[int, dict[str, int]]:
    extension_counts: dict[str, int] = {}
    scanned = 0
    for root, dirs, files in os.walk(workspace, topdown=True):
        dirs[:] = [directory for directory in dirs if directory not in _EXCLUDE_DIRS]
        root_path = Path(root)
        for file_name in files:
            if scanned >= max_files:
                return scanned, extension_counts

            candidate = (root_path / file_name).resolve()
            if not is_within_workspace(workspace, candidate):
                continue
            scanned += 1
            suffix = candidate.suffix.lower() or "[no_ext]"
            extension_counts[suffix] = extension_counts.get(suffix, 0) + 1
    return scanned, extension_counts


def _read_readme_snippet(workspace: Path, max_chars: int = 2000) -> str:
    readme_candidates = (
        workspace / "README.md",
        workspace / "README.rst",
        workspace / "README.txt",
    )
    for candidate in readme_candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        return content[:max_chars].strip()
    return ""


def _build_codebase_summary(workspace: Path) -> str:
    scanned, extension_counts = _iter_workspace_files(workspace)
    sorted_extensions = sorted(
        extension_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )
    extension_section = ", ".join(
        f"{extension}:{count}" for extension, count in sorted_extensions[:10]
    )
    if not extension_section:
        extension_section = "No files detected."

    readme_snippet = _read_readme_snippet(workspace)
    if not readme_snippet:
        readme_snippet = "README not found."

    return (
        f"Workspace: {workspace}\n"
        f"Scanned files: {scanned}\n"
        f"Top file types: {extension_section}\n"
        f"README snippet:\n{readme_snippet}"
    )


def _split_program_requirement(
    requirement: str,
    *,
    max_phases: int = _DEFAULT_PROGRAM_PHASES,
) -> list[str]:
    requirement_clean = requirement.strip()
    if not requirement_clean:
        return []

    capped_max_phases = max(1, min(max_phases, _MAX_PROGRAM_PHASES))
    matches = list(_SECTION_HEADER_PATTERN.finditer(requirement_clean))
    if not matches:
        return [requirement_clean]

    preamble = requirement_clean[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(requirement_clean)
        heading = f"{match.group(1)}. {match.group(2).strip()}"
        section_text = requirement_clean[start:end].strip()
        sections.append((heading, section_text))

    phase_total = min(capped_max_phases, len(sections))
    base_size = len(sections) // phase_total
    remainder = len(sections) % phase_total

    phases: list[str] = []
    cursor = 0
    for phase_index in range(phase_total):
        group_size = base_size + (1 if phase_index < remainder else 0)
        grouped_sections = sections[cursor : cursor + group_size]
        cursor += group_size

        section_headings = ", ".join(heading for heading, _ in grouped_sections[:3])
        if len(grouped_sections) > 3:
            section_headings = f"{section_headings}, ..."

        preamble_block = ""
        if preamble:
            preamble_excerpt = preamble[:1200]
            preamble_block = f"Global Context:\n{preamble_excerpt}\n\n"

        phase_requirement = (
            f"{preamble_block}"
            f"Program delivery phase {phase_index + 1}/{phase_total}.\n"
            "Implement this phase completely while preserving behavior from prior phases.\n"
            f"Focus sections: {section_headings}\n\n"
            f"{'\n\n'.join(section_text for _, section_text in grouped_sections)}\n"
        ).strip()

        if len(phase_requirement) > _MAX_PHASE_REQUIREMENT_CHARS:
            phase_requirement = (
                f"{phase_requirement[:_MAX_PHASE_REQUIREMENT_CHARS]}\n\n"
                "[Truncated for token safety. Preserve section intent.]"
            )

        phases.append(phase_requirement)

    return phases


def _extract_json_candidate(raw_text: str) -> str:
    stripped = raw_text.strip()
    fence_match = CODE_FENCE_PATTERN.search(stripped)
    if fence_match:
        return fence_match.group("code").strip()
    return stripped


def _parse_json_object(raw_text: str) -> dict[str, Any] | None:
    candidate = _extract_json_candidate(raw_text)
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _build_product_spec_prompt(requirement: str) -> str:
    return (
        "Role: Senior Product Architect.\n"
        "Task: Convert the requirement into a strict machine-readable product spec.\n"
        "Return ONLY one JSON object with this schema:\n"
        "{\n"
        '  "product_name": "string",\n'
        '  "summary": "string",\n'
        '  "personas": ["string"],\n'
        '  "features": ["string"],\n'
        '  "constraints": ["string"],\n'
        '  "accessibility_requirements": ["string"],\n'
        '  "security_requirements": ["string"],\n'
        '  "acceptance_criteria": ["string"],\n'
        '  "validation_commands": ["string"]\n'
        "}\n\n"
        "Requirement:\n"
        f"{requirement}\n"
    )


def _build_task_plan_prompt(product_spec_text: str, max_phases: int) -> str:
    return (
        "Role: Senior Delivery Planner.\n"
        "Task: Decompose the product spec into an ordered execution plan.\n"
        "Return ONLY one JSON object with this schema:\n"
        "{\n"
        '  "feature_name": "string",\n'
        '  "summary": "string",\n'
        '  "tasks": [\n'
        "    {\n"
        '      "id": "T1",\n'
        '      "title": "string",\n'
        '      "requirement": "string",\n'
        '      "depends_on": ["task id"],\n'
        '      "validation_commands": ["string"]\n'
        "    }\n"
        "  ]\n"
        "}\n"
        f"Limit to at most {max_phases} tasks.\n\n"
        "Product Spec:\n"
        f"{product_spec_text}\n"
    )


def _build_subtask_plan_prompt(phase_requirement: str, max_subtasks: int) -> str:
    return (
        "Role: Senior Architect.\n"
        "Task: Break this phase requirement into small, executable subtasks.\n"
        "Return ONLY one JSON object with this schema:\n"
        "{\n"
        '  "subtasks": [\n'
        "    {\n"
        '      "title": "string",\n'
        '      "requirement": "string"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        f"Limit to at most {max_subtasks} subtasks.\n"
        "Each subtask must be independently deliverable in a short run.\n\n"
        "Phase Requirement:\n"
        f"{phase_requirement}\n"
    )


def _build_review_prompt(
    *,
    phase_number: int,
    phase_total: int,
    phase_requirement: str,
    phase_success: bool,
    mermaid_path: str | None,
) -> str:
    return (
        "Role: Senior Code Reviewer.\n"
        "Task: Write a concise implementation review for this phase.\n"
        "Use markdown with sections: Outcome, Risks, Recommended Next Step.\n\n"
        f"Phase: {phase_number}/{phase_total}\n"
        f"Success: {phase_success}\n"
        f"Mermaid Artifact: {mermaid_path or 'none'}\n\n"
        "Phase Requirement:\n"
        f"{phase_requirement}\n"
    )


def _default_task_plan(requirement: str, max_phases: int) -> dict[str, Any]:
    phases = _split_program_requirement(requirement, max_phases=max_phases)
    tasks = [
        {
            "id": f"T{index + 1}",
            "title": f"Phase {index + 1}",
            "requirement": phase_text,
            "depends_on": [f"T{index}"] if index > 0 else [],
            "validation_commands": [],
        }
        for index, phase_text in enumerate(phases)
    ]
    return {
        "feature_name": "Program Execution",
        "summary": "Fallback task plan derived from requirement sections.",
        "tasks": tasks,
    }


def _derive_phase_requirements_from_plan(
    plan_payload: dict[str, Any],
    *,
    fallback_requirement: str,
    max_phases: int,
) -> list[str]:
    tasks_raw = plan_payload.get("tasks")
    if not isinstance(tasks_raw, list):
        return _split_program_requirement(fallback_requirement, max_phases=max_phases)

    phase_requirements: list[str] = []
    for task in tasks_raw[:max_phases]:
        if not isinstance(task, dict):
            continue
        requirement_text = str(task.get("requirement") or "").strip()
        title = str(task.get("title") or "").strip()
        task_id = str(task.get("id") or "").strip()
        if not requirement_text:
            continue
        prefix_parts = [part for part in [task_id, title] if part]
        prefix = " - ".join(prefix_parts)
        phase_requirements.append(
            (
                f"Task {prefix}\n{requirement_text}"
                if prefix
                else requirement_text
            )
        )

    if phase_requirements:
        return phase_requirements
    return _split_program_requirement(fallback_requirement, max_phases=max_phases)


def _split_phase_requirement_into_subtasks(
    phase_requirement: str,
    *,
    max_subtasks: int = _DEFAULT_MAX_SUBTASKS_PER_PHASE,
) -> list[str]:
    requirement = phase_requirement.strip()
    if not requirement:
        return []

    capped = max(1, min(max_subtasks, _MAX_SUBTASKS_PER_PHASE))
    lines = [line.strip() for line in requirement.splitlines() if line.strip()]
    if not lines:
        return [requirement]

    list_items: list[str] = []
    for line in lines:
        match = _LIST_ITEM_PATTERN.match(line)
        if not match:
            continue
        item = match.group(1).strip()
        if len(item) >= 8:
            list_items.append(item)

    if not list_items:
        paragraph_items = [
            part.strip()
            for part in re.split(r"\n\s*\n", requirement)
            if part.strip()
        ]
        list_items = [item for item in paragraph_items if len(item) >= 16]

    if not list_items:
        return [requirement]

    unique_items: list[str] = []
    seen: set[str] = set()
    for item in list_items:
        if item in seen:
            continue
        seen.add(item)
        unique_items.append(item)
        if len(unique_items) >= capped:
            break

    if not unique_items:
        return [requirement]

    phase_context = lines[0][:180]
    total = len(unique_items)
    subtasks = [
        (
            f"Phase context: {phase_context}\n"
            f"Subtask {index}/{total}:\n{item}\n"
            "Keep changes focused and small for this subtask only."
        )
        for index, item in enumerate(unique_items, start=1)
    ]
    return subtasks


def _derive_subtasks_with_architect(
    *,
    architect_client: LLMClient,
    phase_requirement: str,
    max_subtasks: int,
) -> tuple[list[str], str, str | None, str]:
    fallback_subtasks = _split_phase_requirement_into_subtasks(
        phase_requirement,
        max_subtasks=max_subtasks,
    )
    prompt = _build_subtask_plan_prompt(phase_requirement, max_subtasks)
    try:
        raw_response = architect_client.generate_fix(prompt)
    except LLMClientError as exc:
        return fallback_subtasks, prompt, None, f"fallback_llm_error:{exc}"

    payload = _parse_json_object(raw_response)
    if not isinstance(payload, dict):
        return fallback_subtasks, prompt, raw_response, "fallback_invalid_json"

    subtasks_raw = payload.get("subtasks")
    if not isinstance(subtasks_raw, list):
        return fallback_subtasks, prompt, raw_response, "fallback_missing_subtasks"

    parsed_subtasks: list[str] = []
    seen: set[str] = set()
    for item in subtasks_raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        requirement = str(item.get("requirement") or "").strip()
        text = requirement or title
        if len(text) < 8:
            continue
        if text in seen:
            continue
        seen.add(text)
        if title and requirement:
            parsed_subtasks.append(f"{title}\n{requirement}")
        else:
            parsed_subtasks.append(text)
        if len(parsed_subtasks) >= max_subtasks:
            break

    if not parsed_subtasks:
        return fallback_subtasks, prompt, raw_response, "fallback_empty_subtasks"

    phase_header = phase_requirement.strip().splitlines()[0][:180]
    total = len(parsed_subtasks)
    subtasks = [
        (
            f"Phase context: {phase_header}\n"
            f"Subtask {index}/{total}:\n{entry}\n"
            "Keep changes focused and small for this subtask only."
        )
        for index, entry in enumerate(parsed_subtasks, start=1)
    ]
    return subtasks, prompt, raw_response, "architect"


def _sanitize_command_list(raw_values: list[Any]) -> list[str]:
    seen: set[str] = set()
    commands: list[str] = []
    for raw in raw_values:
        command = str(raw).strip()
        if not command:
            continue
        if command in seen:
            continue
        seen.add(command)
        commands.append(command)
    return commands


def _collect_validation_commands(
    product_spec_payload: dict[str, Any],
    task_plan_payload: dict[str, Any],
) -> list[str]:
    candidates: list[Any] = []
    product_commands = product_spec_payload.get("validation_commands")
    if isinstance(product_commands, list):
        candidates.extend(product_commands)

    tasks_raw = task_plan_payload.get("tasks")
    if isinstance(tasks_raw, list):
        for task in tasks_raw:
            if not isinstance(task, dict):
                continue
            task_commands = task.get("validation_commands")
            if isinstance(task_commands, list):
                candidates.extend(task_commands)

    return _sanitize_command_list(candidates)


def _autodetect_validation_commands(workspace: Path) -> list[str]:
    commands: list[str] = []

    package_json = workspace / "package.json"
    if package_json.exists() and package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if isinstance(scripts, dict):
            if isinstance(scripts.get("lint"), str):
                commands.append("npm run lint")
            if isinstance(scripts.get("test"), str):
                commands.append("npm test")

    if (workspace / "go.mod").exists():
        commands.append("go test ./...")
    if (workspace / "Cargo.toml").exists():
        commands.append("cargo test")
    if (workspace / "pyproject.toml").exists() or (workspace / "requirements.txt").exists():
        commands.append("python -m unittest discover -s tests -v")

    if not commands:
        commands.append("python -m unittest discover -s tests -v")
    return _sanitize_command_list(list(commands))


def _select_post_heal_commands(
    *,
    workspace: Path,
    product_spec_payload: dict[str, Any],
    task_plan_payload: dict[str, Any],
) -> tuple[str, list[str], str]:
    commands = _collect_validation_commands(product_spec_payload, task_plan_payload)
    source = "spec_and_plan_validation_commands"
    if not commands:
        commands = _autodetect_validation_commands(workspace)
        source = "workspace_autodetect"

    primary = commands[0]
    validations = commands[1:]
    return primary, validations, source


def _prepare_report_directory(workspace: Path, job_id: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = (workspace / _REPORTS_DIR_NAME / f"program_{timestamp}_{job_id[:8]}").resolve()
    if not is_within_workspace(workspace, report_dir):
        raise ValueError(f"Report directory resolved outside workspace: {report_dir}")
    report_dir.mkdir(parents=True, exist_ok=False)
    return report_dir


def _write_report_text(
    *,
    workspace: Path,
    report_dir: Path,
    relative_name: str,
    content: str,
) -> str | None:
    target = (report_dir / relative_name).resolve()
    if not is_within_workspace(workspace, target):
        logger.error("Blocked report write outside workspace: %s", target)
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.error("Failed writing report file %s: %s", target, exc)
        return None
    return target.relative_to(workspace).as_posix()


def _write_report_json(
    *,
    workspace: Path,
    report_dir: Path,
    relative_name: str,
    payload: dict[str, Any],
) -> str | None:
    return _write_report_text(
        workspace=workspace,
        report_dir=report_dir,
        relative_name=relative_name,
        content=f"{json.dumps(payload, indent=2, ensure_ascii=True)}\n",
    )


def _find_latest_mermaid_path(workspace: Path, baseline: set[Path]) -> str | None:
    current = {
        candidate.resolve()
        for candidate in workspace.glob("*.mermaid")
        if candidate.is_file()
    }
    candidates = [path for path in current if is_within_workspace(workspace, path)]
    if not candidates:
        return None

    new_paths = [path for path in candidates if path not in baseline]
    target_pool = new_paths if new_paths else candidates
    try:
        target = max(target_pool, key=lambda path: path.stat().st_mtime)
    except OSError:
        return None
    return target.relative_to(workspace).as_posix()


def _prune_jobs(app: FastAPI) -> None:
    jobs: dict[str, ExecutionJob] = app.state.jobs
    if len(jobs) <= _MAX_JOBS:
        return
    sorted_jobs = sorted(
        jobs.values(),
        key=lambda item: item.created_at,
    )
    overflow = len(jobs) - _MAX_JOBS
    for job in sorted_jobs[:overflow]:
        jobs.pop(job.job_id, None)


def _build_job_response(job: ExecutionJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "status": job.status,
        "success": job.success,
        "mermaid_path": job.mermaid_path,
        "status_url": f"/api/status?job_id={job.job_id}",
        "created_at": job.created_at,
        "queued": job.status in {"queued", "waiting", "running"},
    }


def _enqueue_job_execution(
    *,
    app: FastAPI,
    background_tasks: BackgroundTasks,
    job: ExecutionJob,
) -> None:
    if job.job_type == "execute_feature":
        background_tasks.add_task(_execute_feature_job, app, job.job_id)
        return
    if job.job_type == "execute_program":
        background_tasks.add_task(_execute_program_job, app, job.job_id)
        return
    if job.job_type == "self_heal":
        background_tasks.add_task(_execute_heal_job, app, job.job_id)
        return
    raise HTTPException(status_code=400, detail=f"Unsupported job type: {job.job_type}")


def _cancel_job(
    *,
    app: FastAPI,
    job_id: str,
    reason: str,
) -> ExecutionJob:
    with app.state.jobs_lock:
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

        if job.status in {"succeeded", "failed", "cancelled"}:
            return job

        job.cancel_requested = True
        job.cancel_reason = reason

        if job.status in {"queued", "waiting"}:
            if not isinstance(job.result, dict):
                job.result = {}
            job.result["cancelled"] = True
            job.result["cancel_reason"] = reason
            job.success = False
            job.status = "cancelled"
            job.error = reason
            job.finished_at = _utc_now_iso()
        return job


def _is_job_cancel_requested(app: FastAPI, job_id: str) -> bool:
    with app.state.jobs_lock:
        job = app.state.jobs.get(job_id)
        if job is None:
            return False
        return bool(job.cancel_requested)


def _finalize_job_cancelled(
    *,
    app: FastAPI,
    job_id: str,
    message: str,
) -> bool:
    with app.state.jobs_lock:
        job = app.state.jobs.get(job_id)
        if job is None:
            return False

        if job.status == "cancelled":
            return True

        if not isinstance(job.result, dict):
            job.result = {}
        job.result["cancelled"] = True
        job.result["cancel_reason"] = message
        job.success = False
        job.status = "cancelled"
        job.error = message
        job.finished_at = _utc_now_iso()
        return True


def _build_retry_job(*, previous: ExecutionJob) -> ExecutionJob:
    return ExecutionJob(
        job_id=uuid4().hex,
        job_type=previous.job_type,
        workspace=previous.workspace,
        payload=deepcopy(previous.payload),
        status="queued",
        success=None,
        created_at=_utc_now_iso(),
    )


def _is_likely_text_artifact(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _ARTIFACT_TEXT_SUFFIXES:
        return True
    if not suffix:
        return False
    return suffix.startswith(".phase")


def _find_latest_artifact_file(
    *,
    workspace: Path,
    preferred_reports_dir: str | None = None,
) -> Path | None:
    reports_root = (workspace / _REPORTS_DIR_NAME).resolve()
    if not reports_root.exists() or not reports_root.is_dir():
        return None
    if not is_within_workspace(workspace, reports_root):
        return None

    search_roots: list[Path] = []
    if preferred_reports_dir:
        preferred = (workspace / preferred_reports_dir).resolve()
        if (
            preferred.exists()
            and preferred.is_dir()
            and is_within_workspace(workspace, preferred)
        ):
            search_roots.append(preferred)
    search_roots.append(reports_root)

    best_path: Path | None = None
    best_mtime: float = -1.0
    for root in search_roots:
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            if not is_within_workspace(workspace, candidate):
                continue
            if not _is_likely_text_artifact(candidate):
                continue
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best_path = candidate
    return best_path


def _tail_file_text(path: Path, *, lines: int = 80, max_bytes: int = 128_000) -> str:
    safe_lines = max(1, min(lines, 400))
    try:
        size = path.stat().st_size
    except OSError:
        return ""

    start_pos = max(0, size - max_bytes)
    try:
        with path.open("rb") as handle:
            if start_pos > 0:
                handle.seek(start_pos)
            raw = handle.read()
    except OSError:
        return ""

    decoded = raw.decode("utf-8", errors="replace")
    return "\n".join(decoded.splitlines()[-safe_lines:])


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=True)}\n\n"


def _resolve_workspace(app: FastAPI, raw_workspace: str | None) -> Path:
    base_workspace: Path = app.state.default_workspace
    requested = (raw_workspace or str(base_workspace)).strip()
    if not requested:
        requested = str(base_workspace)

    candidate = Path(requested).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (base_workspace / candidate).resolve()

    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Invalid workspace: {resolved}")
    if not is_within_workspace(base_workspace, resolved):
        raise HTTPException(
            status_code=400,
            detail=f"Workspace must stay within {base_workspace}.",
        )
    return resolved


def _sanitize_project_name(raw_name: str) -> str:
    project_name = raw_name.strip()
    if not project_name:
        raise HTTPException(status_code=400, detail="project_name must not be empty.")
    if not _PROJECT_NAME_PATTERN.fullmatch(project_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "project_name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}. "
                "Spaces are not allowed."
            ),
        )
    return project_name


def _projects_root(base_workspace: Path) -> Path:
    root = (base_workspace / _PROJECTS_DIR_NAME).resolve()
    if not is_within_workspace(base_workspace, root):
        raise HTTPException(status_code=500, detail="Invalid projects root configuration.")
    return root


def _list_projects(base_workspace: Path) -> list[dict[str, str]]:
    projects_dir = _projects_root(base_workspace)
    if not projects_dir.exists():
        return []

    entries: list[dict[str, str]] = []
    for candidate in sorted(projects_dir.iterdir(), key=lambda item: item.name.lower()):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        if not is_within_workspace(base_workspace, resolved):
            continue
        entries.append(
            {
                "name": resolved.name,
                "workspace": str(resolved),
                "relative_path": resolved.relative_to(base_workspace).as_posix(),
            }
        )
    return entries


def _create_project(base_workspace: Path, project_name: str) -> Path:
    clean_name = _sanitize_project_name(project_name)
    projects_dir = _projects_root(base_workspace)
    projects_dir.mkdir(parents=True, exist_ok=True)

    project_dir = (projects_dir / clean_name).resolve()
    if not is_within_workspace(base_workspace, project_dir):
        raise HTTPException(status_code=400, detail="Resolved project path is outside workspace.")
    if project_dir.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Project already exists: {project_dir.relative_to(base_workspace)}",
        )

    (project_dir / "src").mkdir(parents=True, exist_ok=False)
    (project_dir / "tests").mkdir(parents=True, exist_ok=False)
    readme_path = project_dir / "README.md"
    readme_path.write_text(
        (
            f"# {clean_name}\n\n"
            "Created by Senior Agent Control Center.\n\n"
            "## Structure\n"
            "- `src/`\n"
            "- `tests/`\n"
        ),
        encoding="utf-8",
    )
    return project_dir


def _open_directory_in_file_manager(target_dir: Path) -> tuple[bool, str]:
    if not target_dir.exists() or not target_dir.is_dir():
        return False, f"Target directory does not exist: {target_dir}"

    if sys.platform.startswith("darwin"):
        command = ["open", str(target_dir)]
    elif os.name == "nt":
        command = ["explorer", str(target_dir)]
    else:
        command = ["xdg-open", str(target_dir)]

    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return False, f"Unable to open directory with command {command[0]!r}: {exc}"
    return True, f"Opened directory: {target_dir}"


def _system_status(app: FastAPI, workspace: Path | None = None) -> dict[str, Any]:
    jobs = list(app.state.jobs.values())
    if workspace is not None:
        jobs = [job for job in jobs if job.workspace == workspace]

    queue_candidates = sorted(
        (
            job
            for job in jobs
            if job.status in {"queued", "waiting", "running"}
        ),
        key=lambda job: job.created_at,
    )
    queue_positions = {
        job.job_id: index
        for index, job in enumerate(queue_candidates, start=1)
    }

    def with_queue_info(payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        position = queue_positions.get(str(payload.get("job_id")))
        enriched["queue_position"] = position
        enriched["queue_depth"] = len(queue_candidates)
        if position is not None:
            enriched["queued"] = payload.get("status") in {"queued", "waiting", "running"}
        return enriched

    recent_jobs = [
        with_queue_info(item.to_dict())
        for item in sorted(jobs, key=lambda job: job.created_at, reverse=True)[:_MAX_STATUS_JOBS]
    ]
    active_jobs = [job for job in jobs if job.status in {"waiting", "running"}]
    active_jobs_sorted = sorted(active_jobs, key=lambda item: item.created_at, reverse=True)
    active_job_payload = (
        with_queue_info(active_jobs_sorted[0].to_dict())
        if active_jobs_sorted
        else None
    )

    return {
        "status": "ok",
        "provider": app.state.provider_label,
        "preferred_provider": app.state.provider,
        "role_providers": dict(app.state.role_provider_map),
        "default_workspace": str(app.state.default_workspace),
        "workspace": str(workspace) if workspace is not None else None,
        "jobs_total": len(jobs),
        "jobs_waiting": sum(1 for job in jobs if job.status == "waiting"),
        "jobs_running": sum(1 for job in jobs if job.status == "running"),
        "jobs_succeeded": sum(1 for job in jobs if job.status == "succeeded"),
        "jobs_failed": sum(1 for job in jobs if job.status == "failed"),
        "jobs_cancelled": sum(1 for job in jobs if job.status == "cancelled"),
        "jobs_queued": sum(1 for job in jobs if job.status == "queued"),
        "queue_depth": len(queue_candidates),
        "active_job": active_job_payload,
        "recent_jobs": recent_jobs,
    }


def _update_job_result_state(
    *,
    app: FastAPI,
    job_id: str,
    updates: dict[str, Any] | None = None,
    append_hook: str | None = None,
) -> None:
    updates = updates or {}
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

    with app.state.jobs_lock:
        job = app.state.jobs.get(job_id)
        if job is None:
            return

        result_payload: dict[str, Any]
        if isinstance(job.result, dict):
            result_payload = dict(job.result)
        else:
            result_payload = {}

        hooks_raw = result_payload.get("hooks")
        hooks: list[str] = list(hooks_raw) if isinstance(hooks_raw, list) else []
        if append_hook:
            hooks.append(f"{timestamp} | {append_hook}")
            hooks = hooks[-50:]
        if hooks:
            result_payload["hooks"] = hooks

        result_payload.update(updates)
        job.result = result_payload


def _execute_feature_job(app: FastAPI, job_id: str) -> None:
    with app.state.jobs_lock:
        job: ExecutionJob | None = app.state.jobs.get(job_id)
        if job is None:
            return
        job.status = "waiting"
        job.started_at = _utc_now_iso()
    _update_job_result_state(
        app=app,
        job_id=job_id,
        updates={
            "stage": "feature_execution",
            "current_task": "Waiting for execution slot",
        },
        append_hook="Queued feature execution job.",
    )

    if _is_job_cancel_requested(app, job_id):
        _finalize_job_cancelled(
            app=app,
            job_id=job_id,
            message="Cancelled before feature execution started.",
        )
        return

    try:
        baseline_mermaid = {
            candidate.resolve()
            for candidate in job.workspace.glob("*.mermaid")
            if candidate.is_file()
        }
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Preparing orchestrator"},
            append_hook="Collecting workspace baseline and preparing orchestrator.",
        )
        requirement = str(job.payload["requirement"])
        codebase_summary = str(job.payload.get("codebase_summary") or "").strip()
        if not codebase_summary:
            codebase_summary = _build_codebase_summary(job.workspace)

        orchestrator = _build_orchestrator(
            workspace=job.workspace,
            role_provider_map=app.state.role_provider_map,
        )
        with app.state.execution_lock:
            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message="Cancelled before orchestrator run.",
                )
                return
            with app.state.jobs_lock:
                current = app.state.jobs.get(job_id)
                if current is not None:
                    current.status = "running"
            _update_job_result_state(
                app=app,
                job_id=job_id,
                updates={
                    "stage": "feature_execution",
                    "current_task": "Executing feature request",
                },
                append_hook="Running feature implementation and validation.",
            )
            success = orchestrator.execute_feature_request(
                requirement=requirement,
                codebase_summary=codebase_summary,
                workspace=job.workspace,
            )

        if _is_job_cancel_requested(app, job_id):
            _finalize_job_cancelled(
                app=app,
                job_id=job_id,
                message="Cancelled after orchestrator run.",
            )
            return

        mermaid_path = _find_latest_mermaid_path(job.workspace, baseline_mermaid)
        result_payload = {
            "requirement": requirement,
            "success": success,
            "workspace": str(job.workspace),
            "mermaid_path": mermaid_path,
            "role_providers": dict(app.state.role_provider_map),
        }
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Feature job complete"},
            append_hook=(
                "Feature execution completed successfully."
                if success
                else "Feature execution failed."
            ),
        )
        with app.state.jobs_lock:
            existing = app.state.jobs.get(job_id)
            if existing is None:
                return
            existing_result = existing.result if isinstance(existing.result, dict) else {}
            hooks = existing_result.get("hooks")
            if isinstance(hooks, list):
                result_payload["hooks"] = hooks
            existing.success = success
            existing.status = "succeeded" if success else "failed"
            existing.mermaid_path = mermaid_path
            existing.result = result_payload
            existing.finished_at = _utc_now_iso()
    except Exception as exc:  # pragma: no cover - defensive guardrail
        logger.exception("Feature execution job failed unexpectedly: job_id=%s error=%s", job_id, exc)
        _update_job_result_state(
            app=app,
            job_id=job_id,
            append_hook=f"Feature execution crashed: {exc}",
            updates={"current_task": "Feature job failed"},
        )
        with app.state.jobs_lock:
            existing = app.state.jobs.get(job_id)
            if existing is None:
                return
            existing.success = False
            existing.status = "failed"
            existing.error = str(exc)
            existing.finished_at = _utc_now_iso()


def _execute_program_job(app: FastAPI, job_id: str) -> None:
    with app.state.jobs_lock:
        job: ExecutionJob | None = app.state.jobs.get(job_id)
        if job is None:
            return
        job.status = "waiting"
        job.started_at = _utc_now_iso()
    _update_job_result_state(
        app=app,
        job_id=job_id,
        updates={
            "stage": "planning",
            "current_task": "Waiting for execution slot",
        },
        append_hook="Queued program execution job.",
    )

    if _is_job_cancel_requested(app, job_id):
        _finalize_job_cancelled(
            app=app,
            job_id=job_id,
            message="Cancelled before program execution started.",
        )
        return

    try:
        requirement = str(job.payload["requirement"])
        codebase_summary_override = str(job.payload.get("codebase_summary") or "").strip()
        max_phases = int(job.payload.get("max_phases", _DEFAULT_PROGRAM_PHASES))
        max_subtasks_per_phase = int(
            job.payload.get("max_subtasks_per_phase", _DEFAULT_MAX_SUBTASKS_PER_PHASE)
        )
        max_subtasks_per_phase = max(1, min(max_subtasks_per_phase, _MAX_SUBTASKS_PER_PHASE))
        architect_provider = app.state.role_provider_map.get("architect", "gemini")
        architect_client = _build_llm_client(
            provider=architect_provider,
            workspace=job.workspace,
        )
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Preparing planner inputs"},
            append_hook=(
                "Execution slot acquired. "
                f"Architect provider: {architect_provider}, Developer provider: "
                f"{app.state.role_provider_map.get('developer', 'codex')}."
            ),
        )

        if _is_job_cancel_requested(app, job_id):
            _finalize_job_cancelled(
                app=app,
                job_id=job_id,
                message="Cancelled before report preparation.",
            )
            return

        report_dir = _prepare_report_directory(job.workspace, job_id)
        report_dir_relative = report_dir.relative_to(job.workspace).as_posix()
        planning_notes: list[str] = [
            (
                "Dual-agent routing: "
                f"architect={app.state.role_provider_map.get('architect', 'gemini')} "
                f"developer={app.state.role_provider_map.get('developer', 'codex')}"
            )
        ]
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Preparing reports and workspace snapshot"},
            append_hook="Prepared reports directory and workspace summary.",
        )

        master_requirement_file = _write_report_text(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="00_master_requirement.md",
            content=f"{requirement.rstrip()}\n",
        )
        workspace_summary_text = codebase_summary_override or _build_codebase_summary(job.workspace)
        workspace_summary_file = _write_report_text(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="00_workspace_summary.txt",
            content=f"{workspace_summary_text.rstrip()}\n",
        )

        product_spec_prompt = _build_product_spec_prompt(requirement)
        _write_report_text(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="01_product_spec_prompt.txt",
            content=f"{product_spec_prompt.rstrip()}\n",
        )
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Generating product spec"},
            append_hook="Generating product spec with architect model.",
        )
        try:
            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message="Cancelled before product spec generation.",
                )
                return
            product_spec_raw = architect_client.generate_fix(product_spec_prompt)
            _update_job_result_state(
                app=app,
                job_id=job_id,
                append_hook="Product spec generated.",
            )
        except LLMClientError as exc:
            planning_notes.append(f"Product spec generation fallback used: {exc}")
            _update_job_result_state(
                app=app,
                job_id=job_id,
                append_hook=f"Product spec generation failed; using fallback: {exc}",
            )
            product_spec_raw = (
                "{\n"
                '  "product_name": "Program Execution",\n'
                '  "summary": "Fallback product spec generated due to LLM error.",\n'
                '  "personas": [],\n'
                '  "features": [],\n'
                '  "constraints": [],\n'
                '  "accessibility_requirements": [],\n'
                '  "security_requirements": [],\n'
                '  "acceptance_criteria": [],\n'
                '  "validation_commands": []\n'
                "}\n"
            )
        product_spec_response_file = _write_report_text(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="01_product_spec_response.txt",
            content=f"{product_spec_raw.rstrip()}\n",
        )
        product_spec_payload = _parse_json_object(product_spec_raw) or {
            "product_name": "Program Execution",
            "summary": "LLM product spec response was non-JSON; see raw response file.",
            "raw_response_file": product_spec_response_file,
        }
        product_spec_file = _write_report_json(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="01_product_spec.json",
            payload=product_spec_payload,
        )

        product_spec_text_for_planning = json.dumps(product_spec_payload, indent=2, ensure_ascii=True)
        task_plan_prompt = _build_task_plan_prompt(product_spec_text_for_planning, max_phases)
        _write_report_text(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="02_task_plan_prompt.txt",
            content=f"{task_plan_prompt.rstrip()}\n",
        )
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Generating task plan"},
            append_hook="Generating task plan with architect model.",
        )
        try:
            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message="Cancelled before task plan generation.",
                )
                return
            task_plan_raw = architect_client.generate_fix(task_plan_prompt)
            _update_job_result_state(
                app=app,
                job_id=job_id,
                append_hook="Task plan generated.",
            )
        except LLMClientError as exc:
            planning_notes.append(f"Task plan generation fallback used: {exc}")
            _update_job_result_state(
                app=app,
                job_id=job_id,
                append_hook=f"Task plan generation failed; using fallback: {exc}",
            )
            task_plan_raw = ""
        task_plan_response_file = _write_report_text(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="02_task_plan_response.txt",
            content=f"{task_plan_raw.rstrip()}\n",
        )
        task_plan_payload = _parse_json_object(task_plan_raw) or _default_task_plan(
            requirement,
            max_phases,
        )
        if not task_plan_raw.strip():
            task_plan_payload["summary"] = (
                f"{task_plan_payload.get('summary', '')} "
                "Fallback plan generated from requirement splitting."
            ).strip()
        task_plan_payload["raw_response_file"] = task_plan_response_file
        task_plan_file = _write_report_json(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="02_task_plan.json",
            payload=task_plan_payload,
        )

        phases = _derive_phase_requirements_from_plan(
            task_plan_payload,
            fallback_requirement=requirement,
            max_phases=max_phases,
        )
        if not phases:
            raise ValueError("Program requirement must not be empty.")
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": f"Planning complete: {len(phases)} phases"},
            append_hook=f"Planning complete with {len(phases)} phases.",
        )

        recovery_command, recovery_validations, recovery_source = _select_post_heal_commands(
            workspace=job.workspace,
            product_spec_payload=product_spec_payload,
            task_plan_payload=task_plan_payload,
        )

        orchestrator = _build_orchestrator(
            workspace=job.workspace,
            role_provider_map=app.state.role_provider_map,
        )
        phase_results: list[dict[str, Any]] = []
        phase_total = len(phases)
        overall_success = True
        self_heal_summary: dict[str, Any] | None = None

        for phase_index, phase_requirement in enumerate(phases, start=1):
            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message=f"Cancelled before phase {phase_index} started.",
                )
                return

            phase_base_name = f"phases/phase_{phase_index:02d}"
            phase_requirement_file = _write_report_text(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name=f"{phase_base_name}_requirement.md",
                content=f"{phase_requirement.rstrip()}\n",
            )
            _update_job_result_state(
                app=app,
                job_id=job_id,
                updates={"current_task": f"Generating subtasks for phase {phase_index}/{phase_total}"},
                append_hook=f"Requesting Gemini subtask breakdown for phase {phase_index}/{phase_total}.",
            )
            subtasks, subtask_plan_prompt, subtask_plan_response, subtask_plan_source = _derive_subtasks_with_architect(
                architect_client=architect_client,
                phase_requirement=phase_requirement,
                max_subtasks=max_subtasks_per_phase,
            )
            subtask_plan_prompt_file = _write_report_text(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name=f"{phase_base_name}_subtask_plan_prompt.txt",
                content=f"{subtask_plan_prompt.rstrip()}\n",
            )
            subtask_plan_response_file: str | None = None
            if isinstance(subtask_plan_response, str) and subtask_plan_response.strip():
                subtask_plan_response_file = _write_report_text(
                    workspace=job.workspace,
                    report_dir=report_dir,
                    relative_name=f"{phase_base_name}_subtask_plan_response.txt",
                    content=f"{subtask_plan_response.rstrip()}\n",
                )
            _update_job_result_state(
                app=app,
                job_id=job_id,
                updates={
                    "success": None,
                    "stage": "phase_execution",
                    "phase_current": phase_index,
                    "phase_total": phase_total,
                    "subtask_current": 0,
                    "subtask_total": len(subtasks),
                    "reports_dir": report_dir_relative,
                    "product_spec_file": product_spec_file,
                    "task_plan_file": task_plan_file,
                    "phase_results": list(phase_results),
                    "current_task": f"Phase {phase_index}/{phase_total} ready ({len(subtasks)} subtasks)",
                },
                append_hook=(
                    f"Starting phase {phase_index}/{phase_total} with {len(subtasks)} subtasks "
                    f"(source={subtask_plan_source})."
                ),
            )
            baseline_mermaid = {
                candidate.resolve()
                for candidate in job.workspace.glob("*.mermaid")
                if candidate.is_file()
            }
            codebase_summary = workspace_summary_text
            codebase_summary = (
                f"{codebase_summary}\n\n"
                f"Program execution context: phase {phase_index}/{phase_total}."
            )
            phase_success = True
            subtask_results: list[dict[str, Any]] = []
            subtask_prompt_files: list[str] = []

            for subtask_index, subtask_requirement in enumerate(subtasks, start=1):
                if _is_job_cancel_requested(app, job_id):
                    _finalize_job_cancelled(
                        app=app,
                        job_id=job_id,
                        message=f"Cancelled before subtask {subtask_index} of phase {phase_index}.",
                    )
                    return

                _update_job_result_state(
                    app=app,
                    job_id=job_id,
                    updates={
                        "stage": "phase_execution",
                        "phase_current": phase_index,
                        "phase_total": phase_total,
                        "subtask_current": subtask_index,
                        "subtask_total": len(subtasks),
                        "phase_results": list(phase_results),
                        "current_task": (
                            f"Phase {phase_index}/{phase_total} Subtask "
                            f"{subtask_index}/{len(subtasks)}"
                        ),
                    },
                    append_hook=(
                        f"Running phase {phase_index}/{phase_total} subtask "
                        f"{subtask_index}/{len(subtasks)}."
                    ),
                )
                subtask_preview = subtask_requirement.strip().splitlines()[0][:160]
                _update_job_result_state(
                    app=app,
                    job_id=job_id,
                    append_hook=f"Subtask objective: {subtask_preview}",
                )
                subtask_prompt_file = _write_report_text(
                    workspace=job.workspace,
                    report_dir=report_dir,
                    relative_name=f"{phase_base_name}_subtask_{subtask_index:02d}_prompt.txt",
                    content=f"{subtask_requirement.rstrip()}\n",
                )
                if isinstance(subtask_prompt_file, str):
                    subtask_prompt_files.append(subtask_prompt_file)
                    _update_job_result_state(
                        app=app,
                        job_id=job_id,
                        append_hook=(
                            f"Logged subtask prompt: {subtask_prompt_file}"
                        ),
                    )

                with app.state.execution_lock:
                    if _is_job_cancel_requested(app, job_id):
                        _finalize_job_cancelled(
                            app=app,
                            job_id=job_id,
                            message=(
                                f"Cancelled before subtask {subtask_index} "
                                f"execution in phase {phase_index}."
                            ),
                        )
                        return
                    with app.state.jobs_lock:
                        current = app.state.jobs.get(job_id)
                        if current is not None:
                            current.status = "running"
                    subtask_success = orchestrator.execute_feature_request(
                        requirement=subtask_requirement,
                        codebase_summary=codebase_summary,
                        workspace=job.workspace,
                    )

                subtask_result: dict[str, Any] = {
                    "subtask_number": subtask_index,
                    "subtask_total": len(subtasks),
                    "success": subtask_success,
                    "requirement_preview": subtask_requirement[:240],
                }
                subtask_results.append(subtask_result)

                if subtask_success:
                    _update_job_result_state(
                        app=app,
                        job_id=job_id,
                        append_hook=(
                            f"Subtask {subtask_index}/{len(subtasks)} completed."
                        ),
                    )
                    continue

                _update_job_result_state(
                    app=app,
                    job_id=job_id,
                    append_hook=(
                        f"Subtask {subtask_index}/{len(subtasks)} failed. "
                        "Triggering Codex recovery."
                    ),
                )

                with app.state.execution_lock:
                    _update_job_result_state(
                        app=app,
                        job_id=job_id,
                        updates={"current_task": "Codex recovery"},
                        append_hook=(
                            f"Running Codex recovery command: {recovery_command}"
                        ),
                    )
                    recovery_agent = create_default_senior_agent(
                        provider="codex",
                        workspace=job.workspace,
                        max_attempts=4,
                        validation_commands=recovery_validations,
                    )
                    recovery_report = recovery_agent.heal(
                        command=recovery_command,
                        workspace=job.workspace,
                        validation_commands=recovery_validations,
                    )

                subtask_result["codex_recovery"] = {
                    "success": recovery_report.success,
                    "blocked_reason": recovery_report.blocked_reason,
                    "attempts": len(recovery_report.attempts),
                    "command": recovery_command,
                    "validation_commands": recovery_validations,
                    "command_source": recovery_source,
                }

                if recovery_report.success:
                    _update_job_result_state(
                        app=app,
                        job_id=job_id,
                        append_hook=(
                            f"Codex recovery succeeded for subtask {subtask_index}. "
                            "Retrying subtask."
                        ),
                    )
                    with app.state.execution_lock:
                        retry_success = orchestrator.execute_feature_request(
                            requirement=subtask_requirement,
                            codebase_summary=codebase_summary,
                            workspace=job.workspace,
                        )
                    subtask_result["retried_after_recovery"] = True
                    subtask_result["success"] = retry_success
                    if retry_success:
                        _update_job_result_state(
                            app=app,
                            job_id=job_id,
                            append_hook=(
                                f"Subtask {subtask_index}/{len(subtasks)} "
                                "passed after Codex recovery."
                            ),
                        )
                        continue
                    _update_job_result_state(
                        app=app,
                        job_id=job_id,
                        append_hook=(
                            f"Retry failed after Codex recovery on subtask "
                            f"{subtask_index}/{len(subtasks)}."
                        ),
                    )
                else:
                    _update_job_result_state(
                        app=app,
                        job_id=job_id,
                        append_hook=(
                            "Codex recovery failed: "
                            f"{recovery_report.blocked_reason or 'unknown reason'}."
                        ),
                    )

                phase_success = False
                _update_job_result_state(
                    app=app,
                    job_id=job_id,
                    append_hook=(
                        f"Phase {phase_index}/{phase_total} stopped after failed "
                        f"subtask {subtask_index}/{len(subtasks)}."
                    ),
                )
                break

            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message=f"Cancelled after phase {phase_index} execution.",
                )
                return

            mermaid_path = _find_latest_mermaid_path(job.workspace, baseline_mermaid)
            phase_result_payload = {
                "phase_number": phase_index,
                "phase_total": phase_total,
                "success": phase_success,
                "mermaid_path": mermaid_path,
                "requirement_preview": phase_requirement[:240],
                "requirement_file": phase_requirement_file,
                "subtask_plan_prompt_file": subtask_plan_prompt_file,
                "subtask_plan_response_file": subtask_plan_response_file,
                "subtask_plan_source": subtask_plan_source,
                "subtasks_total": len(subtasks),
                "subtasks_completed": sum(
                    1 for item in subtask_results if bool(item.get("success"))
                ),
                "subtask_prompt_files": subtask_prompt_files,
                "subtasks": subtask_results,
            }
            phase_result_file = _write_report_json(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name=f"{phase_base_name}_result.json",
                payload=phase_result_payload,
            )
            phase_review_prompt = _build_review_prompt(
                phase_number=phase_index,
                phase_total=phase_total,
                phase_requirement=phase_requirement,
                phase_success=phase_success,
                mermaid_path=mermaid_path,
            )
            _update_job_result_state(
                app=app,
                job_id=job_id,
                updates={"current_task": f"Generating phase {phase_index}/{phase_total} review"},
                append_hook=f"Generating review for phase {phase_index}/{phase_total}.",
            )
            _write_report_text(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name=f"{phase_base_name}_review_prompt.txt",
                content=f"{phase_review_prompt.rstrip()}\n",
            )
            try:
                if _is_job_cancel_requested(app, job_id):
                    _finalize_job_cancelled(
                        app=app,
                        job_id=job_id,
                        message=f"Cancelled before phase {phase_index} review generation.",
                    )
                    return
                phase_review_text = architect_client.generate_fix(phase_review_prompt)
            except LLMClientError as exc:
                _update_job_result_state(
                    app=app,
                    job_id=job_id,
                    append_hook=f"Review generation fallback used: {exc}",
                )
                phase_review_text = (
                    "## Outcome\n"
                    f"- Review generation unavailable: {exc}\n\n"
                    "## Risks\n"
                    "- Manual review recommended for this phase.\n\n"
                    "## Recommended Next Step\n"
                    "- Inspect generated files and run validation commands.\n"
                )
            phase_review_file = _write_report_text(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name=f"{phase_base_name}_review.md",
                content=f"{phase_review_text.rstrip()}\n",
            )
            phase_results.append(
                {
                    "phase_number": phase_index,
                    "phase_total": phase_total,
                    "success": phase_success,
                    "mermaid_path": mermaid_path,
                    "requirement_preview": phase_requirement[:240],
                    "requirement_file": phase_requirement_file,
                    "result_file": phase_result_file,
                    "review_file": phase_review_file,
                }
            )
            _update_job_result_state(
                app=app,
                job_id=job_id,
                updates={
                    "phase_results": list(phase_results),
                    "current_task": f"Phase {phase_index}/{phase_total} completed",
                },
                append_hook=(
                    f"Phase {phase_index}/{phase_total} "
                    f"{'completed' if phase_success else 'failed'}."
                ),
            )
            if not phase_success:
                overall_success = False
                break

        if overall_success:
            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message="Cancelled before post-run self-heal.",
                )
                return

            post_heal_command = recovery_command
            post_heal_validations = recovery_validations
            command_source = recovery_source
            self_heal_request_payload = {
                "command": post_heal_command,
                "validation_commands": post_heal_validations,
                "command_source": command_source,
                "max_attempts": 4,
            }
            self_heal_request_file = _write_report_json(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name="90_self_heal_request.json",
                payload=self_heal_request_payload,
            )

            _update_job_result_state(
                app=app,
                job_id=job_id,
                updates={
                    "success": None,
                    "stage": "self_heal",
                    "phase_total": phase_total,
                    "phase_completed": len(phase_results),
                    "reports_dir": report_dir_relative,
                    "phase_results": list(phase_results),
                    "self_heal_request_file": self_heal_request_file,
                    "current_task": "Running post-run self-heal",
                },
                append_hook="Starting post-run self-heal pass.",
            )

            with app.state.execution_lock:
                if _is_job_cancel_requested(app, job_id):
                    _finalize_job_cancelled(
                        app=app,
                        job_id=job_id,
                        message="Cancelled before self-heal execution.",
                    )
                    return
                with app.state.jobs_lock:
                    current = app.state.jobs.get(job_id)
                    if current is not None:
                        current.status = "running"
                heal_agent = create_default_senior_agent(
                    provider=app.state.role_provider_map.get("developer", app.state.provider),
                    workspace=job.workspace,
                    max_attempts=4,
                    validation_commands=post_heal_validations,
                )
                heal_report = heal_agent.heal(
                    command=post_heal_command,
                    workspace=job.workspace,
                    validation_commands=post_heal_validations,
                )
            _update_job_result_state(
                app=app,
                job_id=job_id,
                append_hook=(
                    "Post-run self-heal completed: "
                    f"{'success' if heal_report.success else 'failed'}."
                ),
            )

            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message="Cancelled after self-heal execution.",
                )
                return

            self_heal_report_payload = heal_report.to_dict()
            self_heal_report_payload["command_source"] = command_source
            self_heal_report_file = _write_report_json(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name="90_self_heal_report.json",
                payload=self_heal_report_payload,
            )
            self_heal_review_text = (
                "## Outcome\n"
                f"- Success: {heal_report.success}\n"
                f"- Command: `{post_heal_command}`\n"
                f"- Attempts: {len(heal_report.attempts)}\n\n"
                "## Validation\n"
                f"- Additional commands: {post_heal_validations or ['none']}\n"
                f"- Blocked reason: {heal_report.blocked_reason or 'none'}\n\n"
                "## Recommended Next Step\n"
                "- Inspect `90_self_heal_report.json` and re-run project validations.\n"
            )
            self_heal_review_file = _write_report_text(
                workspace=job.workspace,
                report_dir=report_dir,
                relative_name="90_self_heal_review.md",
                content=self_heal_review_text,
            )
            self_heal_summary = {
                "success": heal_report.success,
                "blocked_reason": heal_report.blocked_reason,
                "attempts": len(heal_report.attempts),
                "command": post_heal_command,
                "validation_commands": post_heal_validations,
                "command_source": command_source,
                "request_file": self_heal_request_file,
                "report_file": self_heal_report_file,
                "review_file": self_heal_review_file,
            }
            if not heal_report.success:
                overall_success = False

        summary_payload = {
            "success": overall_success,
            "phase_total": phase_total,
            "phase_completed": len(phase_results),
            "phase_results": phase_results,
            "planning_notes": planning_notes,
            "role_providers": dict(app.state.role_provider_map),
            "workspace": str(job.workspace),
            "reports_dir": report_dir_relative,
            "master_requirement_file": master_requirement_file,
            "workspace_summary_file": workspace_summary_file,
            "product_spec_file": product_spec_file,
            "task_plan_file": task_plan_file,
            "post_self_heal": self_heal_summary,
        }
        _update_job_result_state(
            app=app,
            job_id=job_id,
            append_hook=(
                "Program execution completed successfully."
                if overall_success
                else "Program execution completed with failures."
            ),
            updates={"current_task": "Program complete"},
        )
        summary_file = _write_report_json(
            workspace=job.workspace,
            report_dir=report_dir,
            relative_name="99_program_summary.json",
            payload=summary_payload,
        )
        summary_payload["summary_file"] = summary_file

        with app.state.jobs_lock:
            existing = app.state.jobs.get(job_id)
            if existing is None:
                return
            existing_result = existing.result if isinstance(existing.result, dict) else {}
            hooks = existing_result.get("hooks")
            if isinstance(hooks, list):
                summary_payload["hooks"] = hooks
            existing.success = overall_success
            existing.status = "succeeded" if overall_success else "failed"
            existing.result = summary_payload
            existing.finished_at = _utc_now_iso()
    except Exception as exc:  # pragma: no cover - defensive guardrail
        logger.exception("Program execution job failed unexpectedly: job_id=%s error=%s", job_id, exc)
        _update_job_result_state(
            app=app,
            job_id=job_id,
            append_hook=f"Program execution crashed: {exc}",
            updates={"current_task": "Program failed"},
        )
        with app.state.jobs_lock:
            existing = app.state.jobs.get(job_id)
            if existing is None:
                return
            existing.success = False
            existing.status = "failed"
            existing.error = str(exc)
            existing.finished_at = _utc_now_iso()


def _execute_heal_job(app: FastAPI, job_id: str) -> None:
    with app.state.jobs_lock:
        job: ExecutionJob | None = app.state.jobs.get(job_id)
        if job is None:
            return
        job.status = "waiting"
        job.started_at = _utc_now_iso()
    _update_job_result_state(
        app=app,
        job_id=job_id,
        updates={
            "stage": "self_heal",
            "current_task": "Waiting for execution slot",
        },
        append_hook="Queued self-heal job.",
    )

    if _is_job_cancel_requested(app, job_id):
        _finalize_job_cancelled(
            app=app,
            job_id=job_id,
            message="Cancelled before self-heal started.",
        )
        return

    try:
        command = str(job.payload["command"])
        max_attempts = int(job.payload.get("max_attempts", 3))
        validation_commands = job.payload.get("validation_commands")
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Running self-heal command"},
            append_hook="Starting self-heal execution with developer agent.",
        )

        with app.state.execution_lock:
            if _is_job_cancel_requested(app, job_id):
                _finalize_job_cancelled(
                    app=app,
                    job_id=job_id,
                    message="Cancelled before self-heal execution.",
                )
                return
            with app.state.jobs_lock:
                current = app.state.jobs.get(job_id)
                if current is not None:
                    current.status = "running"
            agent = create_default_senior_agent(
                provider=app.state.role_provider_map.get("developer", app.state.provider),
                workspace=job.workspace,
                max_attempts=max_attempts,
                validation_commands=validation_commands,
            )
            report = agent.heal(
                command=command,
                workspace=job.workspace,
                validation_commands=validation_commands,
            )

        if _is_job_cancel_requested(app, job_id):
            _finalize_job_cancelled(
                app=app,
                job_id=job_id,
                message="Cancelled after self-heal execution.",
            )
            return

        summary = {
            "success": report.success,
            "blocked_reason": report.blocked_reason,
            "attempts": len(report.attempts),
            "final_return_code": report.final_result.return_code,
            "final_command": report.final_result.command,
            "report": report.to_dict(),
        }
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={"current_task": "Self-heal complete"},
            append_hook=(
                "Self-heal completed successfully."
                if report.success
                else "Self-heal completed with failures."
            ),
        )
        with app.state.jobs_lock:
            existing = app.state.jobs.get(job_id)
            if existing is None:
                return
            existing_result = existing.result if isinstance(existing.result, dict) else {}
            hooks = existing_result.get("hooks")
            if isinstance(hooks, list):
                summary["hooks"] = hooks
            existing.success = report.success
            existing.status = "succeeded" if report.success else "failed"
            existing.result = summary
            existing.finished_at = _utc_now_iso()
    except Exception as exc:  # pragma: no cover - defensive guardrail
        logger.exception("Heal job failed unexpectedly: job_id=%s error=%s", job_id, exc)
        _update_job_result_state(
            app=app,
            job_id=job_id,
            append_hook=f"Self-heal crashed: {exc}",
            updates={"current_task": "Self-heal failed"},
        )
        with app.state.jobs_lock:
            existing = app.state.jobs.get(job_id)
            if existing is None:
                return
            existing.success = False
            existing.status = "failed"
            existing.error = str(exc)
            existing.finished_at = _utc_now_iso()


def _render_home_ui(*, default_workspace: Path, provider: str) -> str:
    template = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Senior Agent Projects</title>
  <style>
    :root {
      --bg: #f3f6f2;
      --panel: #ffffff;
      --text: #1b2b23;
      --muted: #50665a;
      --accent: #0f6b4f;
      --accent-soft: #cfeedd;
      --border: #c8d8ce;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at 20% 0%, #e5f2ea 0%, var(--bg) 60%);
      color: var(--text);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }
    .container {
      width: min(980px, 96%);
      margin: 24px auto 40px;
    }
    .hero, .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 12px;
    }
    .hero h1 { margin: 0 0 6px; }
    .meta { color: var(--muted); font-size: 0.92rem; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    input, button {
      border-radius: 10px;
      border: 1px solid var(--border);
      padding: 9px 10px;
      font-family: inherit;
      font-size: 0.92rem;
    }
    #project-name { min-width: 260px; flex: 1; }
    button {
      background: var(--accent);
      color: #fff;
      border: none;
      cursor: pointer;
      font-weight: 600;
    }
    button.secondary {
      background: #f4faf6;
      color: #124333;
      border: 1px solid var(--border);
    }
    .project-item {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px;
      margin-bottom: 8px;
      background: #f8fcf9;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }
    .project-name {
      font-weight: 600;
      color: #124333;
      text-decoration: none;
    }
    .project-meta { color: var(--muted); font-size: 0.82rem; }
    @media (max-width: 680px) {
      .project-item { align-items: flex-start; }
    }
  </style>
</head>
<body>
  <div class="container">
    <section class="hero">
      <h1>Projects</h1>
      <div class="meta">
        Provider: <strong>__PROVIDER__</strong> |
        Workspace Root: <strong>__WORKSPACE__</strong>
      </div>
    </section>

    <section class="card">
      <h2 style="margin-top:0;">Create Project</h2>
      <form id="project-form" class="row">
        <input id="project-name" required placeholder="my-new-project" />
        <button type="submit">Create</button>
        <button id="refresh-projects" class="secondary" type="button">Refresh</button>
      </form>
    </section>

    <section class="card">
      <h2 style="margin-top:0;">Project List</h2>
      <div id="project-list">Loading projects...</div>
    </section>
  </div>

  <script>
    async function api(path, options = {}) {
      const normalizedPath = path.startsWith("/") ? path : `/${path}`;
      const response = await fetch(normalizedPath, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const text = await response.text();
      const payload = text ? JSON.parse(text) : {};
      if (!response.ok) {
        throw new Error(payload.detail || `HTTP ${response.status}`);
      }
      return payload;
    }

    async function loadProjects() {
      const payload = await api("/api/projects");
      const projects = payload.projects || [];
      const root = document.getElementById("project-list");
      if (!projects.length) {
        root.innerHTML = `<p class="project-meta">No projects yet.</p>`;
        return;
      }
      root.innerHTML = projects.map((project) => `
        <div class="project-item">
          <div>
            <a class="project-name" href="/project?workspace=${encodeURIComponent(project.workspace)}">${project.name}</a>
            <div class="project-meta">${project.relative_path}</div>
          </div>
          <div class="row">
            <button class="secondary" type="button" data-open="${project.workspace}">Open Folder</button>
            <button type="button" data-enter="${project.workspace}">Enter Project</button>
          </div>
        </div>
      `).join("");

      root.querySelectorAll("button[data-open]").forEach((button) => {
        button.addEventListener("click", async () => {
          try {
            const workspace = button.dataset.open;
            const result = await api("/api/projects/open", {
              method: "POST",
              body: JSON.stringify({ workspace }),
            });
            if (result.message) alert(result.message);
          } catch (error) {
            alert(error.message);
          }
        });
      });

      root.querySelectorAll("button[data-enter]").forEach((button) => {
        button.addEventListener("click", () => {
          const workspace = button.dataset.enter;
          window.location.href = `/project?workspace=${encodeURIComponent(workspace)}`;
        });
      });
    }

    document.getElementById("project-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const projectName = document.getElementById("project-name").value.trim();
      if (!projectName) return;
      try {
        const created = await api("/api/projects", {
          method: "POST",
          body: JSON.stringify({ project_name: projectName }),
        });
        document.getElementById("project-name").value = "";
        window.location.href = `/project?workspace=${encodeURIComponent(created.workspace)}`;
      } catch (error) {
        alert(error.message);
      }
    });
    document.getElementById("refresh-projects").addEventListener("click", () => {
      loadProjects().catch((error) => alert(error.message));
    });

    loadProjects().catch((error) => {
      document.getElementById("project-list").textContent = `Failed to load projects: ${error.message}`;
    });
  </script>
</body>
</html>
"""
    return (
        template
        .replace("__PROVIDER__", html.escape(provider))
        .replace("__WORKSPACE__", html.escape(str(default_workspace)))
    )


def _render_ui(*, default_workspace: Path, provider: str, selected_workspace: Path) -> str:
    project_name = _workspace_display_name(
        base_workspace=default_workspace,
        workspace=selected_workspace,
    )
    template = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Project Dashboard</title>
  <style>
    :root {
      --bg: #f3f6f2;
      --panel: #ffffff;
      --panel-alt: #eef5ee;
      --text: #1b2b23;
      --muted: #50665a;
      --accent: #0f6b4f;
      --accent-soft: #cfeedd;
      --danger: #8c2f39;
      --border: #c8d8ce;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at 20% 0%, #e5f2ea 0%, var(--bg) 60%);
      color: var(--text);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }
    .container {
      width: min(1160px, 96%);
      margin: 24px auto 40px;
    }
    .hero {
      background: linear-gradient(130deg, #fdfefe 0%, #e8f3ea 100%);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 10px 28px rgba(8, 28, 20, 0.08);
      margin-bottom: 16px;
    }
    .back-link {
      color: var(--muted);
      text-decoration: none;
      font-size: 0.86rem;
      font-weight: 600;
    }
    .back-link:hover { color: var(--accent); }
    .hero-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .project-title {
      margin: 0;
      font-size: 1.6rem;
      letter-spacing: 0.2px;
    }
    .meta {
      color: var(--muted);
      font-size: 0.92rem;
      margin-top: 6px;
    }
    .status-strip {
      margin-top: 12px;
      padding: 10px;
      border-radius: 10px;
      background: var(--accent-soft);
      color: #124333;
      font-size: 0.9rem;
    }
    .open-btn {
      width: auto;
      margin: 0;
      padding: 9px 14px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #f4faf6;
      color: #124333;
      font-weight: 600;
      cursor: pointer;
    }
    .open-btn:hover { border-color: var(--accent); color: var(--accent); }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 12px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
    }
    .grid-wide { grid-column: 1 / -1; }
    .card h2 {
      margin: 0 0 10px;
      font-size: 1.02rem;
      letter-spacing: 0.2px;
    }
    label {
      display: block;
      margin-bottom: 4px;
      font-size: 0.83rem;
      color: var(--muted);
    }
    input, textarea, button {
      width: 100%;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #fff;
      padding: 9px 10px;
      margin-bottom: 10px;
      font-size: 0.92rem;
      font-family: inherit;
    }
    textarea { min-height: 96px; resize: vertical; }
    button {
      background: var(--accent);
      color: #fff;
      border: none;
      cursor: pointer;
      font-weight: 600;
    }
    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 14px rgba(15, 107, 79, 0.25);
    }
    .hint {
      background: var(--panel-alt);
      border: 1px dashed var(--border);
      border-radius: 10px;
      color: var(--muted);
      padding: 8px 9px;
      font-size: 0.82rem;
      margin-bottom: 10px;
    }
    .progress-wrap {
      border: 1px solid var(--border);
      border-radius: 999px;
      background: #f1f7f3;
      overflow: hidden;
      height: 10px;
      margin: 6px 0 10px;
    }
    .progress-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #0f6b4f 0%, #1aa373 100%);
      transition: width 0.2s ease;
    }
    .kpis {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 10px;
    }
    .kpi {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px;
      background: #f8fcf9;
    }
    .kpi-label { color: var(--muted); font-size: 0.75rem; display: block; }
    .kpi-value { font-size: 0.9rem; font-weight: 600; color: #124333; }
    .hook-list, .file-list {
      margin: 0;
      padding-left: 18px;
      font-size: 0.84rem;
      color: #173128;
      max-height: 200px;
      overflow: auto;
    }
    .hook-list li, .file-list li { margin-bottom: 4px; }
    .jobs {
      margin-top: 14px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
    }
    .jobs-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
      gap: 8px;
      flex-wrap: wrap;
    }
    .jobs table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.84rem;
    }
    .jobs th, .jobs td {
      border-bottom: 1px solid #dbe7df;
      padding: 7px 6px;
      text-align: left;
      vertical-align: top;
    }
    .jobs tr:last-child td { border-bottom: none; }
    .jobs tr.active-row { background: #f0f8f3; }
    .chip {
      border-radius: 999px;
      display: inline-block;
      padding: 2px 8px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.35px;
    }
    .queued { background: #f2e8c9; color: #694d00; }
    .waiting { background: #efe5fb; color: #4f2d77; }
    .running { background: #cde7ff; color: #0d4f8c; }
    .succeeded { background: #caefd9; color: #0f5f3a; }
    .failed { background: #f5d5d8; color: var(--danger); }
    .cancelled { background: #ececec; color: #444; }
    .job-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .job-actions button {
      width: auto;
      margin: 0;
      padding: 7px 10px;
    }
    .secondary-btn {
      background: #f4faf6;
      color: #124333;
      border: 1px solid var(--border);
    }
    .danger-btn {
      background: #a03c46;
    }
    .artifact-meta {
      margin: 0 0 8px;
      font-size: 0.83rem;
      color: var(--muted);
    }
    #artifact-tail {
      max-height: 260px;
      min-height: 120px;
    }
    .job-details {
      margin-top: 12px;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #f9fcfa;
      padding: 10px;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 0.8rem;
      color: #1b2b23;
      max-height: 290px;
      overflow: auto;
    }
    .inline-row {
      display: grid;
      grid-template-columns: 1fr 1fr 1.4fr;
      gap: 8px;
    }
    @media (max-width: 700px) {
      .inline-row { grid-template-columns: 1fr; }
      .kpis { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="container">
    <section class="hero">
      <a class="back-link" href="/">← Back to Projects</a>
      <div class="hero-top">
        <h1 class="project-title" id="project-title">Project: __PROJECT_NAME__</h1>
        <button id="open-project-folder" class="open-btn" type="button">Open Project Folder</button>
      </div>
      <div class="meta">
        Provider: <strong>__PROVIDER__</strong> |
        Workspace: <strong id="workspace-label">__SELECTED_WORKSPACE__</strong> |
        Workspace Root: <strong>__WORKSPACE__</strong>
      </div>
      <div class="meta" id="stream-status">Live updates: connecting...</div>
      <div class="status-strip" id="global-status">Loading project status...</div>
    </section>

    <section class="grid">
      <article class="card">
        <h2>Feature Execution</h2>
        <form id="execute-form">
          <label for="requirement">Requirement</label>
          <textarea id="requirement" required placeholder="Implement a targeted feature in this project."></textarea>
          <label for="codebase-summary">Codebase Summary (optional override)</label>
          <textarea id="codebase-summary" placeholder="Leave empty to auto-generate summary."></textarea>
          <button type="submit">Run Feature Job</button>
        </form>
      </article>

      <article class="card">
        <h2>Program Execution</h2>
        <div class="hint">Paste the full brief once. The agent will create product spec, task plans, phase reviews, and auto self-heal artifacts under <code>AgentReports/</code>.</div>
        <form id="program-form">
          <label for="program-requirement">Master Requirement</label>
          <textarea id="program-requirement" required placeholder="Paste complete product brief here."></textarea>
          <div class="inline-row">
            <div>
              <label for="program-max-phases">Max Phases</label>
              <input id="program-max-phases" type="number" min="1" max="12" value="6" />
            </div>
            <div>
              <label for="program-max-subtasks">Max Subtasks / Phase</label>
              <input id="program-max-subtasks" type="number" min="1" max="20" value="6" />
            </div>
            <div>
              <label for="program-summary">Codebase Summary (optional override)</label>
              <textarea id="program-summary" placeholder="Leave empty to auto-generate summary each phase."></textarea>
            </div>
          </div>
          <button type="submit">Run Program Job</button>
        </form>
      </article>

      <article class="card grid-wide">
        <h2>Active Hooks & Runtime Status</h2>
        <div class="kpis">
          <div class="kpi">
            <span class="kpi-label">Current Job</span>
            <span class="kpi-value" id="active-job-label">No active job</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">Completion Rate</span>
            <span class="kpi-value" id="completion-rate">0%</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">Status</span>
            <span class="kpi-value" id="active-status">idle</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">Completion Time</span>
            <span class="kpi-value" id="completion-time">-</span>
          </div>
        </div>
        <div class="progress-wrap"><div id="progress-fill" class="progress-fill"></div></div>
        <div class="hint" id="next-hook">Next: waiting for first job.</div>
        <div class="inline-row">
          <div>
            <h3 style="margin:0 0 8px;font-size:0.92rem;">Active Hooks</h3>
            <ul id="hook-list" class="hook-list"><li>No hooks yet.</li></ul>
          </div>
          <div>
            <h3 style="margin:0 0 8px;font-size:0.92rem;">Created Files</h3>
            <ul id="file-list" class="file-list"><li>No files created yet.</li></ul>
          </div>
        </div>
      </article>

      <article class="card grid-wide">
        <h2>Current Phase Artifact Tail</h2>
        <p id="artifact-path" class="artifact-meta">No artifact available yet.</p>
        <pre id="artifact-tail">Waiting for running phase output...</pre>
      </article>
    </section>

    <section class="jobs">
      <div class="jobs-header">
        <h2 style="margin:0;">Project Jobs</h2>
        <div class="job-actions">
          <button id="cancel-job-button" class="danger-btn" type="button">Cancel Selected</button>
          <button id="retry-job-button" class="secondary-btn" type="button">Retry Selected</button>
          <button id="refresh-button" class="secondary-btn" type="button" style="padding:7px 14px;">Refresh</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Job ID</th>
            <th>Type</th>
            <th>Status</th>
            <th>Hook</th>
            <th>Progress</th>
            <th>Files</th>
            <th>Created</th>
            <th>Finished</th>
          </tr>
        </thead>
        <tbody id="jobs-body"></tbody>
      </table>
      <div class="job-details">
        <strong>Selected Job Details</strong>
        <pre id="job-details">Select a job row to inspect details.</pre>
      </div>
    </section>
  </div>

  <script>
    const state = {
      selectedWorkspace: "__SELECTED_WORKSPACE__",
      selectedJobId: null,
      selectedJob: null,
      eventSource: null,
      reconnectHandle: null,
      artifactBusy: false,
      lastArtifactRefreshMs: 0,
      detailBusy: false,
    };

    const streamStatus = document.getElementById("stream-status");
    const globalStatus = document.getElementById("global-status");
    const jobsBody = document.getElementById("jobs-body");
    const jobDetails = document.getElementById("job-details");
    const activeJobLabel = document.getElementById("active-job-label");
    const completionRateLabel = document.getElementById("completion-rate");
    const activeStatusLabel = document.getElementById("active-status");
    const completionTimeLabel = document.getElementById("completion-time");
    const progressFill = document.getElementById("progress-fill");
    const nextHookLabel = document.getElementById("next-hook");
    const hookList = document.getElementById("hook-list");
    const fileList = document.getElementById("file-list");
    const artifactPath = document.getElementById("artifact-path");
    const artifactTail = document.getElementById("artifact-tail");
    const cancelJobButton = document.getElementById("cancel-job-button");
    const retryJobButton = document.getElementById("retry-job-button");

    function closeEventStream() {
      if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
      }
      if (state.reconnectHandle) {
        clearTimeout(state.reconnectHandle);
        state.reconnectHandle = null;
      }
    }

    async function api(path, options = {}) {
      const normalizedPath = path.startsWith("/") ? path : `/${path}`;
      let response;
      try {
        response = await fetch(normalizedPath, {
          headers: { "Content-Type": "application/json" },
          ...options,
        });
      } catch (error) {
        console.error("API network error", {
          path: normalizedPath,
          method: options.method || "GET",
          error,
        });
        throw new Error(error?.message || "Network request failed.");
      }

      const text = await response.text();
      let payload = {};
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (error) {
          console.error("API response JSON parse error", {
            path: normalizedPath,
            method: options.method || "GET",
            status: response.status,
            body_preview: text.slice(0, 300),
            error,
          });
          throw new Error(`Invalid API response (HTTP ${response.status}).`);
        }
      }

      if (!response.ok) {
        const detail = (
          payload &&
          typeof payload === "object" &&
          typeof payload.detail === "string" &&
          payload.detail.trim()
        ) ? payload.detail.trim() : `HTTP ${response.status}`;
        console.error("API request failed", {
          path: normalizedPath,
          method: options.method || "GET",
          status: response.status,
          detail,
          payload,
        });
        throw new Error(detail);
      }
      return payload;
    }

    function statusClass(status) {
      if (status === "queued") return "queued";
      if (status === "waiting") return "waiting";
      if (status === "running") return "running";
      if (status === "succeeded") return "succeeded";
      if (status === "failed") return "failed";
      if (status === "cancelled") return "cancelled";
      return "";
    }

    function truncate(text, max = 40) {
      if (!text) return "";
      return text.length <= max ? text : `${text.slice(0, max)}...`;
    }

    function isoToDate(rawValue) {
      if (!rawValue) return null;
      const value = new Date(rawValue);
      if (Number.isNaN(value.getTime())) return null;
      return value;
    }

    function formatTimestamp(rawValue) {
      const value = isoToDate(rawValue);
      if (!value) return "-";
      return value.toLocaleString();
    }

    function formatDuration(startRaw, endRaw = null) {
      const start = isoToDate(startRaw);
      if (!start) return "-";
      const end = isoToDate(endRaw) || new Date();
      const ms = Math.max(0, end.getTime() - start.getTime());
      const seconds = Math.floor(ms / 1000);
      const minutes = Math.floor(seconds / 60);
      const remSeconds = seconds % 60;
      if (minutes > 0) return `${minutes}m ${remSeconds}s`;
      return `${remSeconds}s`;
    }

    function renderJobs(jobs) {
      if (!jobs.length) {
        jobsBody.innerHTML = `<tr><td colspan="8">No jobs in this project yet.</td></tr>`;
        return;
      }
      jobsBody.innerHTML = jobs
        .map((job) => {
          const className = statusClass(job.status);
          const progress = job.progress || {};
          const createdCount = (job.created_files || []).length;
          const isActive = state.selectedJobId === job.job_id ? "active-row" : "";
          const cancelTag = job.cancel_requested ? " (cancel requested)" : "";
          return `
            <tr class="${isActive}" data-job-id="${job.job_id}">
              <td><code>${truncate(job.job_id, 12)}</code></td>
              <td>${job.job_type}</td>
              <td><span class="chip ${className}">${job.status}${cancelTag}</span></td>
              <td>${progress.active_hook || "-"}</td>
              <td>${progress.percent || 0}%</td>
              <td>${createdCount}</td>
              <td>${formatTimestamp(job.created_at)}</td>
              <td>${formatTimestamp(job.finished_at)}</td>
            </tr>`;
        })
        .join("");

      jobsBody.querySelectorAll("tr[data-job-id]").forEach((row) => {
        row.addEventListener("click", async () => {
          state.selectedJobId = row.dataset.jobId;
          await refreshStatus();
        });
      });
    }

    function renderHookList(job) {
      const progress = job.progress || {};
      const hooks = [];
      hooks.push(`Current: ${progress.active_hook || job.status}`);
      if (progress.next_hook) hooks.push(`Next: ${progress.next_hook}`);
      if (job.queue_position) {
        hooks.push(`Queue Position: ${job.queue_position}/${job.queue_depth || "?"}`);
      }
      if (job.cancel_requested) hooks.push(`Cancellation requested: ${job.cancel_reason || "yes"}`);

      const result = job.result || {};
      if (typeof result.stage === "string" && result.stage.trim()) {
        hooks.push(`Stage: ${result.stage}`);
      }
      if (typeof result.current_task === "string" && result.current_task.trim()) {
        hooks.push(`Working On: ${result.current_task}`);
      }
      const phaseCurrent = Number(result.phase_current || 0);
      const phaseTotal = Number(result.phase_total || 0);
      if (phaseTotal > 0) {
        hooks.push(`Phase: ${phaseCurrent > 0 ? `${phaseCurrent}/${phaseTotal}` : `${result.phase_completed || 0}/${phaseTotal}`}`);
      }
      const subtaskCurrent = Number(result.subtask_current || 0);
      const subtaskTotal = Number(result.subtask_total || 0);
      if (subtaskTotal > 0) {
        hooks.push(`Subtask: ${subtaskCurrent > 0 ? `${subtaskCurrent}/${subtaskTotal}` : `0/${subtaskTotal}`}`);
      }
      if (job.payload && typeof job.payload.command === "string") {
        hooks.push(`Command: ${job.payload.command}`);
      }
      if (job.payload && typeof job.payload.requirement === "string") {
        hooks.push(`Requirement: ${truncate(job.payload.requirement, 140)}`);
      }
      if (Array.isArray(result.hooks) && result.hooks.length) {
        result.hooks.slice(-25).forEach((entry) => {
          hooks.push(`Live: ${String(entry)}`);
        });
      }
      hookList.innerHTML = hooks.length
        ? hooks.map((hook) => `<li>${hook}</li>`).join("")
        : "<li>No hooks yet.</li>";
    }

    function renderCreatedFiles(job) {
      const files = job.created_files || [];
      if (!files.length) {
        fileList.innerHTML = "<li>No files created yet.</li>";
        return;
      }
      fileList.innerHTML = files
        .slice(0, 80)
        .map((filePath) => `<li><code>${filePath}</code></li>`)
        .join("");
    }

    function updateActionButtons(job) {
      const hasSelection = Boolean(job && job.job_id);
      const active = hasSelection && ["queued", "waiting", "running"].includes(job.status);
      cancelJobButton.disabled = !active;
      retryJobButton.disabled = !hasSelection || active;
    }

    function renderActiveOverview(job) {
      if (!job) {
        state.selectedJob = null;
        activeJobLabel.textContent = "No active job";
        completionRateLabel.textContent = "0%";
        activeStatusLabel.textContent = "idle";
        completionTimeLabel.textContent = "-";
        progressFill.style.width = "0%";
        nextHookLabel.textContent = "Next: waiting for first job.";
        hookList.innerHTML = "<li>No hooks yet.</li>";
        fileList.innerHTML = "<li>No files created yet.</li>";
        artifactPath.textContent = "No artifact available yet.";
        artifactTail.textContent = "Waiting for running phase output...";
        updateActionButtons(null);
        return;
      }

      state.selectedJob = job;
      const progress = job.progress || {};
      const percent = Number(progress.percent || 0);
      activeJobLabel.textContent = `${job.job_type} (${truncate(job.job_id, 12)})`;
      completionRateLabel.textContent = `${percent}%`;
      activeStatusLabel.textContent = job.status;
      completionTimeLabel.textContent = formatDuration(job.started_at, job.finished_at);
      progressFill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
      nextHookLabel.textContent = `Next: ${progress.next_hook || "Done"}`;
      renderHookList(job);
      renderCreatedFiles(job);
      updateActionButtons(job);
    }

    async function refreshArtifactTail(force = false) {
      const now = Date.now();
      if (!force && now - state.lastArtifactRefreshMs < 1200) {
        return;
      }
      if (state.artifactBusy) {
        return;
      }
      state.artifactBusy = true;
      state.lastArtifactRefreshMs = now;

      try {
        const params = new URLSearchParams();
        params.set("workspace", state.selectedWorkspace);
        if (state.selectedJobId) {
          params.set("job_id", state.selectedJobId);
        } else if (state.selectedJob && state.selectedJob.job_id) {
          params.set("job_id", state.selectedJob.job_id);
        }
        const payload = await api(`/api/artifacts/tail?${params.toString()}`);
        if (payload.status !== "ok" || !payload.artifact_path) {
          artifactPath.textContent = "No artifact available yet.";
          artifactTail.textContent = "Waiting for running phase output...";
          return;
        }
        artifactPath.textContent = `Artifact: ${payload.artifact_path}`;
        artifactTail.textContent = payload.content || "(artifact file is empty)";
      } catch (error) {
        artifactPath.textContent = `Artifact unavailable: ${error.message}`;
      } finally {
        state.artifactBusy = false;
      }
    }

    function applyStatusPayload(statusPayload) {
      globalStatus.textContent = (
        `Project jobs: ${statusPayload.jobs_total} total | `
        + `${statusPayload.jobs_queued || 0} queued | `
        + `${statusPayload.jobs_running || 0} running | `
        + `${statusPayload.jobs_waiting || 0} waiting | `
        + `${statusPayload.jobs_succeeded || 0} succeeded | `
        + `${statusPayload.jobs_failed || 0} failed | `
        + `${statusPayload.jobs_cancelled || 0} cancelled`
      );

      const jobs = statusPayload.recent_jobs || [];
      renderJobs(jobs);

      let focusJob = null;
      if (state.selectedJobId) {
        focusJob = jobs.find((job) => job.job_id === state.selectedJobId) || null;
      }
      if (!focusJob) {
        focusJob = statusPayload.active_job || jobs[0] || null;
      }
      if (focusJob && !state.selectedJobId) {
        state.selectedJobId = focusJob.job_id;
      }
      if (!focusJob) {
        state.selectedJobId = null;
      }
      if (!state.selectedJobId) {
        jobDetails.textContent = focusJob ? JSON.stringify(focusJob, null, 2) : "No job selected.";
      }
      renderActiveOverview(focusJob);

      const shouldTailRefresh = Boolean(
        focusJob && ["queued", "waiting", "running", "succeeded", "failed", "cancelled"].includes(focusJob.status)
      );
      if (shouldTailRefresh) {
        refreshArtifactTail().catch(() => null);
      }
    }

    async function refreshStatus() {
      const queryWorkspace = encodeURIComponent(state.selectedWorkspace);
      const statusPayload = await api(`/api/status?workspace=${queryWorkspace}`);
      applyStatusPayload(statusPayload);
      if (state.selectedJobId) {
        if (state.detailBusy) {
          return;
        }
        state.detailBusy = true;
        try {
          const detail = await api(`/api/status?job_id=${encodeURIComponent(state.selectedJobId)}`);
          if (detail.workspace === state.selectedWorkspace) {
            renderActiveOverview(detail);
            jobDetails.textContent = JSON.stringify(detail, null, 2);
            refreshArtifactTail(true).catch(() => null);
          }
        } catch (error) {
          jobDetails.textContent = `Failed to load selected job: ${error.message}`;
        } finally {
          state.detailBusy = false;
        }
      }
    }

    function startEventStream() {
      closeEventStream();
      if (!window.EventSource) {
        streamStatus.textContent = "Live updates: unavailable in this browser. Use Refresh.";
        return;
      }

      const streamUrl = `/api/events?workspace=${encodeURIComponent(state.selectedWorkspace)}`;
      const source = new EventSource(streamUrl);
      state.eventSource = source;
      streamStatus.textContent = "Live updates: connected";

      source.addEventListener("status", (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload && payload.type === "status" && payload.status) {
            applyStatusPayload(payload.status);
          }
        } catch (error) {
          console.error("Failed to parse SSE payload", error);
        }
      });

      source.onerror = () => {
        streamStatus.textContent = "Live updates: reconnecting...";
        closeEventStream();
        state.reconnectHandle = setTimeout(() => {
          startEventStream();
        }, 2000);
      };
    }

    async function cancelSelectedJob() {
      if (!state.selectedJobId) {
        throw new Error("Select a job first.");
      }
      await api(`/api/jobs/${encodeURIComponent(state.selectedJobId)}/cancel`, {
        method: "POST",
      });
      await refreshStatus();
    }

    async function retrySelectedJob() {
      if (!state.selectedJobId) {
        throw new Error("Select a job first.");
      }
      const result = await api(`/api/jobs/${encodeURIComponent(state.selectedJobId)}/retry`, {
        method: "POST",
      });
      state.selectedJobId = result.job_id;
      await refreshStatus();
    }

    async function submitExecute(event) {
      event.preventDefault();
      const requirement = document.getElementById("requirement").value.trim();
      if (!requirement) return;
      const codebaseSummary = document.getElementById("codebase-summary").value.trim();
      const payload = {
        requirement,
        workspace: state.selectedWorkspace,
        codebase_summary: codebaseSummary || null,
      };
      const result = await api("/api/execute", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.selectedJobId = result.job_id;
      if (result.queued) {
        streamStatus.textContent = `Job submitted: ${truncate(result.job_id, 12)} queued.`;
      }
      await refreshStatus();
    }

    async function submitProgram(event) {
      event.preventDefault();
      const requirement = document.getElementById("program-requirement").value.trim();
      if (!requirement) return;
      const codebaseSummary = document.getElementById("program-summary").value.trim();
      const maxPhases = Number(document.getElementById("program-max-phases").value || "6");
      const maxSubtasksPerPhase = Number(document.getElementById("program-max-subtasks").value || "6");
      const payload = {
        requirement,
        workspace: state.selectedWorkspace,
        codebase_summary: codebaseSummary || null,
        max_phases: maxPhases,
        max_subtasks_per_phase: maxSubtasksPerPhase,
      };
      const result = await api("/api/execute-program", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.selectedJobId = result.job_id;
      if (result.queued) {
        streamStatus.textContent = `Program job submitted: ${truncate(result.job_id, 12)} queued.`;
      }
      await refreshStatus();
    }

    document.getElementById("execute-form").addEventListener("submit", async (event) => {
      try {
        await submitExecute(event);
      } catch (error) {
        alert(error.message);
      }
    });

    document.getElementById("program-form").addEventListener("submit", async (event) => {
      try {
        await submitProgram(event);
      } catch (error) {
        alert(error.message);
      }
    });

    document.getElementById("open-project-folder").addEventListener("click", async () => {
      try {
        const result = await api("/api/projects/open", {
          method: "POST",
          body: JSON.stringify({ workspace: state.selectedWorkspace }),
        });
        if (result && typeof result.message === "string" && result.message.trim()) {
          alert(result.message);
        }
      } catch (error) {
        alert(error.message);
      }
    });

    document.getElementById("refresh-button").addEventListener("click", async () => {
      try {
        await refreshStatus();
        await refreshArtifactTail(true);
      } catch (error) {
        alert(error.message);
      }
    });

    cancelJobButton.addEventListener("click", async () => {
      try {
        await cancelSelectedJob();
      } catch (error) {
        alert(error.message);
      }
    });

    retryJobButton.addEventListener("click", async () => {
      try {
        await retrySelectedJob();
      } catch (error) {
        alert(error.message);
      }
    });

    refreshStatus().catch((error) => {
      globalStatus.textContent = `Failed to load project status: ${error.message}`;
    });
    refreshArtifactTail(true).catch(() => null);
    startEventStream();
    window.addEventListener("beforeunload", () => {
      closeEventStream();
    });
  </script>
</body>
</html>
"""
    return (
        template
        .replace("__PROVIDER__", html.escape(provider))
        .replace("__WORKSPACE__", html.escape(str(default_workspace)))
        .replace("__SELECTED_WORKSPACE__", html.escape(str(selected_workspace)))
        .replace("__PROJECT_NAME__", html.escape(project_name))
    )


def create_app(
    *,
    provider: str = _DEFAULT_PROVIDER,
    workspace: str | Path = _DEFAULT_WORKSPACE,
    api_key: str | None = None,
    bind_host: str = _DEFAULT_HOST,
    allow_unsecure: bool = False,
) -> FastAPI:
    workspace_root = Path(workspace).resolve()
    if not workspace_root.exists() or not workspace_root.is_dir():
        raise ValueError(f"Workspace path is invalid: {workspace_root}")

    app = FastAPI(title="Senior Agent Control Center", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.provider = provider.lower()
    app.state.role_provider_map = _resolve_role_provider_map(app.state.provider)
    app.state.provider_label = _format_role_provider_label(app.state.role_provider_map)
    app.state.default_workspace = workspace_root
    configured_api_key = (api_key or os.getenv(_API_KEY_ENV_NAME, "")).strip() or None
    app.state.api_key = configured_api_key
    app.state.bind_host = bind_host
    app.state.allow_unsecure = allow_unsecure
    app.state.jobs: dict[str, ExecutionJob] = {}
    app.state.jobs_lock = threading.Lock()
    app.state.execution_lock = threading.Lock()

    @app.get("/", response_class=HTMLResponse)
    def get_root() -> str:
        return _render_home_ui(
            default_workspace=app.state.default_workspace,
            provider=app.state.provider_label,
        )

    @app.get("/project", response_class=HTMLResponse)
    def get_project(workspace: str | None = Query(default=None)) -> str:
        selected_workspace = _resolve_workspace(app, workspace)
        return _render_ui(
            default_workspace=app.state.default_workspace,
            provider=app.state.provider_label,
            selected_workspace=selected_workspace,
        )

    @app.get("/api/health")
    def get_health() -> dict[str, Any]:
        with app.state.jobs_lock:
            summary = _system_status(app)
        return {
            "status": summary["status"],
            "provider": summary["provider"],
            "preferred_provider": summary["preferred_provider"],
            "role_providers": summary["role_providers"],
            "default_workspace": summary["default_workspace"],
            "jobs_total": summary["jobs_total"],
            "jobs_waiting": summary["jobs_waiting"],
            "jobs_running": summary["jobs_running"],
            "jobs_cancelled": summary["jobs_cancelled"],
        }

    @app.get("/api/projects")
    def get_projects() -> dict[str, Any]:
        return {"projects": _list_projects(app.state.default_workspace)}

    @app.post("/api/projects", status_code=201)
    def create_project(payload: CreateProjectRequest) -> dict[str, Any]:
        project_dir = _create_project(app.state.default_workspace, payload.project_name)
        return {
            "status": "created",
            "project_name": project_dir.name,
            "workspace": str(project_dir),
            "relative_path": project_dir.relative_to(app.state.default_workspace).as_posix(),
        }

    @app.post("/api/projects/open")
    def open_project(payload: OpenProjectRequest) -> dict[str, Any]:
        workspace_root = _resolve_workspace(app, payload.workspace)
        opened, message = _open_directory_in_file_manager(workspace_root)
        if not opened:
            raise HTTPException(status_code=500, detail=message)
        return {
            "status": "opened",
            "workspace": str(workspace_root),
            "message": message,
        }

    @app.get("/api/status")
    def get_status(
        job_id: str | None = Query(default=None),
        workspace: str | None = Query(default=None),
    ) -> dict[str, Any]:
        with app.state.jobs_lock:
            if job_id is None:
                target_workspace = _resolve_workspace(app, workspace) if workspace else None
                return _system_status(app, workspace=target_workspace)
            job = app.state.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
            return job.to_dict()

    @app.get("/api/events")
    async def stream_status_events(
        request: Request,
        workspace: str | None = Query(default=None),
    ) -> StreamingResponse:
        target_workspace = _resolve_workspace(app, workspace) if workspace else None

        async def event_stream() -> Any:
            while True:
                if await request.is_disconnected():
                    break

                with app.state.jobs_lock:
                    snapshot = _system_status(app, workspace=target_workspace)
                payload = {
                    "type": "status",
                    "timestamp": _utc_now_iso(),
                    "status": snapshot,
                }
                yield _sse_event("status", payload)
                await asyncio.sleep(1.0)

        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers=headers,
        )

    @app.get("/api/artifacts/tail")
    def get_artifact_tail(
        workspace: str | None = Query(default=None),
        job_id: str | None = Query(default=None),
        lines: int = Query(default=80, ge=1, le=400),
    ) -> dict[str, Any]:
        target_workspace = _resolve_workspace(app, workspace)
        preferred_reports_dir: str | None = None
        if job_id:
            with app.state.jobs_lock:
                job = app.state.jobs.get(job_id)
                if job is None:
                    raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
                if job.workspace != target_workspace:
                    raise HTTPException(
                        status_code=400,
                        detail="job_id does not belong to requested workspace.",
                    )
                result_payload = job.result if isinstance(job.result, dict) else {}
                preferred_raw = str(result_payload.get("reports_dir") or "").strip()
                preferred_reports_dir = preferred_raw or None

        artifact = _find_latest_artifact_file(
            workspace=target_workspace,
            preferred_reports_dir=preferred_reports_dir,
        )
        if artifact is None:
            return {
                "status": "empty",
                "workspace": str(target_workspace),
                "artifact_path": None,
                "content": "",
                "line_count": 0,
            }

        content = _tail_file_text(artifact, lines=lines)
        relative_path = artifact.relative_to(target_workspace).as_posix()
        return {
            "status": "ok",
            "workspace": str(target_workspace),
            "artifact_path": relative_path,
            "content": content,
            "line_count": len(content.splitlines()) if content else 0,
            "modified_at": datetime.fromtimestamp(artifact.stat().st_mtime, timezone.utc).isoformat(),
        }

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        reason = f"Cancelled by user at {datetime.now(timezone.utc).isoformat()}."
        job = _cancel_job(app=app, job_id=job_id, reason=reason)
        return {
            "status": "ok",
            "job_id": job_id,
            "job_status": job.status,
            "cancel_requested": job.cancel_requested,
            "cancel_reason": job.cancel_reason,
        }

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        with app.state.jobs_lock:
            previous = app.state.jobs.get(job_id)
            if previous is None:
                raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
            if previous.status in {"queued", "waiting", "running"}:
                raise HTTPException(
                    status_code=409,
                    detail=f"Job is still active and cannot be retried: {job_id}",
                )
            retry = _build_retry_job(previous=previous)
            app.state.jobs[retry.job_id] = retry
            _prune_jobs(app)

        _enqueue_job_execution(
            app=app,
            background_tasks=background_tasks,
            job=retry,
        )
        response = _build_job_response(retry)
        response["retried_from"] = job_id
        return response

    @app.post("/api/execute", status_code=202)
    def execute(
        request: Request,
        payload: ExecuteRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        _require_api_key(request, app)
        requirement = payload.requirement.strip()
        if not requirement:
            raise HTTPException(status_code=400, detail="requirement must not be empty.")

        workspace_root = _resolve_workspace(app, payload.workspace)
        job_id = uuid4().hex
        created_at = _utc_now_iso()
        job = ExecutionJob(
            job_id=job_id,
            job_type="execute_feature",
            workspace=workspace_root,
            payload={
                "requirement": requirement,
                "codebase_summary": (payload.codebase_summary or "").strip(),
            },
            created_at=created_at,
        )
        with app.state.jobs_lock:
            app.state.jobs[job_id] = job
            _prune_jobs(app)
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={
                "stage": "queued",
                "current_task": "Waiting for available execution slot",
            },
            append_hook="Job submitted and queued.",
        )

        _enqueue_job_execution(app=app, background_tasks=background_tasks, job=job)
        return _build_job_response(job)

    @app.post("/api/execute-program", status_code=202)
    def execute_program(
        request: Request,
        payload: ProgramExecuteRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        _require_api_key(request, app)
        requirement = payload.requirement.strip()
        if not requirement:
            raise HTTPException(status_code=400, detail="requirement must not be empty.")

        workspace_root = _resolve_workspace(app, payload.workspace)
        job_id = uuid4().hex
        created_at = _utc_now_iso()
        job = ExecutionJob(
            job_id=job_id,
            job_type="execute_program",
            workspace=workspace_root,
            payload={
                "requirement": requirement,
                "codebase_summary": (payload.codebase_summary or "").strip(),
                "max_phases": payload.max_phases,
                "max_subtasks_per_phase": payload.max_subtasks_per_phase,
            },
            created_at=created_at,
        )
        with app.state.jobs_lock:
            app.state.jobs[job_id] = job
            _prune_jobs(app)
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={
                "stage": "queued",
                "current_task": "Waiting for available execution slot",
            },
            append_hook="Program job submitted and queued.",
        )

        _enqueue_job_execution(app=app, background_tasks=background_tasks, job=job)
        return _build_job_response(job)

    @app.post("/api/heal", status_code=202)
    def heal(
        request: Request,
        payload: HealRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        _require_api_key(request, app)
        if not _is_local_bind_host(app.state.bind_host) and not app.state.allow_unsecure:
            raise HTTPException(
                status_code=403,
                detail=(
                    "The /api/heal endpoint is restricted when server is not bound to localhost. "
                    "Restart with --unsecure to override."
                ),
            )
        command = payload.command.strip()
        if not command:
            raise HTTPException(status_code=400, detail="command must not be empty.")

        workspace_root = _resolve_workspace(app, payload.workspace)
        validation_commands = [
            entry.strip()
            for entry in (payload.validation_commands or [])
            if entry.strip()
        ]

        job_id = uuid4().hex
        created_at = _utc_now_iso()
        job = ExecutionJob(
            job_id=job_id,
            job_type="self_heal",
            workspace=workspace_root,
            payload={
                "command": command,
                "max_attempts": payload.max_attempts,
                "validation_commands": validation_commands or None,
            },
            created_at=created_at,
        )
        with app.state.jobs_lock:
            app.state.jobs[job_id] = job
            _prune_jobs(app)
        _update_job_result_state(
            app=app,
            job_id=job_id,
            updates={
                "stage": "queued",
                "current_task": "Waiting for available execution slot",
            },
            append_hook="Self-heal job submitted and queued.",
        )

        _enqueue_job_execution(app=app, background_tasks=background_tasks, job=job)
        return _build_job_response(job)

    return app


def run_server(
    *,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    provider: str = _DEFAULT_PROVIDER,
    workspace: str | Path = _DEFAULT_WORKSPACE,
    verbose: bool = False,
    api_key: str | None = None,
    allow_unsecure: bool = False,
) -> None:
    app = create_app(
        provider=provider,
        workspace=workspace,
        api_key=api_key,
        bind_host=host,
        allow_unsecure=allow_unsecure,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="debug" if verbose else "info",
    )


__all__ = ["create_app", "run_server"]
