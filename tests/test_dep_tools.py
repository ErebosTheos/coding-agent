import asyncio
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock, patch

from codegen_agent.dependency_manager import DependencyManager
from codegen_agent.models import GeneratedFile


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


def test_passlib_project_pins_bcrypt_compat(tmp_path):
    mock_result = MagicMock()
    mock_result.exit_code = 0
    mock_result.stderr = ""

    dm = DependencyManager(workspace=str(tmp_path))
    dm.executor = MagicMock(return_value=mock_result)
    generated_files = [
        GeneratedFile(
            file_path="src/auth.py",
            content="from passlib.context import CryptContext\n",
            node_id="auth",
            sha256="x",
        )
    ]

    with patch(
        "codegen_agent.dependency_manager.dist_version",
        side_effect=PackageNotFoundError,
    ):
        result = asyncio.run(
            dm.resolve_and_install(
                generated_files=generated_files,
                plan=MagicMock(),
                validation_commands=[],
            )
        )

    calls = [str(c) for c in dm.executor.call_args_list]
    assert any("bcrypt==4.0.1" in c for c in calls), (
        f"Expected bcrypt compatibility pin, got: {calls}"
    )
    assert "Pinned bcrypt==4.0.1" in result.get("compatibility_fixes", [])


def test_passlib_project_skips_bcrypt_pin_when_already_compatible(tmp_path):
    dm = DependencyManager(workspace=str(tmp_path))
    dm.executor = MagicMock()
    generated_files = [
        GeneratedFile(
            file_path="src/auth.py",
            content="from passlib.context import CryptContext\n",
            node_id="auth",
            sha256="x",
        )
    ]

    with patch("codegen_agent.dependency_manager.dist_version", return_value="4.0.1"):
        result = asyncio.run(
            dm.resolve_and_install(
                generated_files=generated_files,
                plan=MagicMock(),
                validation_commands=[],
            )
        )

    dm.executor.assert_not_called()
    assert result.get("compatibility_fixes", []) == []
