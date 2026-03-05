import asyncio
import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from codegen_agent.healer import Healer, _consolidate_commands, _cap_file_content
from codegen_agent.models import CommandResult


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield tmp_dir


@pytest.fixture
def healer(temp_workspace):
    mock_llm = MagicMock()
    return Healer(llm_client=mock_llm, workspace=temp_workspace)


# ── _consolidate_commands ─────────────────────────────────────────────────────

def test_consolidate_pytest_commands_no_x_flag():
    """Merged pytest command must NOT have -x so all failures are visible."""
    result = _consolidate_commands(["pytest tests/test_a.py", "pytest tests/test_b.py"])
    assert len(result) == 1
    assert "-x" not in result[0]
    assert "pytest" in result[0]


def test_consolidate_mixed_commands_unchanged():
    cmds = ["pytest tests/", "go test ./..."]
    assert _consolidate_commands(cmds) == cmds


def test_consolidate_empty_unchanged():
    assert _consolidate_commands([]) == []


# ── _cap_file_content ─────────────────────────────────────────────────────────

def test_cap_file_content_short_file_unchanged():
    content = "import os\nprint('hello')\n"
    assert _cap_file_content(content) == content


def test_cap_file_content_large_file_has_head_and_tail():
    head = "# HEAD\n" + "x = 1\n" * 2000
    tail = "y = 2\n" * 2000 + "# TAIL\n"   # marker at the end, inside tail window
    big = head + tail
    result = _cap_file_content(big)
    assert "# HEAD" in result
    assert "# TAIL" in result
    assert "omitted" in result


# ── _extract_all_broken_files ─────────────────────────────────────────────────

def test_extract_all_broken_files_quoted_paths(healer, temp_workspace):
    for name in ("auth.py", "models.py"):
        open(os.path.join(temp_workspace, name), "w").close()

    failure = CommandResult(
        command="pytest tests/",
        exit_code=1,
        stdout='File "auth.py", line 5\nFile "models.py", line 10',
        stderr="",
    )
    found = healer._extract_all_broken_files(failure)
    assert "auth.py" in found
    assert "models.py" in found


def test_extract_all_broken_files_skips_test_files(healer, temp_workspace):
    os.makedirs(os.path.join(temp_workspace, "tests"), exist_ok=True)
    open(os.path.join(temp_workspace, "src.py"), "w").close()
    open(os.path.join(temp_workspace, "tests", "test_src.py"), "w").close()

    failure = CommandResult(
        command="pytest",
        exit_code=1,
        stdout='File "tests/test_src.py"\nFile "src.py"',
        stderr="",
    )
    found = healer._extract_all_broken_files(failure)
    assert "src.py" in found
    assert "tests/test_src.py" not in found


def test_extract_all_broken_files_returns_empty_when_no_match(healer):
    failure = CommandResult(
        command="pytest",
        exit_code=1,
        stdout="some generic error with no file paths",
        stderr="",
    )
    assert healer._extract_all_broken_files(failure) == []


def test_extract_all_broken_files_caps_at_eight(healer, temp_workspace):
    for i in range(12):
        open(os.path.join(temp_workspace, f"file{i}.py"), "w").close()

    stdout = " ".join(f'"file{i}.py"' for i in range(12))
    failure = CommandResult(command="pytest", exit_code=1, stdout=stdout, stderr="")
    found = healer._extract_all_broken_files(failure)
    assert len(found) <= 8


# ── blocking / auto-fix behaviour (via _fix_file_for_errors / heal internals) ─

def test_blocks_on_missing_tool(healer):
    """heal() should surface a blocked_reason when a tool is missing."""
    missing_result = CommandResult(
        command="ruff check .",
        exit_code=127,
        stdout="",
        stderr="/bin/sh: ruff: command not found",
    )

    async def fake_run(cmd, cwd):
        return missing_result

    with patch("codegen_agent.healer.run_shell_command", side_effect=lambda cmd, cwd: missing_result):
        report = asyncio.run(healer.heal(["ruff check ."]))

    assert not report.success
    assert report.blocked_reason is not None
    assert "ruff" in report.blocked_reason
    healer.llm_client.generate.assert_not_called()


def test_bootstraps_conftest_for_missing_root_module(healer, temp_workspace):
    stack_file = os.path.join(temp_workspace, "stack.py")
    with open(stack_file, "w") as f:
        f.write("class Stack:\n    pass\n")

    missing_mod = CommandResult(
        command="pytest tests/",
        exit_code=2,
        stdout="ModuleNotFoundError: No module named 'stack'",
        stderr="",
    )
    passing = CommandResult(command="pytest tests/", exit_code=0, stdout="", stderr="")

    call_count = 0

    def fake_run(cmd, cwd):
        nonlocal call_count
        call_count += 1
        return passing if call_count > 1 else missing_mod

    with patch("codegen_agent.healer.run_shell_command", side_effect=fake_run):
        report = asyncio.run(healer.heal(["pytest tests/"]))

    conftest_path = os.path.join(temp_workspace, "conftest.py")
    assert os.path.exists(conftest_path)
    assert "ROOT = os.path.dirname" in open(conftest_path).read()
    healer.llm_client.generate.assert_not_called()


def test_applies_ruff_autofix_without_llm(healer):
    failure = CommandResult(
        command="ruff check .",
        exit_code=1,
        stdout="",
        stderr="I001 Import block is un-sorted or un-formatted",
    )
    autofix_ok = CommandResult(command="ruff check --fix .", exit_code=0, stdout="", stderr="")
    passing = CommandResult(command="ruff check .", exit_code=0, stdout="", stderr="")

    call_count = 0

    def fake_run(cmd, cwd):
        nonlocal call_count
        call_count += 1
        if "ruff check --fix" in cmd:
            return autofix_ok
        return failure if call_count == 1 else passing

    with patch("codegen_agent.healer.run_shell_command", side_effect=fake_run):
        report = asyncio.run(healer.heal(["ruff check ."]))

    healer.llm_client.generate.assert_not_called()
    assert any("ruff" in (a.fix_applied or "") for a in report.attempts)
