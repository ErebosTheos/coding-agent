import os
import re
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional
from .models import HealingReport, HealAttempt, FailureType
from .llm.protocol import LLMClient
from .classifier import classify_failure
from .utils import run_shell_command, extract_code_from_markdown

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
        attempts: List[HealAttempt] = []
        for target_file, issues in issues_by_file.items():
            full_path, path_error = self._resolve_target_path(target_file)
            if path_error:
                logger.warning("Skipping static heal target %s: %s", target_file, path_error)
                continue
            if not full_path:
                continue
            if _is_test_file(target_file) and not self.allow_test_file_edits:
                logger.warning("Skipping static heal for test file: %s", target_file)
                continue

            with open(full_path, 'r') as f:
                content = f.read()

            issue_lines = "\n".join(f"- {issue}" for issue in issues)
            prompt = STATIC_ISSUE_HEALER_USER_PROMPT_TEMPLATE.format(
                issues=issue_lines,
                file_path=target_file,
                file_content=content,
            )
            fixed_content = await self.llm_client.generate(prompt, system_prompt=HEALER_SYSTEM_PROMPT)
            fixed_content = self._normalize_content(fixed_content)

            with open(full_path, 'w') as f:
                f.write(fixed_content)

            attempts.append(HealAttempt(
                attempt_number=attempt_number,
                failure_type=FailureType.BUILD_ERROR,
                fix_applied=f"Static fix for {target_file}",
                changed_files=[target_file],
            ))
        return attempts

    async def _fix_single_failure(self, last_result, attempt_number: int):
        """Attempt to fix one failing command. Returns HealAttempt, a blocked_reason str, or None."""
        # Don't burn an LLM call on a timed-out process — it's a structural issue, not a code bug.
        if last_result.exit_code == -1 and "timeout" in (last_result.stderr or "").lower():
            return f"Command timed out: {last_result.command!r}. Cannot heal a hanging process via code changes."

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
            error_output=f"{last_result.stdout}\n{last_result.stderr}",
            file_path=target_file,
            file_content=content,
        )

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
            if not self.allow_test_file_edits and _is_test_file(match):
                continue
            if os.path.exists(os.path.join(self.workspace, match)):
                return match
        return None

    def _get_most_recent_file(self) -> Optional[str]:
        """Finds the most recently modified source file in the workspace."""
        files = []
        for root, _, filenames in os.walk(self.workspace):
            if ".codegen_agent" in root or ".git" in root:
                continue
            for f in filenames:
                if any(f.endswith(ext) for ext in ALLOWED_EXTENSIONS):
                    path = os.path.join(root, f)
                    rel = os.path.relpath(path, self.workspace)
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
