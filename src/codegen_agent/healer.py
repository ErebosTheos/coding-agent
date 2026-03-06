import hashlib
import os
import re
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional
from .models import HealingReport, HealAttempt, FailureType
from .llm.protocol import LLMClient
from .classifier import classify_failure
from .patch_cache import PatchCache
from .pattern_store import PatternStore
from .pytest_parser import (
    run_pytest_structured,
    format_structured_failures_for_prompt,
    PytestReport,
)
from .utils import run_shell_command, extract_code_from_markdown, prune_prompt, find_json_in_text, resolve_workspace_path

logger = logging.getLogger(__name__)

HEALER_SYSTEM_PROMPT = """You are an expert Software Engineer specializing in debugging and fixing code.

ANTI-TRUNCATION MANDATE (enforced by automated validator — truncated output is DISCARDED):
- Return the COMPLETE corrected file — every single line from the first to the last.
- If the file is 400 lines, your output must be ~400 lines. Never cut off mid-function.
- An automated check compares your output length to the original. If your output is <60%
  of the original length it is REJECTED and the file stays broken. Write it ALL.
- NEVER end your response with a partial line, hanging indent, or incomplete expression.
- Do NOT truncate or summarise the unchanged parts. Copy them verbatim.

CRITICAL RULES — violating any of these makes the fix worse than the original:
- Fix ONLY the specific error shown. Do NOT refactor, rename, or restructure surrounding code.
- Do NOT change function signatures, class names, or the public API of the file.
- Do NOT remove existing functionality — only change what is necessary to fix the error.
- If fixing an ImportError or NameError: look at the "Related files" section to find what
  is actually exported before writing any import statement.
- If fixing a missing import: add it to the existing import block, do not rewrite the whole file.
- No markdown fences, no commentary, no explanations before or after the code."""

HEALER_USER_PROMPT_TEMPLATE = """Failing command: {command}
Failure type: {failure_type}

Error output (root cause is often near the TOP, traceback at the bottom):
{error_output}

File to fix: {file_path}
{file_content}

Related files — what other modules in this project export (use this to write correct imports):
{related_files}

Return the complete corrected content of {file_path} only."""

HEALER_MULTI_FILE_PROMPT_TEMPLATE = """Failing command: {command}
Failure type: {failure_type}

Error output (root cause is often near the TOP, traceback at the bottom):
{error_output}

Fix ALL broken files listed below. Return a JSON object where keys are file paths and
values are the complete corrected file contents.
Example: {{"src/main.py": "...", "src/auth.py": "..."}}

Files to fix:
{files_to_fix}

Related files — what other modules export (use this to write correct imports):
{related_files}"""

STATIC_ISSUE_HEALER_USER_PROMPT_TEMPLATE = """Static consistency issues were detected:
{issues}

File to fix: {file_path}
{file_content}

Related files — what other modules export (use this to write correct imports):
{related_files}

Fix only the listed issues. Return the complete corrected content of {file_path}."""

ALLOWED_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".html", ".css", ".json", ".md", ".txt"}

# Pre-compile file-extraction patterns once at module load
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

# ── Async pytest-fixture patterns ─────────────────────────────────────────────
# Matches: @pytest.fixture[(args)]\n<indent>async def
_ASYNC_FIXTURE_RE = re.compile(
    r"@pytest\.fixture\b([^\n]*)\n(\s*async\s+def\s)",
    re.MULTILINE,
)
_PYTEST_ASYNCIO_IMPORT_RE = re.compile(r"^\s*import\s+pytest_asyncio", re.MULTILINE)

# ── Healer output validation ────────────────────────────────────────────────────
# Same truncation patterns as TruncationGuard in executor.py.
# A healer that writes truncated output is worse than no fix at all.
_HEALER_TRUNC_BRACKET_RE = re.compile(r'^\s*\[\.{2,}[^\]\n]*\]\s*$', re.MULTILINE)
# Lone indented identifier at EOF — potential mid-word truncation
_HEALER_MIDWORD_RE = re.compile(r'\n([ \t]+)([a-z_][a-z0-9_]*)\s*$')
# Partial assignment value at EOF, e.g. `    default=F` (False cut to F)
_HEALER_MIDASSIGN_RE = re.compile(r'\n[ \t]+\w+\s*=\s*[A-Z]\w{0,4}\s*$')
# Python keywords that are valid as the last token — not mid-word truncation
_HEALER_VALID_LAST_WORDS = frozenset({
    'pass', 'return', 'break', 'continue', 'else', 'finally', 'raise', 'yield',
    'true', 'false', 'none', 'and', 'or', 'not', 'in', 'is',
})
# Shrink threshold: reject fix if output is < 60% of original line count
_HEALER_SHRINK_RATIO = 0.60


def _is_healer_output_truncated(content: str) -> bool:
    """True if healer output appears LLM-truncated (mirrors executor._is_content_truncated)."""
    if bool(_HEALER_TRUNC_BRACKET_RE.search(content)):
        return True
    if bool(_HEALER_MIDASSIGN_RE.search(content)):
        return True
    m = _HEALER_MIDWORD_RE.search(content)
    if m:
        word = m.group(2).lower()
        if word not in _HEALER_VALID_LAST_WORDS:
            return True
    return False


# Agent-internal strings that indicate a file has been corrupted by injection.
# When the ORIGINAL file has these, the fix is expected to be much smaller — skip shrink check.
_AGENT_INJECTION_MARKERS = frozenset({
    "CachingLLMClient", "LLMRouter", "get_client_for_role", "HEALER_SYSTEM_PROMPT",
    "EXECUTOR_SYSTEM_PROMPT", "PlannerArchitect", "StreamingPlanArchExecutor",
    "from .caching_client import", "_BulkFileParser", "HealingReport", "ExecutionNode",
})


def _original_has_injection(original: str) -> bool:
    return any(marker in original for marker in _AGENT_INJECTION_MARKERS)


def _healer_output_ok(fixed: str, original: str) -> bool:
    """Return True if the healer's fixed content is valid and safe to write.

    Rejects output that is:
    - Truncated mid-word  (e.g. last line is `    to_enc`)
    - Contains [...] placeholder
    - More than 40% shorter than the original (unless the original was itself injected/bloated)
    """
    if not fixed or not fixed.strip():
        return False
    if _is_healer_output_truncated(fixed):
        return False
    if original:
        orig_lines = original.count('\n') + 1
        fix_lines  = fixed.count('\n') + 1
        # Skip shrink ratio when the original was corrupted by injection — fix is expected to be smaller
        if orig_lines > 30 and fix_lines < orig_lines * _HEALER_SHRINK_RATIO:
            if not _original_has_injection(original):
                return False
    return True

# Failure patterns that indicate test-infrastructure problems (not logic bugs).
# For these the healer should temporarily edit test files.
_TEST_INFRA_RE = re.compile(
    r"(async_generator|ScopeMismatch|asyncio.*fixture|fixture.*asyncio"
    r"|PytestUnraisableException|DeprecationWarning.*pytest_asyncio"
    r"|ERROR collecting|collection error|ImportError while importing"
    r"|is not a.*fixture|fixture.*not found)",
    re.IGNORECASE,
)

# Maximum LLM fix calls per heal round (prevents slow/messy parallel floods).
_LLM_FIX_CONCURRENCY = int(os.environ.get("CODEGEN_HEALER_FAN_OUT", "4"))

# Maximum broken files to attempt to fix in a single round.
_MAX_BROKEN_FILES_PER_ROUND = 4


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


_HEALER_ERROR_MAX_LINES = 80
_HEALER_FILE_HEAD_CHARS = 3_000   # imports / class defs are at the top
_HEALER_FILE_TAIL_CHARS = 5_000   # recent changes tend to be at the bottom
_HEALER_FILE_CONTENT_MAX = _HEALER_FILE_HEAD_CHARS + _HEALER_FILE_TAIL_CHARS


def _truncate_error_output(text: str, max_lines: int = _HEALER_ERROR_MAX_LINES) -> str:
    """Keep head + tail so both the root cause (top) and failing assertion (bottom) are visible.

    Python import chains put the root ImportError at the top.
    pytest puts the failing assertion at the bottom. We need both.
    """
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head_lines = max_lines // 3
    tail_lines = max_lines - head_lines
    dropped = len(lines) - head_lines - tail_lines
    return (
        "\n".join(lines[:head_lines])
        + f"\n[... {dropped} lines truncated ...]\n"
        + "\n".join(lines[-tail_lines:])
    )


def _cap_file_content(content: str) -> str:
    """Return head + tail of a large file."""
    if len(content) <= _HEALER_FILE_CONTENT_MAX:
        return content
    head = content[:_HEALER_FILE_HEAD_CHARS]
    tail = content[-_HEALER_FILE_TAIL_CHARS:]
    omitted = len(content) - _HEALER_FILE_HEAD_CHARS - _HEALER_FILE_TAIL_CHARS
    return f"{head}\n# [...{omitted} chars omitted...]\n{tail}"


def _missing_tool_from_output(command: str, stdout: str, stderr: str) -> Optional[str]:
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


def _failure_hash(failures: list) -> str:
    """Stable hash of failure outputs used by HealerLoopGuard to detect stuck loops."""
    combined = "|".join(
        f"{r.exit_code}:{(r.stdout + r.stderr)[-800:]}"
        for r in sorted(failures, key=lambda r: r.command)
    )
    return hashlib.sha256(combined.encode(), usedforsecurity=False).hexdigest()


def _file_set_hash(file_paths: frozenset[str]) -> str:
    """Stable hash of a set of file paths (for no-progress detection)."""
    combined = "|".join(sorted(file_paths))
    return hashlib.sha256(combined.encode(), usedforsecurity=False).hexdigest()


def _content_hash(workspace: str, file_paths) -> str:
    """SHA-256 of the current on-disk content of the given files (order-independent)."""
    parts = []
    for fp in sorted(file_paths):
        full = Path(workspace) / fp
        if full.exists():
            parts.append(hashlib.sha256(full.read_bytes(), usedforsecurity=False).hexdigest()[:16])
        else:
            parts.append("missing")
    return "|".join(parts)


def _patch_cache_key(
    failure_hash: str,
    file_paths,
    workspace: str,
    model: str = "",
) -> str:
    """Hardened cache key: failure hash + hash of target file contents + model.

    Including file-content hashes prevents stale patches from being applied
    after the files have been significantly changed between runs.
    """
    file_sig = _content_hash(workspace, file_paths)
    raw = f"{model}|{failure_hash}|{file_sig}"
    return hashlib.sha256(raw.encode(), usedforsecurity=False).hexdigest()


def _consolidate_commands(commands: List[str]) -> List[str]:
    """Merge all-pytest command lists into a single run showing ALL failures."""
    if not commands:
        return commands
    all_pytest = all(
        "pytest" in cmd or "python -m pytest" in cmd
        for cmd in commands
    )
    if all_pytest:
        return ["pytest -q --tb=short"]
    return commands


def _is_test_infra_failure(failures: list) -> bool:
    """True when failures look like test infrastructure (fixture/collection errors).

    For these the healer enables temporary test-file editing: the broken file
    is the test, not the app.
    """
    combined = " ".join(f.stdout + f.stderr for f in failures)
    return bool(_TEST_INFRA_RE.search(combined))


def _fix_async_pytest_fixtures(content: str) -> Optional[str]:
    """Replace @pytest.fixture + async def → @pytest_asyncio.fixture + async def.

    Also injects `import pytest_asyncio` if not already present.
    Returns fixed content or None if no change was needed.
    """
    if "async def" not in content or "@pytest.fixture" not in content:
        return None

    new_content = _ASYNC_FIXTURE_RE.sub(r"@pytest_asyncio.fixture\1\n\2", content)
    if new_content == content:
        return None

    # Ensure import is present
    if not _PYTEST_ASYNCIO_IMPORT_RE.search(new_content):
        # Insert after `import pytest` if present
        new_content = re.sub(
            r"^(import pytest\b)",
            r"\1\nimport pytest_asyncio",
            new_content,
            count=1,
            flags=re.MULTILINE,
        )
        # If still missing, prepend at top
        if not _PYTEST_ASYNCIO_IMPORT_RE.search(new_content):
            new_content = "import pytest_asyncio\n" + new_content

    return new_content


def _ensure_asyncio_mode_auto(workspace: str) -> bool:
    """Add asyncio_mode = auto to pyproject.toml or pytest.ini.

    Returns True if a config file was written/modified.
    """
    pyproject = Path(workspace) / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        if "asyncio_mode" in text:
            return False
        if "[tool.pytest.ini_options]" in text:
            new = text.replace(
                "[tool.pytest.ini_options]",
                '[tool.pytest.ini_options]\nasyncio_mode = "auto"',
                1,
            )
        else:
            new = text.rstrip() + '\n\n[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'
        pyproject.write_text(new, encoding="utf-8")
        return True

    pytest_ini = Path(workspace) / "pytest.ini"
    if pytest_ini.exists():
        text = pytest_ini.read_text(encoding="utf-8")
        if "asyncio_mode" in text:
            return False
        pytest_ini.write_text(text.rstrip() + "\nasyncio_mode = auto\n", encoding="utf-8")
        return True

    # Create minimal pytest.ini
    pytest_ini.write_text("[pytest]\nasyncio_mode = auto\n", encoding="utf-8")
    return True


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
        _cache_env = os.environ.get("CODEGEN_PATCH_CACHE", "1").strip()
        self._patch_cache: Optional[PatchCache] = (
            PatchCache(workspace) if _cache_env != "0" else None
        )
        # Model identifier for cache key hardening
        _model_attr = getattr(llm_client, "model", None) or getattr(llm_client, "_model", None)
        self._model_id: str = str(_model_attr) if _model_attr else ""
        # Cross-project pattern store: failure fingerprint → successful fix description
        self._pattern_store = PatternStore()

    async def heal(self, validation_commands: List[str]) -> HealingReport:
        """Run tests → extract ALL broken files → fix in parallel → repeat."""
        attempts: List[HealAttempt] = []
        last_failures = []
        _seen_failure_hashes: set[str] = set()   # HealerLoopGuard: identical output
        _seen_file_set_hashes: set[str] = set()  # No-progress: same broken file set
        _cache_hits = 0

        for attempt_num in range(1, self.max_attempts + 1):
            commands = _consolidate_commands(validation_commands)

            # Run all commands; for pytest commands also collect structured JSON report
            run_tasks = [
                asyncio.to_thread(run_shell_command, cmd, cwd=self.workspace)
                for cmd in commands
            ]
            # Run structured pytest in parallel with the plain run_tasks
            structured_tasks = [
                run_pytest_structured(cmd, self.workspace) for cmd in commands
            ]
            results, structured_results = await asyncio.gather(
                asyncio.gather(*run_tasks),
                asyncio.gather(*structured_tasks),
            )
            last_failures = [r for r in results if r.exit_code != 0]
            # Merge structured reports: pick the first non-None one with failures
            _pytest_report: Optional[PytestReport] = next(
                (pr for pr in structured_results if pr and pr.failures),
                None,
            )

            if not last_failures:
                return HealingReport(
                    success=True,
                    attempts=attempts,
                    final_command_result=results[-1] if results else None,
                    cache_hits=_cache_hits,
                )

            # ── HealerLoopGuard: stop on identical failure output ──────────────
            _fhash = _failure_hash(last_failures)
            if _fhash in _seen_failure_hashes:
                print(
                    f"  [Healer] Attempt {attempt_num}: failure output unchanged "
                    "across attempts — heal loop is stuck, stopping early."
                )
                break
            _seen_failure_hashes.add(_fhash)

            # ── Detect test-infrastructure failures: temporarily allow test edits ─
            _test_infra = _is_test_infra_failure(last_failures)
            _effective_allow_test_edits = self.allow_test_file_edits or _test_infra

            # ── PatchCache: apply known-good patch (zero LLM) ─────────────────
            _cache_key: Optional[str] = None
            if self._patch_cache:
                _broken_candidates = self._extract_broken_files_raw(last_failures)
                _cache_key = _patch_cache_key(
                    _fhash, _broken_candidates, self.workspace, self._model_id
                )
                cached_patch = self._patch_cache.get(_cache_key)
                if cached_patch:
                    _applied = []
                    for fp, content in cached_patch.items():
                        full = resolve_workspace_path(self.workspace, fp)
                        if full is None:
                            logger.warning("[Healer] PatchCache: skipping path outside workspace: %s", fp)
                            continue
                        if full.exists():
                            full.write_text(content, encoding="utf-8")
                            _applied.append(fp)
                    if _applied:
                        print(
                            f"  [PatchCache] Cache hit — applied {len(_applied)} cached patch(es)"
                            f" for attempt {attempt_num} (no LLM call)"
                        )
                        _cache_hits += 1
                        attempts.append(HealAttempt(
                            attempt_number=attempt_num,
                            failure_type=FailureType.UNKNOWN,
                            fix_applied=f"PatchCache hit: applied {_applied}",
                            changed_files=_applied,
                            note="Applied from persistent patch cache — no LLM call",
                        ))
                        continue

            # ── Pre-process each failure: blocking / auto-fix / collect LLM targets
            file_errors: dict[str, list] = {}
            blocked: Optional[str] = None

            for failure in last_failures:
                # Blocking: timed-out process
                if failure.exit_code == -1 and "timeout" in (failure.stderr or "").lower():
                    blocked = (
                        f"Command timed out: {failure.command!r}. "
                        "Cannot heal a hanging process via code changes."
                    )
                    break

                # Deterministic: command-specific auto-fixes (no LLM)
                auto_fix = await self._apply_known_auto_fixes(failure, attempt_num)
                if auto_fix:
                    attempts.append(auto_fix)
                    continue

                # Blocking: missing executable
                missing_tool = _missing_tool_from_output(
                    failure.command, failure.stdout, failure.stderr
                )
                if missing_tool:
                    blocked = (
                        f"Missing tool '{missing_tool}' required by command "
                        f"{failure.command!r}. Install it in the environment."
                    )
                    break

                # ── Command gating: run ruff --fix before LLM when pytest fails ─
                # This strips lint noise from the failure set cheaply. Runs once
                # (only when pytest is the failing command and ruff is available).
                ruff_fix = await self._try_ruff_auto_fix(failure, attempt_num)
                if ruff_fix:
                    attempts.append(ruff_fix)
                    # Don't `continue` — still extract broken files in case
                    # ruff fixed lint but the test still fails for other reasons.

                # Deterministic: async fixture rewrite (zero LLM)
                async_fix = await self._fix_async_fixtures_deterministically(
                    failure, attempt_num
                )
                if async_fix:
                    attempts.append(async_fix)
                    continue  # re-run tests after fixture fix

                # Deterministic: missing conftest.py (no LLM)
                det = self._fix_pytest_import_path_if_needed(failure, attempt_num)
                if det:
                    attempts.append(det)
                    continue

                # LLM fixes: collect broken files.
                # Prefer structured pytest data (exact source files from traceback)
                # over regex-scanning raw text — more accurate attribution.
                if _pytest_report and _pytest_report.broken_source_files:
                    for fp in list(_pytest_report.broken_source_files.keys())[:_MAX_BROKEN_FILES_PER_ROUND]:
                        if self._is_fixable_file(fp, allow_test_files=_effective_allow_test_edits):
                            if fp not in file_errors:
                                file_errors[fp] = []
                            file_errors[fp].append(failure)
                    # Fall back to test files if no source files identified
                    if not file_errors:
                        broken = self._extract_all_broken_files(
                            failure, allow_test_files=_effective_allow_test_edits
                        )
                        for fp in broken:
                            file_errors.setdefault(fp, []).append(failure)
                else:
                    broken = self._extract_all_broken_files(
                        failure, allow_test_files=_effective_allow_test_edits
                    )
                    for fp in broken:
                        file_errors.setdefault(fp, []).append(failure)

            if blocked and not file_errors:
                return HealingReport(
                    success=False,
                    attempts=attempts,
                    final_command_result=last_failures[0],
                    blocked_reason=blocked,
                )

            if not file_errors:
                if attempts:
                    continue
                break  # nothing fixable

            # ── No-progress guard: same broken file set as a previous round ────
            _fset_hash = _file_set_hash(frozenset(file_errors.keys()))
            if _fset_hash in _seen_file_set_hashes:
                print(
                    f"  [Healer] Attempt {attempt_num}: same broken file set repeats "
                    "— no progress, stopping early."
                )
                break
            _seen_file_set_hashes.add(_fset_hash)

            # ── Record pre-fix content hashes for diff check ─────────────────
            _pre_hash = _content_hash(self.workspace, file_errors.keys())

            # ── Fix broken files (capped fan-out, bounded concurrency) ────────
            batch_attempts = await self._fix_files_parallel(
                file_errors, attempt_num, _effective_allow_test_edits,
                pytest_report=_pytest_report,
            )
            attempts.extend(batch_attempts)

            # ── No-progress guard: patch produced no diff ─────────────────────
            _post_hash = _content_hash(self.workspace, file_errors.keys())
            if _pre_hash == _post_hash and batch_attempts:
                print(
                    f"  [Healer] Attempt {attempt_num}: LLM applied no file changes "
                    "— stopping early."
                )
                break

            # ── PatchCache: store patches for next run ────────────────────────
            if self._patch_cache and file_errors:
                patch_to_store: dict[str, str] = {}
                for fp in file_errors:
                    full = Path(self.workspace) / fp
                    if full.exists():
                        patch_to_store[fp] = full.read_text(encoding="utf-8")
                if patch_to_store:
                    _store_key = _cache_key or _patch_cache_key(
                        _fhash, list(file_errors.keys()), self.workspace, self._model_id
                    )
                    self._patch_cache.put(_store_key, patch_to_store)

            # ── PatternStore: record successful cross-project patterns ─────────
            if batch_attempts:
                for failure in last_failures:
                    failure_type_str = classify_failure(
                        failure.command, failure.stdout, failure.stderr
                    ).value
                    fp_key = self._pattern_store.fingerprint(
                        failure_type_str, f"{failure.stdout}\n{failure.stderr}"
                    )
                    changed = [fp for a in batch_attempts for fp in a.changed_files]
                    fix_desc = f"Modified {changed[:3]} to fix {failure_type_str}"
                    try:
                        self._pattern_store.record(fp_key, fix_desc)
                    except Exception:
                        pass  # pattern persistence is best-effort

        return HealingReport(
            success=False,
            attempts=attempts,
            final_command_result=last_failures[0] if last_failures else None,
            cache_hits=_cache_hits,
        )

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

            content = full_path.read_text(encoding="utf-8")
            issue_lines = "\n".join(f"- {issue}" for issue in issues)
            related_files = self._build_related_files_context(target_file)
            prompt = STATIC_ISSUE_HEALER_USER_PROMPT_TEMPLATE.format(
                issues=issue_lines,
                file_path=target_file,
                file_content=f"Current content:\n{_cap_file_content(content)}",
                related_files=related_files,
            )
            prompt = prune_prompt(prompt, max_chars=20_000)
            fixed_content = await self.llm_client.generate(prompt, system_prompt=HEALER_SYSTEM_PROMPT)
            fixed_content = self._normalize_content(fixed_content)

            if not _healer_output_ok(fixed_content, content):
                logger.warning("[Healer] Static fix rejected for %s — truncated/too short", target_file)
                return None

            full_path.write_text(fixed_content, encoding="utf-8")
            return HealAttempt(
                attempt_number=attempt_number,
                failure_type=FailureType.BUILD_ERROR,
                fix_applied=f"Static fix for {target_file}",
                changed_files=[target_file],
            )

        import os as _os
        _per_file_timeout = int(_os.environ.get("CODEGEN_LLM_TIMEOUT", "120")) + 30

        async def _fix_with_timeout(fp: str, issues: list) -> Optional[HealAttempt]:
            try:
                result = await asyncio.wait_for(
                    _fix_static_issue(fp, issues),
                    timeout=_per_file_timeout,
                )
                status = "fixed" if result else "no change"
                print(f"  [LiveGuard] {fp}: {status}")
                return result
            except asyncio.TimeoutError:
                logger.warning("[LiveGuard] Micro-heal timed out after %ds: %s", _per_file_timeout, fp)
                print(f"  [LiveGuard] {fp}: timed out ({_per_file_timeout}s) — skipped")
                return None
            except Exception as exc:
                logger.warning("[LiveGuard] Micro-heal error for %s: %s", fp, exc)
                return None

        # Cap concurrency to 3 so slow CLI providers don't all block at once
        _sem = asyncio.Semaphore(3)

        async def _guarded(fp: str, issues: list) -> Optional[HealAttempt]:
            async with _sem:
                return await _fix_with_timeout(fp, issues)

        tasks = [_guarded(fp, issues) for fp, issues in issues_by_file.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, HealAttempt)]

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _fix_files_parallel(
        self,
        file_errors: dict[str, list],
        attempt_num: int,
        allow_test_files: bool = False,
        pytest_report: Optional[PytestReport] = None,
    ) -> List[HealAttempt]:
        """Fix broken files. Uses combined multi-file fix when >1 file is broken
        (single LLM call sees all broken files together — better for cross-file errors).
        Falls back to per-file parallel fixes if multi-file call fails or returns bad JSON.
        """
        fixable = {
            fp: errs for fp, errs in file_errors.items()
            if self._is_fixable_file(fp, allow_test_files=allow_test_files)
               or (allow_test_files and _is_test_file(fp))
        }
        if not fixable:
            return []

        if len(fixable) > 1:
            combined = await self._fix_files_combined(
                fixable, attempt_num, pytest_report=pytest_report
            )
            if combined:
                return combined

        # Single file or combined fix failed — parallel per-file fixes
        sem = asyncio.Semaphore(_LLM_FIX_CONCURRENCY)

        async def _guarded(fp: str, errs: list) -> Optional[HealAttempt]:
            async with sem:
                return await self._fix_file_for_errors(
                    fp, errs, attempt_num,
                    allow_test_files=allow_test_files,
                    pytest_report=pytest_report,
                )

        tasks = [_guarded(fp, errs) for fp, errs in fixable.items()]
        batch = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in batch if isinstance(r, HealAttempt)]

    async def _fix_files_combined(
        self,
        file_errors: dict[str, list],
        attempt_num: int,
        pytest_report: Optional[PytestReport] = None,
    ) -> List[HealAttempt]:
        """Fix multiple broken files in a single LLM call.

        Sends all broken file contents + error context together so the LLM can see
        cross-file import relationships and fix them consistently.
        Returns an empty list if the response is not valid JSON (caller falls back).
        """
        # Collect all failures for command/type
        all_failures = [f for errs in file_errors.values() for f in errs]
        failure_type = classify_failure(
            all_failures[0].command, all_failures[0].stdout, all_failures[0].stderr
        )
        combined_errors = _truncate_error_output(
            "\n\n".join(
                f"$ {f.command}\n{f.stdout}\n{f.stderr}" for f in all_failures[:3]
            )
        )

        # Build files_to_fix section
        files_section_parts = []
        for fp, errs in file_errors.items():
            full_path = Path(self.workspace) / fp
            if not full_path.exists():
                continue
            content = full_path.read_text(encoding="utf-8")
            files_section_parts.append(
                f"--- {fp} ---\n{_cap_file_content(content)}"
            )
        if not files_section_parts:
            return []
        files_section = "\n\n".join(files_section_parts)

        # Build related files from the first broken file's directory context
        first_fp = next(iter(file_errors))
        related_files = self._build_related_files_context(first_fp, max_files=8)

        # Structured pytest context (exact test names, assertions, source traces)
        structured_ctx = ""
        if pytest_report:
            structured_ctx = format_structured_failures_for_prompt(pytest_report)

        # Pattern store hints
        fingerprints = [
            self._pattern_store.fingerprint(failure_type.value, f"{f.stdout}\n{f.stderr}")
            for f in all_failures[:3]
        ]
        pattern_hints = self._pattern_store.known_patterns_prompt(fingerprints)

        error_block = (structured_ctx or combined_errors) + pattern_hints

        prompt = HEALER_MULTI_FILE_PROMPT_TEMPLATE.format(
            command=all_failures[0].command,
            failure_type=failure_type.value,
            error_output=error_block,
            files_to_fix=files_section,
            related_files=related_files,
        )
        prompt = prune_prompt(prompt, max_chars=24_000)

        response = await self.llm_client.generate(prompt, system_prompt=HEALER_SYSTEM_PROMPT)

        # Parse JSON response
        data = find_json_in_text(response)
        if not data or not isinstance(data, dict):
            return []  # caller falls back to per-file

        attempts = []
        for fp, fixed_content in data.items():
            if not isinstance(fixed_content, str) or not fixed_content.strip():
                continue
            full_path = resolve_workspace_path(self.workspace, fp)
            if full_path is None:
                logger.warning("[Healer] Multi-fix: skipping path outside workspace: %s", fp)
                continue
            if not full_path.exists():
                continue
            original_content = full_path.read_text(encoding="utf-8", errors="replace")
            fixed_content = self._normalize_content(fixed_content)
            if not _healer_output_ok(fixed_content, original_content):
                logger.warning(
                    "[Healer] Multi-fix: rejecting fix for %s — truncated or too short (%d→%d lines)",
                    fp, original_content.count('\n'), fixed_content.count('\n') if fixed_content else 0,
                )
                continue
            full_path.write_text(fixed_content, encoding="utf-8")
            print(f"  [Healer] Multi-fix: wrote {fp} (attempt {attempt_num})")
            attempts.append(HealAttempt(
                attempt_number=attempt_num,
                failure_type=failure_type,
                fix_applied=f"Multi-file fix: {fp}",
                changed_files=[fp],
                note="Combined multi-file LLM fix",
            ))
        return attempts

    def _extract_broken_files_raw(self, failures: list) -> list[str]:
        """Extract candidate broken file paths without workspace-existence check.
        Used for cache-key computation before editing.
        """
        seen: list[str] = []
        output = " ".join(f"{r.stdout}\n{r.stderr}" for r in failures)
        for match in _FILE_QUOTED_RE.finditer(output):
            fp = match.group(1).strip(".,:;)]}")
            if fp not in seen:
                seen.append(fp)
        if not seen:
            for match in _FILE_GENERAL_RE.finditer(output):
                fp = match.group(1).strip(".,:;)]}")
                if fp not in seen:
                    seen.append(fp)
        return seen[:_MAX_BROKEN_FILES_PER_ROUND]

    def _extract_all_broken_files(
        self, result, allow_test_files: bool = False
    ) -> List[str]:
        """Return fixable source files referenced in error output (max 4).

        Capped at _MAX_BROKEN_FILES_PER_ROUND to prevent slow/messy fan-out.
        """
        output = f"{result.stdout}\n{result.stderr}"
        seen: List[str] = []

        for match in _FILE_QUOTED_RE.finditer(output):
            fp = match.group(1).strip(".,:;)]}")
            if self._is_fixable_file(fp, allow_test_files=allow_test_files) and fp not in seen:
                seen.append(fp)

        if not seen:
            for match in _FILE_GENERAL_RE.finditer(output):
                fp = match.group(1).strip(".,:;)]}")
                if self._is_fixable_file(fp, allow_test_files=allow_test_files) and fp not in seen:
                    seen.append(fp)

        return seen[:_MAX_BROKEN_FILES_PER_ROUND]

    def _is_fixable_file(self, fp: str, allow_test_files: bool = False) -> bool:
        """True if fp is a real fixable file inside the workspace."""
        if _is_ignored_runtime_path(fp):
            return False
        if not allow_test_files and not self.allow_test_file_edits and _is_test_file(fp):
            return False
        candidate = resolve_workspace_path(self.workspace, fp)
        return candidate is not None and candidate.exists()

    def _build_related_files_context(self, target_file_path: str, max_files: int = 6) -> str:
        """Build a compact summary of what other project files export.

        Reads sibling source files and extracts their top-level definitions so the
        LLM healer knows exactly what is importable — prevents it from guessing and
        re-introducing the same ImportError / NameError it is trying to fix.
        """
        workspace = Path(self.workspace)
        target_dir = Path(target_file_path).parent
        ext = Path(target_file_path).suffix.lower()

        # Collect candidate related files: same directory first, then workspace root
        candidates: list[Path] = []
        for p in sorted((workspace / target_dir).glob(f"*{ext}")):
            rel = str(p.relative_to(workspace))
            if rel != target_file_path and not _is_ignored_runtime_path(rel):
                candidates.append(p)
        for p in sorted(workspace.glob(f"*{ext}")):
            rel = str(p.relative_to(workspace))
            if rel != target_file_path and p not in candidates and not _is_ignored_runtime_path(rel):
                candidates.append(p)

        lines: list[str] = []
        for p in candidates[:max_files]:
            rel = str(p.relative_to(workspace))
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Extract only top-level definitions (class / def / CONSTANT = ...)
            exports: list[str] = []
            for line in text.splitlines()[:120]:
                s = line.strip()
                if s.startswith(("class ", "def ", "async def ")):
                    name = s.split("(")[0].split(":")[0].split()[1]
                    exports.append(name)
                elif re.match(r"^[A-Z_][A-Z0-9_]+ *=", s):
                    exports.append(s.split("=")[0].strip())
            if exports:
                lines.append(f"  {rel}: exports [{', '.join(exports[:20])}]")
            else:
                lines.append(f"  {rel}")
        return "\n".join(lines) if lines else "  (no related files found)"

    async def _fix_file_for_errors(
        self,
        file_path: str,
        failures: list,
        attempt_num: int,
        allow_test_files: bool = False,
        pytest_report: Optional[PytestReport] = None,
    ) -> Optional[HealAttempt]:
        """LLM-fix a single source file using all error outputs that reference it."""
        full_path, path_err = self._resolve_target_path(file_path)
        if path_err:
            return None
        if _is_test_file(file_path) and not allow_test_files and not self.allow_test_file_edits:
            return None

        content = full_path.read_text(encoding="utf-8")
        failure_type = classify_failure(
            failures[0].command, failures[0].stdout, failures[0].stderr
        )

        if len(failures) == 1:
            combined_errors = _truncate_error_output(
                f"{failures[0].stdout}\n{failures[0].stderr}"
            )
        else:
            parts = [
                f"$ {f.command}\n{_truncate_error_output(f'{f.stdout}\n{f.stderr}', max_lines=40)}"
                for f in failures
            ]
            combined_errors = "\n\n---\n\n".join(parts)

        related_files = self._build_related_files_context(file_path)

        # Prefer structured pytest context when available for this specific file
        structured_ctx = ""
        if pytest_report:
            relevant = [
                tf for tf in pytest_report.failures
                if file_path in tf.source_files or file_path == tf.test_file
            ]
            if relevant:
                from .pytest_parser import PytestReport as _PR, format_structured_failures_for_prompt
                _mini = _PR(failures=relevant, failed=len(relevant))
                structured_ctx = format_structured_failures_for_prompt(_mini)

        # Pattern store hints
        fingerprints = [
            self._pattern_store.fingerprint(failure_type.value, f"{f.stdout}\n{f.stderr}")
            for f in failures[:2]
        ]
        pattern_hints = self._pattern_store.known_patterns_prompt(fingerprints)

        error_block = (structured_ctx or combined_errors) + pattern_hints

        prompt = HEALER_USER_PROMPT_TEMPLATE.format(
            command=failures[0].command,
            failure_type=failure_type.value,
            error_output=error_block,
            file_path=file_path,
            file_content=f"Current content:\n{_cap_file_content(content)}",
            related_files=related_files,
        )
        prompt = prune_prompt(prompt, max_chars=20_000)

        fixed = await self.llm_client.generate(prompt, system_prompt=HEALER_SYSTEM_PROMPT)
        fixed = self._normalize_content(fixed)

        if not _healer_output_ok(fixed, content):
            logger.warning(
                "[Healer] Rejecting fix for %s — output is truncated or >40%% shorter than original "
                "(%d lines → %d lines). File unchanged.",
                file_path, content.count('\n'), fixed.count('\n') if fixed else 0,
            )
            return None

        full_path.write_text(fixed, encoding="utf-8")
        print(f"  [Healer] Fixed {file_path} (attempt {attempt_num})")
        return HealAttempt(
            attempt_number=attempt_num,
            failure_type=failure_type,
            fix_applied=f"Fixed {file_path}",
            changed_files=[file_path],
        )

    async def _fix_async_fixtures_deterministically(
        self, failure, attempt_num: int
    ) -> Optional[HealAttempt]:
        """Deterministically rewrite @pytest.fixture async def → @pytest_asyncio.fixture.

        Also writes asyncio_mode=auto to pytest config.
        Runs zero LLM calls. Returns HealAttempt if any file was changed.
        """
        combined = f"{failure.stdout}\n{failure.stderr}"
        if not _TEST_INFRA_RE.search(combined):
            return None
        if "async" not in combined and "asyncio" not in combined:
            return None

        changed: list[str] = []

        # Fix all test files in the workspace
        for test_path in Path(self.workspace).rglob("test_*.py"):
            rel = str(test_path.relative_to(self.workspace))
            if _is_ignored_runtime_path(rel):
                continue
            try:
                content = test_path.read_text(encoding="utf-8")
                fixed = _fix_async_pytest_fixtures(content)
                if fixed:
                    test_path.write_text(fixed, encoding="utf-8")
                    changed.append(rel)
            except Exception:
                continue

        # Ensure asyncio_mode=auto in pytest config
        if _ensure_asyncio_mode_auto(self.workspace):
            changed.append("pytest.ini/pyproject.toml")

        if not changed:
            return None

        print(
            f"  [Healer] Async fixture deterministic fix: "
            f"{[c for c in changed if not c.startswith('pytest')]} "
            f"+ asyncio_mode=auto"
        )
        return HealAttempt(
            attempt_number=attempt_num,
            failure_type=FailureType.BUILD_ERROR,
            fix_applied=f"Async fixture rewrite + asyncio_mode=auto ({len(changed)} file(s))",
            changed_files=changed,
            note="Deterministic: @pytest.fixture async def → @pytest_asyncio.fixture",
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
            fix_applied=f"Added conftest.py to fix pytest import path for '{module_name}'",
            changed_files=["conftest.py"],
        )

    async def _try_ruff_auto_fix(self, failure, attempt_num: int) -> Optional[HealAttempt]:
        """Run ruff --fix when pytest fails to remove lint noise before LLM.

        Only fires when pytest is the failing command and ruff is installed.
        Runs at most once per heal round.
        """
        command = (failure.command or "").strip()
        # Only gate on pytest failures; ruff failures are handled separately
        if "pytest" not in command and "python -m pytest" not in command:
            return None
        # Only run ruff if it's not explicitly disabled
        if os.environ.get("CODEGEN_SKIP_RUFF_GATE", "0").strip() == "1":
            return None

        fix_result = await asyncio.to_thread(
            run_shell_command, "ruff check --fix .", cwd=self.workspace
        )
        if fix_result.exit_code not in (0, 1):
            return None  # ruff not installed or fatal error — skip silently

        return HealAttempt(
            attempt_number=attempt_num,
            failure_type=FailureType.LINT_TYPE_FAILURE,
            fix_applied="ruff --fix pre-pass before LLM heal",
            changed_files=[],
            note=_truncate_error_output(
                f"{fix_result.stdout}\n{fix_result.stderr}", max_lines=10
            ) or None,
        )

    async def _apply_known_auto_fixes(self, last_result, attempt_number: int) -> Optional[HealAttempt]:
        command = (last_result.command or "").strip()
        if re.match(r"^ruff\s+check(\s|$)", command) and "--fix" not in command:
            fix_command = re.sub(r"^ruff\s+check", "ruff check --fix", command, count=1)
            fix_result = await asyncio.to_thread(run_shell_command, fix_command, cwd=self.workspace)
            if fix_result.exit_code in (0, 1):
                note = None
                if fix_result.exit_code != 0:
                    note = _truncate_error_output(
                        f"{fix_result.stdout}\n{fix_result.stderr}", max_lines=20
                    )
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

    def _normalize_content(self, content: str) -> str:
        code_blocks = extract_code_from_markdown(content)
        if code_blocks:
            return code_blocks[0].strip()
        return content.strip()
