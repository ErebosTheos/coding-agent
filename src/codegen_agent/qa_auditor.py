import asyncio
import json
import os
import re
from pathlib import Path
from typing import Awaitable, Callable
from .models import QAReport, PipelineReport
from .llm.protocol import LLMClient
from .utils import find_json_in_text, prune_prompt

# Deterministic anti-pattern checks for fast per-file scan
_ANTIPATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'datetime\.utcnow\(\)'), "datetime.utcnow() is deprecated — use datetime.now(timezone.utc)"),
    (re.compile(r'(?<!\w)sessionmaker\('), "ORM sessionmaker detected — use async_sessionmaker for async code"),
    (re.compile(r'AsyncClient\(app='), "httpx.AsyncClient(app=...) deprecated — use ASGITransport"),
    (re.compile(r'@app\.on_event\('), "@app.on_event deprecated — use lifespan context manager"),
    (re.compile(r'SECRET_KEY\s*=\s*["\'][^"\']{4,}["\']'), "Possible hardcoded SECRET_KEY in source"),
    (re.compile(r'password\s*=\s*["\'][^"\']{3,}["\']'), "Possible hardcoded password in source"),
]

QA_SYSTEM_PROMPT = """You are a Senior Software Engineer performing a real code review.
You have been given the actual source code. Read it carefully and find real bugs.

Review the code for:
1. CORRECTNESS — Does the implementation match what was requested? Is every planned feature present?
2. BUGS — Import errors (functions called but not imported), NameErrors, missing None-checks,
   unhandled exceptions, logic errors, broken API routes, wrong HTTP methods.
3. SECURITY — Hardcoded secrets or insecure fallbacks, missing auth guards on protected endpoints,
   leaked sensitive fields (passwords/tokens in API responses), missing input validation.
4. COMPLETENESS — Are all planned API endpoints implemented? Do all routers have the right routes?
   Are all features from the spec present in the code?
5. TESTS — Do test files import and exercise the real source modules? Do tests have assertions?

Scoring (start at 100, deduct):
- Critical bug that crashes the app or exposes a security hole: -20 to -30 points each
- Missing feature from the spec or broken endpoint: -10 to -15 points each
- Logic bug or missing guard: -5 to -10 points each
- Minor issue: -2 to -5 points each

Hard rules:
- Do NOT report a file as missing if it appears in known_files.
- Do NOT penalise for linting tools (ruff, black, flake8).
- healing_success=false with healing_attempts=0 means healing was skipped, NOT that tests failed.
- Be specific: name the file and the exact line or pattern that has the issue.
- If the code compiles and tests pass (healing_success=true, no last_failed_command),
  start your review from 80 and deduct only for real issues you find in the source.

Return ONLY a JSON object:
{"score": <0-100>, "issues": ["<specific issue>", ...], "suggestions": ["<improvement>", ...], "approved": <true|false>}"""

QA_USER_PROMPT_TEMPLATE = """Original request:
{original_prompt}

Project summary:
{pipeline_summary}

Source code to review:
{source_code}

Review the source code above and return a JSON quality report."""

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
    def _filter_lint_commands(commands: list) -> list:
        """Remove pure linting/formatting commands that are not functional tests."""
        _LINT_PREFIXES = ("ruff", "black", "flake8", "isort", "pylint", "mypy", "pyflakes")
        return [
            cmd for cmd in (commands or [])
            if not any(cmd.strip().startswith(p) for p in _LINT_PREFIXES)
        ]

    @staticmethod
    def _validation_evidence(report: PipelineReport) -> dict:
        healing = report.healing_report
        final = healing.final_command_result if healing else None
        raw_cmds = (
            report.test_suite.validation_commands
            if report.test_suite else (
                report.architecture.global_validation_commands
                if report.architecture else []
            )
        )
        return {
            "declared_commands": QAAuditor._filter_lint_commands(raw_cmds),
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
                "validation_commands": self._filter_lint_commands(report.architecture.global_validation_commands if report.architecture else []),
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
                "validation_commands": self._filter_lint_commands(report.test_suite.validation_commands if report.test_suite else []),
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

            # Always drop issues that are purely about linting tools
            mentions_lint_failure = any(
                token in lower
                for token in ("ruff check", "ruff ", "black ", "flake8", "isort", "pylint")
            )
            if mentions_lint_failure:
                continue

            mentions_command_failure = any(
                token in lower
                for token in (
                    "validation command fails",
                    "validation commands fail",
                    "does not pass",
                    "fails with",
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

        return {
            "score": score,
            "issues": issues,
            "suggestions": suggestions,
            "approved": approved,
        }

    def _read_source_files(self, report: PipelineReport, max_chars: int = 16_000) -> str:
        """Read actual source file contents for the QA LLM to review.

        Prioritises source files over test files, skips binary/config/asset files.
        Caps total chars so the prompt stays within token budget.
        """
        _CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".php", ".rb"}
        _SKIP_NAMES = {
            "requirements.txt", "package-lock.json", "go.sum",
            "__init__.py", ".gitignore", "README.md",
        }
        generated = report.execution_result.generated_files if report.execution_result else []
        test_files = set(report.test_suite.test_files.keys()) if report.test_suite else set()

        # Source files first, then test files, skip tiny/empty/config
        ordered = sorted(
            generated,
            key=lambda f: (f.file_path in test_files, f.file_path),
        )

        parts: list[str] = []
        total = 0
        for gf in ordered:
            if os.path.basename(gf.file_path) in _SKIP_NAMES:
                continue
            if os.path.splitext(gf.file_path)[1].lower() not in _CODE_EXTS:
                continue
            # Read current on-disk content (may have been healed since execution)
            disk_path = Path(self.workspace) / gf.file_path
            try:
                content = disk_path.read_text(encoding="utf-8", errors="replace") if disk_path.exists() else gf.content
            except OSError:
                content = gf.content
            if not content.strip():
                continue
            entry = f"=== {gf.file_path} ===\n{content.strip()}"
            if total + len(entry) > max_chars:
                remaining = max_chars - total
                if remaining > 200:
                    parts.append(entry[:remaining] + "\n[... truncated ...]")
                break
            parts.append(entry)
            total += len(entry)

        return "\n\n".join(parts) if parts else "(no source files available)"

    def _quick_file_check(self, file_path: str, content: str) -> dict:
        """Fast deterministic scan — no LLM, instant per-file feedback."""
        issues: list[str] = []
        for pattern, msg in _ANTIPATTERNS:
            if pattern.search(content):
                issues.append(msg)
        return {
            "file": file_path,
            "issues": issues,
            "clean": len(issues) == 0,
            "lines": content.count("\n") + 1,
        }

    async def audit_streaming(
        self,
        report: PipelineReport,
        on_file_reviewed: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> QAReport:
        """Phase 1: fast deterministic per-file scan (publishes events as each file is checked).
        Phase 2: full LLM batch audit for final score and deep issues."""
        _CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".php", ".rb"}
        generated = report.execution_result.generated_files if report.execution_result else []

        for gf in generated:
            if os.path.splitext(gf.file_path)[1].lower() not in _CODE_EXTS:
                continue
            disk_path = Path(self.workspace) / gf.file_path
            try:
                content = disk_path.read_text(encoding="utf-8", errors="replace") if disk_path.exists() else (gf.content or "")
            except OSError:
                content = gf.content or ""
            result = self._quick_file_check(gf.file_path, content)
            if on_file_reviewed:
                await on_file_reviewed(gf.file_path, result)
            await asyncio.sleep(0)  # yield to event loop between files

        # Full LLM audit for final scored report
        return await self.audit(report)

    async def audit(self, report: PipelineReport) -> QAReport:
        """Audits the project by reading actual source code and generating a QA report."""
        summary = self._build_summary(report)
        source_code = self._read_source_files(report, max_chars=18_000)
        user_prompt = QA_USER_PROMPT_TEMPLATE.format(
            original_prompt=report.prompt[-1_000:],
            pipeline_summary=json.dumps(summary, indent=2),
            source_code=source_code,
        )
        user_prompt = prune_prompt(user_prompt, max_chars=28_000)
        response = await self.llm_client.generate(user_prompt, system_prompt=QA_SYSTEM_PROMPT)

        data = find_json_in_text(response)
        if not data or not isinstance(data, dict):
            raise ValueError(f"Failed to extract JSON from QA response: {response}")

        normalized = self._normalize_report_data(data, summary)
        return QAReport(**normalized)
