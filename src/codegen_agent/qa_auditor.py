import json
import os
import re
from pathlib import Path
from .models import QAReport, PipelineReport
from .llm.protocol import LLMClient
from .utils import find_json_in_text, prune_prompt

QA_SYSTEM_PROMPT = """You are an expert QA Auditor and Senior Developer.
Your goal is to audit a completed software project and provide a quality report in JSON format.
Use ONLY the provided evidence snapshot as ground truth.
Hard rules:
- Do not report a file as missing if it appears in known_files or workspace_files_sample.
- Do not claim a validation command failed when validation.healing_success is true
  and validation.last_failed_command is null.
- If evidence is ambiguous, place it in suggestions, not issues.
The report must include:
- score: A number from 0 to 100.
- issues: A list of identified bugs or poor practices (strings).
- suggestions: A list of improvements (strings).
- approved: A boolean indicating if the project is ready for delivery.

Respond ONLY with the JSON block."""

QA_USER_PROMPT_TEMPLATE = """Project Summary:
{pipeline_summary}

Audit the project and provide a report."""

_IGNORED_QA_DIRS = {
    ".codegen_agent",
    ".git",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "build",
    "dist",
    ".mypy_cache",
}


def _tail_text(text: str, max_lines: int = 20, max_chars: int = 1500) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    clipped = "\n".join(lines[-max_lines:])
    if len(clipped) <= max_chars:
        return clipped
    return clipped[-max_chars:]


def _extract_candidate_paths(text: str) -> list[str]:
    candidates = re.findall(r"`([^`]+)`", text or "")
    candidates.extend(re.findall(r"\b([A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+)\b", text or ""))
    normalized: list[str] = []
    for item in candidates:
        path = item.replace("\\", "/").lstrip("./")
        if path:
            normalized.append(path)
    return normalized

class QAAuditor:
    def __init__(self, llm_client: LLMClient, workspace: str = "."):
        self.llm_client = llm_client
        self.workspace = workspace

    def _workspace_files_sample(self, max_files: int = 160) -> list[str]:
        root = Path(self.workspace).resolve()
        if not root.exists():
            return []

        sampled: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _IGNORED_QA_DIRS and not d.startswith(".")
            ]
            for name in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
                sampled.append(rel)
                if len(sampled) >= max_files:
                    return sampled
        return sampled

    @staticmethod
    def _dependency_summary(payload) -> dict:
        if not isinstance(payload, dict):
            return {}
        return {
            "installed_manifests": payload.get("installed_manifests", []),
            "errors": payload.get("errors", []),
            "conftest_injected": payload.get("conftest_injected", False),
            "conftest_injected_post_tests": payload.get("conftest_injected_post_tests", False),
        }

    @staticmethod
    def _validation_evidence(report: PipelineReport) -> dict:
        healing = report.healing_report
        final = healing.final_command_result if healing else None
        return {
            "declared_commands": (
                report.test_suite.validation_commands
                if report.test_suite else (
                    report.architecture.global_validation_commands
                    if report.architecture else []
                )
            ),
            "healing_success": healing.success if healing else False,
            "healing_attempts": len(healing.attempts) if healing else 0,
            "blocked_reason": healing.blocked_reason if healing else None,
            "last_failed_command": (
                {
                    "command": final.command,
                    "exit_code": final.exit_code,
                    "stdout_tail": _tail_text(final.stdout),
                    "stderr_tail": _tail_text(final.stderr),
                } if final else None
            ),
        }

    def _build_summary(self, report: PipelineReport) -> dict:
        """Build a compact, high-signal summary for QA scoring."""
        features = report.plan.features if report.plan else []
        file_tree = report.architecture.file_tree if report.architecture else []
        nodes = report.architecture.nodes if report.architecture else []
        generated_files = report.execution_result.generated_files if report.execution_result else []
        test_files = list(report.test_suite.test_files.keys()) if report.test_suite else []
        healing_attempts = report.healing_report.attempts if report.healing_report else []
        workspace_sample = self._workspace_files_sample()

        known_files = set(file_tree[:200])
        known_files.update(f.file_path for f in generated_files[:200])
        known_files.update(test_files[:200])
        known_files.update(workspace_sample[:200])

        return {
            "prompt": report.prompt[-800:],
            "plan": {
                "project_name": report.plan.project_name if report.plan else None,
                "tech_stack": report.plan.tech_stack if report.plan else None,
                "entry_point": report.plan.entry_point if report.plan else None,
                "feature_count": len(features),
                "features": [f.title for f in features[:12]],
            },
            "architecture": {
                "file_count": len(file_tree),
                "files_sample": file_tree[:50],
                "node_count": len(nodes),
                "validation_commands": report.architecture.global_validation_commands if report.architecture else [],
            },
            "execution": {
                "success": report.execution_result is not None,
                "generated_file_count": len(generated_files),
                "failed_nodes": report.execution_result.failed_nodes if report.execution_result else [],
                "skipped_nodes": report.execution_result.skipped_nodes if report.execution_result else [],
            },
            "tests": {
                "framework": report.test_suite.framework if report.test_suite else None,
                "test_file_count": len(test_files),
                "test_files_sample": test_files[:20],
                "validation_commands": report.test_suite.validation_commands if report.test_suite else [],
            },
            "healing": {
                "success": report.healing_report.success if report.healing_report else False,
                "attempts": len(healing_attempts),
                "blocked_reason": report.healing_report.blocked_reason if report.healing_report else None,
            },
            "evidence": {
                "known_files": sorted(path for path in known_files if path)[:220],
                "workspace_files_sample": workspace_sample[:160],
                "dependency_resolution": self._dependency_summary(report.dependency_resolution),
                "validation": self._validation_evidence(report),
            },
        }

    @staticmethod
    def _normalize_text_list(values) -> list[str]:
        normalized: list[str] = []
        if not isinstance(values, list):
            return normalized
        for item in values:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                parts = []
                for key in ("severity", "title", "file", "issue", "details"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(f"{key}={value.strip()}")
                text = " | ".join(parts) if parts else json.dumps(item, sort_keys=True)
            else:
                text = str(item).strip()
            if text:
                normalized.append(text)
        return normalized

    @staticmethod
    def _filter_contradictions(
        issues: list[str],
        known_files: set[str],
        validation: dict,
    ) -> list[str]:
        filtered: list[str] = []
        for issue in issues:
            lower = issue.lower()
            mentions_missing_file = any(
                token in lower
                for token in ("missing", "does not exist", "not found", "file or directory not found")
            )
            if mentions_missing_file:
                paths = _extract_candidate_paths(issue)
                if any(path in known_files for path in paths):
                    continue

            mentions_command_failure = any(
                token in lower
                for token in (
                    "validation command fails",
                    "validation commands fail",
                    "does not pass",
                    "fails with",
                    "ruff check",
                    "pytest",
                )
            )
            if (
                mentions_command_failure
                and validation.get("healing_success")
                and validation.get("last_failed_command") is None
            ):
                continue
            filtered.append(issue)
        return filtered

    def _normalize_report_data(self, data: dict, summary: dict) -> dict:
        issues = self._normalize_text_list(data.get("issues", []))
        suggestions = self._normalize_text_list(data.get("suggestions", []))
        known_files = set(summary.get("evidence", {}).get("known_files", []))
        validation = summary.get("evidence", {}).get("validation", {})
        issues = self._filter_contradictions(issues, known_files, validation)

        raw_score = data.get("score", 0)
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(100.0, score))

        approved_raw = data.get("approved", False)
        approved = bool(approved_raw)
        if not issues and validation.get("healing_success") and not validation.get("blocked_reason"):
            approved = True
            score = max(score, 85.0)

        return {
            "score": score,
            "issues": issues,
            "suggestions": suggestions,
            "approved": approved,
        }

    async def audit(self, report: PipelineReport) -> QAReport:
        """Audits the project and generates a QA report."""
        summary = self._build_summary(report)
        user_prompt = QA_USER_PROMPT_TEMPLATE.format(pipeline_summary=json.dumps(summary, indent=2))
        user_prompt = prune_prompt(user_prompt, max_chars=12_000)
        response = await self.llm_client.generate(user_prompt, system_prompt=QA_SYSTEM_PROMPT)
        
        data = find_json_in_text(response)
        if not data or not isinstance(data, dict):
            raise ValueError(f"Failed to extract JSON from QA response: {response}")

        normalized = self._normalize_report_data(data, summary)
        return QAReport(**normalized)
