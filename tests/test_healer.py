import asyncio
import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from codegen_agent.healer import Healer
from codegen_agent.models import CommandResult

@pytest.fixture
def temp_workspace():
    """Create a temporary workspace for testing Healer."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield tmp_dir

@pytest.fixture
def healer(temp_workspace):
    """Create a Healer instance with a mock LLMClient and a temporary workspace."""
    mock_llm = MagicMock()
    return Healer(llm_client=mock_llm, workspace=temp_workspace)

def test_get_most_recent_file_filters_by_extension(healer, temp_workspace):
    # Create allowed files
    py_file = os.path.join(temp_workspace, "test.py")
    with open(py_file, "w") as f:
        f.write("print('hello')")
    
    # Create disallowed files
    pyc_file = os.path.join(temp_workspace, "test.pyc")
    with open(pyc_file, "wb") as f:
        f.write(b"\x00\x01\x02")
    
    bin_file = os.path.join(temp_workspace, "test.bin")
    with open(bin_file, "wb") as f:
        f.write(b"something binary")
        
    # Set mtime so pyc is "newer"
    os.utime(py_file, (100, 100))
    os.utime(pyc_file, (200, 200))
    os.utime(bin_file, (300, 300))
    
    recent_file = healer._get_most_recent_file()
    
    # It should pick test.py because test.pyc and test.bin are not in ALLOWED_EXTENSIONS
    assert recent_file == "test.py"

def test_extract_target_file_uses_allowed_extensions(healer):
    # Create the file first
    py_path = os.path.join(healer.workspace, "test.py")
    with open(py_path, "w") as f:
        f.write("")
        
    output = 'Error in File "test.py", line 1'
    assert healer._extract_target_file(output) == "test.py"
    
    output_disallowed = 'Error in File "test.pyc", line 1'
    # Even if it exists, it should not be extracted if not in ALLOWED_EXTENSIONS
    pyc_path = os.path.join(healer.workspace, "test.pyc")
    with open(pyc_path, "w") as f:
        f.write("")
        
    assert healer._extract_target_file(output_disallowed) is None
    
    valid_path = os.path.join(healer.workspace, "valid.py")
    with open(valid_path, "w") as f:
        f.write("")
    
    output_valid = 'Error in File "valid.py", line 1'
    assert healer._extract_target_file(output_valid) == "valid.py"


def test_extract_target_file_skips_tests_by_default(healer):
    test_path = os.path.join(healer.workspace, "tests", "test_logic.py")
    os.makedirs(os.path.dirname(test_path), exist_ok=True)
    with open(test_path, "w") as f:
        f.write("")

    output = 'Error in File "tests/test_logic.py", line 1'
    assert healer._extract_target_file(output) is None


def test_extract_target_file_can_edit_tests_when_enabled(temp_workspace):
    mock_llm = MagicMock()
    permissive_healer = Healer(
        llm_client=mock_llm,
        workspace=temp_workspace,
        allow_test_file_edits=True,
    )
    test_path = os.path.join(temp_workspace, "tests", "test_logic.py")
    os.makedirs(os.path.dirname(test_path), exist_ok=True)
    with open(test_path, "w") as f:
        f.write("")

    output = 'Error in File "tests/test_logic.py", line 1'
    assert permissive_healer._extract_target_file(output) == "tests/test_logic.py"


def test_fix_single_failure_blocks_missing_tool(healer):
    failure = CommandResult(
        command="ruff check .",
        exit_code=127,
        stdout="",
        stderr="/bin/sh: ruff: command not found",
    )

    result = asyncio.run(healer._fix_single_failure(failure, attempt_number=1))

    assert isinstance(result, str)
    assert "Missing tool 'ruff'" in result
    healer.llm_client.generate.assert_not_called()


def test_get_most_recent_file_ignores_pytest_cache(healer, temp_workspace):
    src_file = os.path.join(temp_workspace, "main.py")
    with open(src_file, "w") as f:
        f.write("print('ok')\n")

    cache_dir = os.path.join(temp_workspace, ".pytest_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, "README.md")
    with open(cache_file, "w") as f:
        f.write("cache")

    os.utime(src_file, (100, 100))
    os.utime(cache_file, (200, 200))

    assert healer._get_most_recent_file() == "main.py"


def test_fix_single_failure_bootstraps_conftest_for_missing_root_module(healer):
    stack_file = os.path.join(healer.workspace, "stack.py")
    with open(stack_file, "w") as f:
        f.write("class Stack:\n    pass\n")

    failure = CommandResult(
        command="pytest tests/",
        exit_code=2,
        stdout="ModuleNotFoundError: No module named 'stack'",
        stderr="",
    )

    result = asyncio.run(healer._fix_single_failure(failure, attempt_number=1))

    assert result is not None
    assert "conftest.py" in result.changed_files
    conftest_path = os.path.join(healer.workspace, "conftest.py")
    assert os.path.exists(conftest_path)
    with open(conftest_path, "r") as f:
        assert "ROOT = os.path.dirname" in f.read()
    healer.llm_client.generate.assert_not_called()


def test_fix_single_failure_applies_ruff_autofix_without_llm(healer):
    failure = CommandResult(
        command="ruff check .",
        exit_code=1,
        stdout="",
        stderr="I001 Import block is un-sorted or un-formatted",
    )
    autofix = CommandResult(
        command="ruff check --fix .",
        exit_code=0,
        stdout="",
        stderr="",
    )

    with patch("codegen_agent.healer.run_shell_command", return_value=autofix) as mocked:
        result = asyncio.run(healer._fix_single_failure(failure, attempt_number=1))

    assert result is not None
    assert result.failure_type.value == "LINT_TYPE_FAILURE"
    assert "ruff check --fix ." in result.fix_applied
    mocked.assert_called_once()
    healer.llm_client.generate.assert_not_called()
