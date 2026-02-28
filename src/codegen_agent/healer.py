import os
import re
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional
from .models import HealingReport, HealAttempt, FailureType
from .llm.protocol import LLMClient
from .classifier import classify_failure
from .utils import run_shell_command, extract_code_from_markdown, prune_prompt

logger = logging.getLogger(__name__)

HEALER_SYSTEM_PROMPT = """You are an expert Software Engineer specializing in debugging and fixing code.
Your goal is to fix a failing command by modifying the source code.
Return ONLY the full corrected file content for the target file. No markdown fences or commentary."""

HEALER_USER_PROMPT_TEMPLATE = """Failing command: {command}
Failure type: {failure_type}
Error output:
{error_output}

Target file: {file_path}
Current content:
{file_content}

Fix the code to resolve the error."""

STATIC_ISSUE_HEALER_USER_PROMPT_TEMPLATE = """Static consistency issues were detected:
{issues}

Target file: {file_path}
Current content:
{file_content}

Fix the file so all listed issues are resolved. Keep behavior unchanged beyond the required fixes."""

ALLOWED_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".html", ".css", ".json", ".md", ".txt"}

# Pre-compile file-extraction patterns once at module load (avoids per-call recompilation)
_EXT_PATTERN = "|".join(re.escape(ext[1:]) for ext in sorted(ALLOWED_EXTENSIONS) if ext.startswith("."))
_FILE_QUOTED_RE = re.compile(rf'File "([a-zA-Z0-9_./\-]+\.(?:{_EXT_PATTERN}))(?!\w)"')
_FILE_GENERAL_RE = re.compile(rf'\b([a-zA-Z0-9_./\-]+\.(?:{_EXT_PATTERN}))(?!\w)')
_MISSING_TOOL_PATTERNS = (
    re.compile(r"(?:/bin/sh:\s*)?(?P<tool>[a-zA-Z0-9_.-]+): command not found"),
    re.compile(r"'(?P<tool>[^']+)' is not recognized as an internal or external command", re.IGNORECASE),
    re.compile(r"No such file or directory: '(?P<tool>[^']+)'"),
)
_PYTHON_MISSING_MODULE_RE = re.compile(
    r"""ModuleNotFoundError:\s*No module named ['"](?P<name>[A-Za-z0-9_.-]+)['"]"""
)
_IGNORED_RUNTIME_DIRS = {
    ".codegen_agent",
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
}


def _is_test_file(path: str) -> bool:
    name = os.path.basename(path)
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
        or "/tests/" in path
        or path.startswith("tests/")
    )


_HEALER_ERROR_MAX_LINES = 60    # keep the tail — errors are at the bottom of pytest output
_HEALER_FILE_CONTENT_MAX = 8_000  # chars; ~200 lines; keep the tail for the same reason


def _truncate_error_output(text: str, max_lines: int = _HEALER_ERROR_MAX_LINES) -> str:
    """Preserve the tail of error output where the actual failure is reported."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    dropped = len(lines) - max_lines
    return f"[... {dropped} lines truncated ...]\n" + "\n".join(lines[-max_lines:])


def _cap_file_content(content: str, max_chars: int = _HEALER_FILE_CONTENT_MAX) -> str:
    """Keep the tail of a large file — recent changes (the bug) tend to be at the bottom."""
    if len(content) <= max_chars:
        return content
    return f"# [...truncated — showing last {max_chars} chars...]\n" + content[-max_chars:]


def _missing_tool_from_output(command: str, stdout: str, stderr: str) -> Optional[str]:
    """Return missing executable name when failure is an environment/tooling issue."""
    text = f"{stdout}\n{stderr}"
    for pattern in _MISSING_TOOL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group("tool")
    return None


def _is_ignored_runtime_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    return any(part in _IGNORED_RUNTIME_DIRS for part in parts)


def _conftest_bootstrap_content() -> str:
    return (
        "import os\n"
        "import sys\n\n"
        "ROOT = os.path.dirname(os.path.abspath(__file__))\n"
        "if ROOT not in sys.path:\n"
        "    sys.path.insert(0, ROOT)\n"
    )


def _consolidate_commands(commands: List[str]) -> List[str]:
    """Merge all-pytest command lists into a single pytest -q -x invocation."""
    if not commands:
        return commands
    all_pytest = all(
        "pytest" in cmd or "python -m pytest" in cmd
        for cmd in commands
    )
    if all_pytest:
        return ["pytest -q -x"]
    return commands


class Healer:
    def __init__(
        self,
        llm_client: LLMClient,
        workspace: str,
        max_attempts: int = 3,
        allow_test_file_edits: bool = False,
    ):
        self.llm_client = llm_client
        self.workspace = workspace
        self.max_attempts = max_attempts
        self.allow_test_file_edits = allow_test_file_edits

    async def heal(self, validation_commands: List[str]) -> HealingReport:
        """Runs the healing loop: run tests -> fix all failures in parallel -> retry."""
        attempts = []
        failures = []

        for i in range(1, self.max_attempts + 1):
            consolidated = _consolidate_commands(validation_commands)
            run_tasks = [asyncio.to_thread(run_shell_command, cmd, cwd=self.workspace) for cmd in consolidated]
            results = await asyncio.gather(*run_tasks)

            # If consolidated command failed and we collapsed multiple commands, retry individually
            if consolidated != validation_commands and any(r.exit_code != 0 for r in results):
                run_tasks = [asyncio.to_thread(run_shell_command, cmd, cwd=self.workspace) for cmd in validation_commands]
                results = await asyncio.gather(*run_tasks)

            failures = [res for res in results if res.exit_code != 0]

            if not failures:
                return HealingReport(success=True, attempts=attempts, final_command_result=results[-1] if results else None)

            # Fix all failures concurrently; deduplicate by target file to avoid write conflicts
            fix_tasks = [self._fix_single_failure(failure, i) for failure in failures]
            fix_results = await asyncio.gather(*fix_tasks, return_exceptions=True)

            batch_attempts = []
            blocked = None
            for res in fix_results:
                if isinstance(res, Exception):
                    blocked = str(res)
                elif res is None:
                    pass  # skipped (duplicate file or path error)
                elif isinstance(res, str):
                    blocked = res  # blocked_reason string
                else:
                    batch_attempts.append(res)

            attempts.extend(batch_attempts)

            if blocked and not batch_attempts:
                return HealingReport(
                    success=False,
                    attempts=attempts,
                    final_command_result=failures[0],
                    blocked_reason=blocked,
                )

        return HealingReport(success=False, attempts=attempts, final_command_result=failures[0] if failures else None)

    async def heal_static_issues(
        self,
        issues_by_file: Dict[str, List[str]],
        attempt_number: int = 0,
    ) -> List[HealAttempt]:
        """Apply targeted fixes for static issues before test-driven healing."""
        async def _fix_static_issue(target_file: str, issues: List[str]) -> Optional[HealAttempt]:
            full_path, path_error = self._resolve_target_path(target_file)
            if path_error:
                logger.warning("Skipping static heal target %s: %s", target_file, path_error)
                return None
            if not full_path:
                return None
            if _is_test_file(target_file) and not self.allow_test_file_edits:
                logger.warning("Skipping static heal for test file: %s", target_file)
                return None

            with open(full_path, 'r') as f:
                content = f.read()

            issue_lines = "\n".join(f"- {issue}" for issue in issues)
            prompt = STATIC_ISSUE_HEALER_USER_PROMPT_TEMPLATE.format(
                issues=issue_lines,
                file_path=target_file,
                file_content=_cap_file_content(content),
            )
            prompt = prune_prompt(prompt, max_chars=16_000)
            fixed_content = await self.llm_client.generate(prompt, system_prompt=HEALER_SYSTEM_PROMPT)
            fixed_content = self._normalize_content(fixed_content)

            with open(full_path, 'w') as f:
                f.write(fixed_content)

            return HealAttempt(
                attempt_number=attempt_number,
                failure_type=FailureType.BUILD_ERROR,
                fix_applied=f"Static fix for {target_file}",
                changed_files=[target_file],
            )

        tasks = [
            _fix_static_issue(target_file, issues)
            for target_file, issues in issues_by_file.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        attempts: List[HealAttempt] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            if result is None:
                continue
            attempts.append(result)
        return attempts

    async def _fix_single_failure(self, last_result, attempt_number: int):
        """Attempt to fix one failing command. Returns HealAttempt, a blocked_reason str, or None."""
        # Don't burn an LLM call on a timed-out process — it's a structural issue, not a code bug.
        if last_result.exit_code == -1 and "timeout" in (last_result.stderr or "").lower():
            return f"Command timed out: {last_result.command!r}. Cannot heal a hanging process via code changes."
        missing_tool = _missing_tool_from_output(
            last_result.command,
            last_result.stdout,
            last_result.stderr,
        )
        if missing_tool:
            return (
                f"Missing tool '{missing_tool}' required by validation command "
                f"{last_result.command!r}. Install it in the environment; "
                "this is not healable via source edits."
            )

        auto_fix = await self._apply_known_auto_fixes(last_result, attempt_number)
        if auto_fix:
            return auto_fix

        deterministic_fix = self._fix_pytest_import_path_if_needed(last_result, attempt_number)
        if deterministic_fix:
            return deterministic_fix

        failure_type = classify_failure(last_result.command, last_result.stdout, last_result.stderr)

        target_file = self._extract_target_file(last_result.stderr)
        if not target_file:
            target_file = self._extract_target_file(last_result.stdout)
        if not target_file:
            target_file = self._get_most_recent_file()

        if not target_file:
            return "Could not identify target file from error output."
        if _is_test_file(target_file) and not self.allow_test_file_edits:
            return f"Refusing to edit test file {target_file}. No source-file target found."

        full_path, path_error = self._resolve_target_path(target_file)
        if path_error:
            return path_error
        if not full_path:
            return f"Target file {target_file} not found."

        with open(full_path, 'r') as f:
            content = f.read()

        prompt = HEALER_USER_PROMPT_TEMPLATE.format(
            command=last_result.command,
            failure_type=failure_type.value,
            error_output=_truncate_error_output(f"{last_result.stdout}\n{last_result.stderr}"),
            file_path=target_file,
            file_content=_cap_file_content(content),
        )
        prompt = prune_prompt(prompt, max_chars=16_000)

        fixed_content = await self.llm_client.generate(prompt, system_prompt=HEALER_SYSTEM_PROMPT)
        fixed_content = self._normalize_content(fixed_content)

        with open(full_path, 'w') as f:
            f.write(fixed_content)

        return HealAttempt(
            attempt_number=attempt_number,
            failure_type=failure_type,
            fix_applied=f"Fixed {target_file}",
            changed_files=[target_file],
        )

    def _fix_pytest_import_path_if_needed(self, last_result, attempt_number: int) -> Optional[HealAttempt]:
        command = (last_result.command or "").lower()
        if "pytest" not in command:
            return None

        output = f"{last_result.stdout}\n{last_result.stderr}"
        match = _PYTHON_MISSING_MODULE_RE.search(output)
        if not match:
            return None
        module_name = match.group("name").strip()
        if not module_name or "." in module_name:
            return None

        module_file = Path(self.workspace) / f"{module_name}.py"
        if not module_file.exists():
            return None

        conftest_path = Path(self.workspace) / "conftest.py"
        if conftest_path.exists():
            return None

        conftest_path.write_text(_conftest_bootstrap_content())
        return HealAttempt(
            attempt_number=attempt_number,
            failure_type=FailureType.BUILD_ERROR,
            fix_applied=f"Added conftest.py to fix pytest import path for module '{module_name}'",
            changed_files=["conftest.py"],
        )

    async def _apply_known_auto_fixes(self, last_result, attempt_number: int) -> Optional[HealAttempt]:
        command = (last_result.command or "").strip()
        if re.match(r"^ruff\s+check(\s|$)", command) and "--fix" not in command:
            fix_command = re.sub(r"^ruff\s+check", "ruff check --fix", command, count=1)
            fix_result = await asyncio.to_thread(run_shell_command, fix_command, cwd=self.workspace)
            if fix_result.exit_code in (0, 1):
                note = None
                if fix_result.exit_code != 0:
                    note = _truncate_error_output(f"{fix_result.stdout}\n{fix_result.stderr}", max_lines=20)
                return HealAttempt(
                    attempt_number=attempt_number,
                    failure_type=FailureType.LINT_TYPE_FAILURE,
                    fix_applied=f"Applied auto-fix via `{fix_command}`",
                    changed_files=[],
                    note=note,
                )
        return None

    def _resolve_target_path(self, target_file: str) -> tuple[Optional[Path], Optional[str]]:
        workspace_path = Path(self.workspace).resolve()
        try:
            full_path = (workspace_path / Path(target_file)).resolve()
            full_path.relative_to(workspace_path)
        except (ValueError, OSError):
            return None, f"Security alert: Invalid path {target_file} attempted outside workspace."
        if not full_path.exists():
            return None, f"Target file {target_file} not found."
        return full_path, None

    def _extract_target_file(self, output: str) -> Optional[str]:
        """Extracts the most likely target file from error output."""
        if not output:
            return None
        matches = _FILE_QUOTED_RE.findall(output)
        if not matches:
            matches = _FILE_GENERAL_RE.findall(output)
            
        if not matches:
            return None
        
        for match in matches:
            match = match.strip(".,:;)]}")
            if _is_ignored_runtime_path(match):
                continue
            if not self.allow_test_file_edits and _is_test_file(match):
                continue
            if os.path.exists(os.path.join(self.workspace, match)):
                return match
        return None

    def _get_most_recent_file(self) -> Optional[str]:
        """Finds the most recently modified source file in the workspace."""
        files = []
        for root, dirnames, filenames in os.walk(self.workspace):
            dirnames[:] = [
                d for d in dirnames
                if d not in _IGNORED_RUNTIME_DIRS and not d.startswith(".")
            ]
            for f in filenames:
                if any(f.endswith(ext) for ext in ALLOWED_EXTENSIONS):
                    path = os.path.join(root, f)
                    rel = os.path.relpath(path, self.workspace)
                    if _is_ignored_runtime_path(rel):
                        continue
                    if not self.allow_test_file_edits and _is_test_file(rel):
                        continue
                    files.append((path, os.path.getmtime(path)))
        
        if not files:
            return None
        
        files.sort(key=lambda x: x[1], reverse=True)
        return os.path.relpath(files[0][0], self.workspace)

    def _normalize_content(self, content: str) -> str:
        code_blocks = extract_code_from_markdown(content)
        if code_blocks:
            return code_blocks[0].strip()
        return content.strip()
