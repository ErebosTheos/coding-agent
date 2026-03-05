"""Tests for HealerLoopGuard — stuck-loop detection in healer.heal()."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codegen_agent.healer import Healer, _failure_hash
from codegen_agent.models import CommandResult, HealingReport


def _result(cmd: str, exit_code: int, stderr: str = "") -> CommandResult:
    return CommandResult(command=cmd, exit_code=exit_code, stdout="", stderr=stderr)


# ── _failure_hash ─────────────────────────────────────────────────────────────

def test_failure_hash_same_for_identical_output():
    r1 = [_result("pytest", 1, "FAILED test_foo")]
    r2 = [_result("pytest", 1, "FAILED test_foo")]
    assert _failure_hash(r1) == _failure_hash(r2)


def test_failure_hash_different_for_different_output():
    r1 = [_result("pytest", 1, "FAILED test_foo")]
    r2 = [_result("pytest", 1, "FAILED test_bar")]
    assert _failure_hash(r1) != _failure_hash(r2)


def test_failure_hash_different_for_different_exit_code():
    r1 = [_result("pytest", 1, "error")]
    r2 = [_result("pytest", 2, "error")]
    assert _failure_hash(r1) != _failure_hash(r2)


def test_failure_hash_order_independent():
    """Hash must be stable regardless of failure list order."""
    r1 = [_result("cmd_a", 1, "err_a"), _result("cmd_b", 1, "err_b")]
    r2 = [_result("cmd_b", 1, "err_b"), _result("cmd_a", 1, "err_a")]
    assert _failure_hash(r1) == _failure_hash(r2)


# ── HealerLoopGuard in heal() ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_heal_stops_on_repeated_failure_hash(tmp_path):
    """heal() must exit early when the same failure repeats — not exhaust budget."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value="# unchanged")
    healer = Healer(llm_client=mock_llm, workspace=str(tmp_path), max_attempts=5)

    # Write a source file so the healer can attempt a fix
    (tmp_path / "src.py").write_text("def broken(): pass\n")

    identical_failure = CommandResult(
        command="pytest", exit_code=1,
        stdout="", stderr='File "src.py", line 1\nFAILED test_foo',
    )

    call_count = 0

    def always_fail(cmd, cwd):
        nonlocal call_count
        call_count += 1
        return identical_failure

    with patch("codegen_agent.healer.run_shell_command", side_effect=always_fail):
        report = await healer.heal(["pytest"])

    assert not report.success
    # With max_attempts=5 and identical output, should stop after attempt 2
    # (attempt 1 adds hash, attempt 2 detects repeat → break)
    assert call_count <= 3, f"Expected early exit, got {call_count} test runs"


@pytest.mark.asyncio
async def test_heal_continues_when_failure_changes(tmp_path):
    """heal() must keep going when error output changes between attempts."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value="# fixed\ndef foo(): return 1\n")
    healer = Healer(llm_client=mock_llm, workspace=str(tmp_path), max_attempts=3)

    (tmp_path / "src.py").write_text("def broken(): pass\n")

    failures = [
        CommandResult("pytest", 1, "", 'File "src.py"\nFAILED test_a'),
        CommandResult("pytest", 1, "", 'File "src.py"\nFAILED test_b'),  # different
        CommandResult("pytest", 0, "3 passed", ""),
    ]
    idx = 0

    def rotating_results(cmd, cwd):
        nonlocal idx
        r = failures[min(idx, len(failures) - 1)]
        idx += 1
        return r

    with patch("codegen_agent.healer.run_shell_command", side_effect=rotating_results):
        report = await healer.heal(["pytest"])

    assert report.success
