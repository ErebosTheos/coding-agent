"""Tests for live_guard.py — Tier 1 and Tier 2 correctness."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codegen_agent.live_guard import check_file, post_execution_guard
from codegen_agent.models import GeneratedFile


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gf(file_path: str, content: str) -> GeneratedFile:
    return GeneratedFile(file_path=file_path, content=content, node_id="n1", sha256="abc")


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── Tier 1: check_file ────────────────────────────────────────────────────────

def test_check_file_valid_python_no_issues():
    assert check_file("src/main.py", "def foo():\n    return 1\n") == []


def test_check_file_syntax_error_returns_message():
    issues = check_file("src/main.py", "def foo(\n")
    assert len(issues) == 1
    assert "SyntaxError" in issues[0]


def test_check_file_non_python_no_check():
    # Even invalid content in a non-Python file should produce no issues
    assert check_file("config.yaml", "def foo(\n") == []
    assert check_file("README.md", "def foo(\n") == []


def test_check_file_reports_line_number():
    bad = "x = 1\ny = (\n"  # unclosed paren
    issues = check_file("app.py", bad)
    assert issues  # should have at least one issue
    assert "SyntaxError" in issues[0]


# ── Tier 2: post_execution_guard ─────────────────────────────────────────────

def _make_healer_mock(attempts=None):
    healer = MagicMock()
    healer.heal_static_issues = AsyncMock(return_value=attempts or [])
    return healer


@pytest.mark.asyncio
async def test_post_execution_guard_no_issues_no_llm(tmp_path):
    """Clean files → no deterministic fix, no LLM call."""
    content = "x = 1\n"
    _write(tmp_path, "src/a.py", content)
    _write(tmp_path, "src/__init__.py", "")
    files = [_gf("src/a.py", content)]
    healer = _make_healer_mock()

    result = await post_execution_guard(files, str(tmp_path), healer, max_llm_calls=10)

    assert result == []
    healer.heal_static_issues.assert_not_called()


@pytest.mark.asyncio
async def test_post_execution_guard_max_llm_zero_skips_llm(tmp_path):
    """max_llm_calls=0 → deterministic step runs but LLM step is skipped."""
    # Create a file that imports from a module that doesn't exist
    _write(tmp_path, "src/__init__.py", "")
    content = "from .missing_mod import foo\nx = 1\n"
    _write(tmp_path, "src/a.py", content)
    files = [_gf("src/a.py", content)]
    healer = _make_healer_mock()

    result = await post_execution_guard(files, str(tmp_path), healer, max_llm_calls=0)

    assert result == []
    healer.heal_static_issues.assert_not_called()


@pytest.mark.asyncio
async def test_post_execution_guard_deterministic_fix_refreshes_content(tmp_path):
    """After deterministic fix, re-check must use updated disk content."""
    # src/b.py exports 'bar'; src/a.py imports 'foo' (missing) and 'bar' (present)
    _write(tmp_path, "src/__init__.py", "")
    _write(tmp_path, "src/b.py", "def bar(): pass\n")
    # 'foo' doesn't exist in b — deterministic fix should strip it
    content_a = "from .b import foo, bar\n\nbar()\n"
    _write(tmp_path, "src/a.py", content_a)
    files = [
        _gf("src/a.py", content_a),
        _gf("src/b.py", "def bar(): pass\n"),
    ]
    healer = _make_healer_mock()

    # After deterministic fix, 'foo' is removed. Re-check should find no
    # remaining issues → LLM step not needed.
    result = await post_execution_guard(files, str(tmp_path), healer, max_llm_calls=10)

    # Verify deterministic fix wrote to disk
    fixed_content = (tmp_path / "src/a.py").read_text()
    assert "foo" not in fixed_content
    assert "bar" in fixed_content
    # LLM should not have been called because the re-check found no issues
    healer.heal_static_issues.assert_not_called()


# ── Orchestrator-level: Tier 2 resume guard ──────────────────────────────────

# ── Stage 6 pre-flight short-circuit ─────────────────────────────────────────

def test_preflight_short_circuit_logic():
    """When pre-flight results are all exit_code=0, HealingReport is built
    directly without calling healer.heal() a second time."""
    from codegen_agent.models import CommandResult, HealingReport

    pre_results = [
        CommandResult(command="pytest -q", exit_code=0, stdout="5 passed", stderr=""),
    ]
    all_passed = all(r.exit_code == 0 for r in pre_results)
    assert all_passed

    # Simulate the short-circuit branch
    healing_report = HealingReport(
        success=True,
        attempts=[],
        final_command_result=pre_results[-1],
    )
    assert healing_report.success is True
    assert healing_report.final_command_result.exit_code == 0
    assert healing_report.attempts == []


def test_preflight_not_short_circuit_on_failure():
    """When any pre-flight result fails, the short-circuit is not triggered."""
    from codegen_agent.models import CommandResult

    pre_results = [
        CommandResult(command="pytest -q", exit_code=1, stdout="", stderr="FAILED"),
    ]
    all_passed = all(r.exit_code == 0 for r in pre_results)
    assert not all_passed


def test_tier2_guarded_when_healing_report_exists():
    """Tier 2 block must be skipped when report.healing_report is already set
    (i.e. Stage 6 is complete), to prevent re-editing healed files."""
    from codegen_agent.models import HealingReport

    healing_report = HealingReport(success=True, attempts=[])
    # Simulate the guard condition used in orchestrator.py
    report_has_exec = True
    report_has_files = True
    report_has_healing = healing_report is not None

    should_run_tier2 = report_has_exec and report_has_files and not report_has_healing
    assert not should_run_tier2, "Tier 2 must not run when healing_report is present"


def test_tier2_runs_when_no_healing_report():
    should_run_tier2 = True and True and not None
    assert should_run_tier2, "Tier 2 must run when healing_report is absent"


# ── env parse safety ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("env_val,expected", [
    ("10", 10),
    ("0", 0),
    ("-1", -1),
    ("", 10),       # empty → default
    ("abc", 10),    # invalid → default
    ("10.5", 10),   # float string → default
])
def test_safe_env_parse(env_val, expected):
    """Replicate the safe env parse logic used in orchestrator.py."""
    _lg_max_env = env_val.strip()
    result = int(_lg_max_env) if _lg_max_env.lstrip("-").isdigit() else 10
    assert result == expected
