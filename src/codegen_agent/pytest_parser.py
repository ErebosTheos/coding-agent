"""Structured pytest failure parser.

Runs pytest with --json-report and returns a clean data structure so the
healer knows *exactly* which source files caused failures, which assertion
failed, and what the full traceback looks like — without regex-mining raw text.

Falls back gracefully to empty results if pytest-json-report is not installed,
json-report is missing, or the command doesn't use pytest.
"""
from __future__ import annotations

import json
import os
import re
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .utils import run_shell_command

_REPORT_FILENAME = ".test_report.json"


@dataclass
class TestFailure:
    """Structured information about a single failing test."""
    __test__ = False  # prevent pytest from collecting this as a test class
    test_id: str                          # e.g. "tests/test_auth.py::test_login"
    test_file: str                        # e.g. "tests/test_auth.py"
    outcome: str                          # "failed" | "error"
    short_repr: str                       # one-line failure summary
    long_repr: str                        # full failure text
    source_files: list[str] = field(default_factory=list)  # non-test files in traceback


@dataclass
class PytestReport:
    """Parsed output of a pytest --json-report run."""
    passed: int = 0
    failed: int = 0
    errors: int = 0
    failures: list[TestFailure] = field(default_factory=list)
    # Map: source_file → [TestFailure] that reference it
    broken_source_files: dict[str, list[TestFailure]] = field(default_factory=dict)
    raw_stdout: str = ""
    raw_stderr: str = ""
    exit_code: int = 0


def _is_pytest_command(cmd: str) -> bool:
    return bool(re.search(r"\bpytest\b|python\s+-m\s+pytest", cmd))


def _inject_json_report(cmd: str, report_path: str) -> str:
    """Add --json-report flags to a pytest command if not already present."""
    if "--json-report" in cmd:
        return cmd
    flags = f"--json-report --json-report-file={report_path} --tb=short"
    # Insert after 'pytest' or 'python -m pytest'
    cmd = re.sub(r"(\bpython\s+-m\s+pytest|\bpytest)", rf"\1 {flags}", cmd, count=1)
    return cmd


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


def _parse_report_json(data: dict, workspace: str) -> PytestReport:
    """Parse a pytest-json-report JSON dict into a PytestReport."""
    summary = data.get("summary", {})
    report = PytestReport(
        passed=summary.get("passed", 0),
        failed=summary.get("failed", 0),
        errors=summary.get("errors", 0),
    )

    workspace_abs = str(Path(workspace).resolve())

    for test in data.get("tests", []):
        outcome = test.get("outcome", "")
        if outcome not in ("failed", "error"):
            continue

        node_id: str = test.get("nodeid", "")
        test_file = node_id.split("::")[0] if "::" in node_id else node_id

        # Gather all source file paths from the traceback
        source_files: list[str] = []
        call = test.get("call") or test.get("setup") or {}
        traceback = call.get("traceback", [])
        for frame in traceback:
            raw_path: str = frame.get("path", "")
            if not raw_path:
                continue
            # Normalise to relative workspace path
            try:
                p = Path(raw_path)
                if p.is_absolute():
                    rel = str(p.relative_to(workspace_abs))
                else:
                    rel = raw_path
            except ValueError:
                rel = raw_path
            rel = rel.replace("\\", "/")
            if rel and not _is_test_file(rel) and rel not in source_files:
                source_files.append(rel)

        long_repr: str = ""
        if call:
            crash = call.get("crash", {})
            long_repr = crash.get("message", "") or call.get("longrepr", "") or ""
        if not long_repr:
            long_repr = test.get("longrepr", "")

        short_repr = (long_repr or "").splitlines()[0][:200] if long_repr else outcome

        failure = TestFailure(
            test_id=node_id,
            test_file=test_file,
            outcome=outcome,
            short_repr=short_repr,
            long_repr=long_repr,
            source_files=source_files,
        )
        report.failures.append(failure)

        # Build broken_source_files index
        for sf in source_files:
            report.broken_source_files.setdefault(sf, []).append(failure)

    return report


async def run_pytest_structured(
    command: str,
    workspace: str,
) -> Optional[PytestReport]:
    """Run a pytest command with --json-report and return a PytestReport.

    Returns None if:
    - command is not a pytest command
    - pytest-json-report is not installed
    - the report file cannot be parsed
    """
    if not _is_pytest_command(command):
        return None

    report_dir = Path(workspace) / ".codegen_agent"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / _REPORT_FILENAME
    report_path = str(report_file)

    # Delete stale report before running so that if pytest crashes without
    # emitting a new one we won't silently consume old data.
    report_file.unlink(missing_ok=True)

    instrumented = _inject_json_report(command, report_path)

    result = await asyncio.to_thread(run_shell_command, instrumented, cwd=workspace)

    if not report_file.exists():
        return None  # pytest-json-report probably not installed

    try:
        data = json.loads(report_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    pr = _parse_report_json(data, workspace)
    pr.raw_stdout = result.stdout
    pr.raw_stderr = result.stderr
    pr.exit_code = result.exit_code
    return pr


def format_structured_failures_for_prompt(report: PytestReport, max_failures: int = 5) -> str:
    """Render a PytestReport into a compact, healer-readable string."""
    if not report.failures:
        return ""

    lines: list[str] = [f"Structured test failures ({report.failed} failed, {report.errors} errors):"]
    for tf in report.failures[:max_failures]:
        lines.append(f"\n  [{tf.outcome.upper()}] {tf.test_id}")
        if tf.short_repr:
            lines.append(f"  Failure: {tf.short_repr}")
        if tf.source_files:
            lines.append(f"  Source files in traceback: {tf.source_files}")
        if tf.long_repr and len(tf.long_repr) > len(tf.short_repr) + 10:
            # Include up to 8 lines of detail
            detail = "\n".join(tf.long_repr.splitlines()[:8])
            lines.append(f"  Detail:\n{detail}")

    if len(report.failures) > max_failures:
        lines.append(f"\n  ... and {len(report.failures) - max_failures} more failures")

    return "\n".join(lines)
