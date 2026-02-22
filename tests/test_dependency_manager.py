import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.dependency_manager import DependencyManager
from senior_agent.models import CommandResult


@dataclass
class ScriptedExecutor:
    command_results: dict[str, list[CommandResult]]
    command_calls: list[str] = field(default_factory=list)

    def __call__(self, command: str, workspace: Path) -> CommandResult:
        self.command_calls.append(command)
        scripted = self.command_results.get(command)
        if not scripted:
            return CommandResult(command=command, return_code=1, stderr="unconfigured command")
        return scripted.pop(0)


class DependencyManagerTests(unittest.TestCase):
    def test_installs_missing_python_module_with_pip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            executor = ScriptedExecutor(
                command_results={
                    "pip install requests": [
                        CommandResult(command="pip install requests", return_code=0, stdout="ok", stderr=""),
                    ]
                }
            )
            manager = DependencyManager(executor=executor)
            failure = CommandResult(
                command="python -m pytest",
                return_code=1,
                stdout="",
                stderr="ModuleNotFoundError: No module named 'requests'",
            )

            fixed = manager.check_and_fix_dependencies(failure, workspace)

            self.assertTrue(fixed)
            self.assertEqual(executor.command_calls, ["pip install requests"])

    def test_installs_missing_node_module_with_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "package.json").write_text("{}", encoding="utf-8")
            executor = ScriptedExecutor(
                command_results={
                    "npm install left-pad": [
                        CommandResult(command="npm install left-pad", return_code=0, stdout="ok", stderr=""),
                    ]
                }
            )
            manager = DependencyManager(executor=executor)
            failure = CommandResult(
                command="npm test",
                return_code=1,
                stdout="",
                stderr="Error: Cannot find module 'left-pad'",
            )

            fixed = manager.check_and_fix_dependencies(failure, workspace)

            self.assertTrue(fixed)
            self.assertEqual(executor.command_calls, ["npm install left-pad"])

    def test_returns_false_when_no_missing_dependency_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            executor = ScriptedExecutor(command_results={})
            manager = DependencyManager(executor=executor)
            failure = CommandResult(
                command="python -m pytest",
                return_code=1,
                stdout="",
                stderr="AssertionError: expected 2 == 3",
            )

            fixed = manager.check_and_fix_dependencies(failure, workspace)

            self.assertFalse(fixed)
            self.assertEqual(executor.command_calls, [])


if __name__ == "__main__":
    unittest.main()
