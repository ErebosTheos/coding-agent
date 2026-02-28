import asyncio
from unittest.mock import MagicMock, patch

from codegen_agent.dependency_manager import DependencyManager


def test_missing_whitelisted_tool_is_installed():
    mock_result = MagicMock()
    mock_result.exit_code = 0
    mock_result.stderr = ""

    dm = DependencyManager(workspace="/tmp")
    dm.executor = MagicMock(return_value=mock_result)

    with patch("shutil.which", return_value=None):
        asyncio.run(
            dm.resolve_and_install(
                generated_files=[],
                plan=MagicMock(),
                validation_commands=["ruff check ."],
            )
        )

    calls = [str(c) for c in dm.executor.call_args_list]
    assert any("pip" in c and "ruff" in c for c in calls), (
        f"Expected pip install ruff call, got: {calls}"
    )


def test_non_whitelisted_tool_is_not_installed():
    dm = DependencyManager(workspace="/tmp")
    dm.executor = MagicMock()

    with patch("shutil.which", return_value=None):
        asyncio.run(
            dm.resolve_and_install(
                generated_files=[],
                plan=MagicMock(),
                validation_commands=["make test", "./run_tests.sh"],
            )
        )

    dm.executor.assert_not_called()


def test_already_installed_tool_is_skipped():
    dm = DependencyManager(workspace="/tmp")
    dm.executor = MagicMock()

    with patch("shutil.which", return_value="/usr/bin/ruff"):
        asyncio.run(
            dm.resolve_and_install(
                generated_files=[],
                plan=MagicMock(),
                validation_commands=["ruff check ."],
            )
        )

    dm.executor.assert_not_called()
