import io
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main as cli_main
from senior_agent.engine import SeniorAgent
from senior_agent.models import CommandResult, FailureContext, FixOutcome


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, command: str, workspace: Path) -> CommandResult:
        self.calls += 1
        if self.calls == 1:
            return CommandResult(
                command=command,
                return_code=1,
                stdout="",
                stderr="assertionerror",
            )
        return CommandResult(
            command=command,
            return_code=0,
            stdout="ok",
            stderr="",
        )


@dataclass
class FakeFixStrategy:
    name: str = "fake-fix"
    apply_calls: int = 0

    def apply(self, context: FailureContext) -> FixOutcome:
        self.apply_calls += 1
        return FixOutcome(
            applied=True,
            note="patched by fake strategy",
            diff_summary=("Modified sample.py: +1/-1 lines.",),
        )


class MainCLITests(unittest.TestCase):
    def test_main_runs_end_to_end_with_fake_agent(self) -> None:
        fake_executor = FakeExecutor()
        fake_strategy = FakeFixStrategy()
        fake_agent = SeniorAgent(
            max_attempts=1,
            executor=fake_executor,
            default_strategies=(fake_strategy,),
        )
        stdout_buffer = io.StringIO()

        with (
            mock.patch.object(
                sys,
                "argv",
                ["main.py", "python -m pytest", "--provider", "codex"],
            ),
            mock.patch("main.create_default_senior_agent", return_value=fake_agent),
            redirect_stdout(stdout_buffer),
            self.assertRaises(SystemExit) as exit_info,
        ):
            cli_main.main()

        self.assertEqual(exit_info.exception.code, 0)
        output = stdout_buffer.getvalue()
        self.assertIn("SESSION SUMMARY", output)
        self.assertIn("Status:      SUCCESS", output)
        self.assertIn("fake-fix", output)
        self.assertEqual(fake_executor.calls, 2)
        self.assertEqual(fake_strategy.apply_calls, 1)

    def test_main_rejects_dangerous_command_input(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["main.py", "rm -rf /"]),
            mock.patch("main.create_default_senior_agent") as create_agent_mock,
            self.assertRaises(SystemExit) as exit_info,
        ):
            cli_main.main()

        self.assertEqual(exit_info.exception.code, 2)
        create_agent_mock.assert_not_called()

    def test_main_requires_command_when_not_serving(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["main.py"]),
            mock.patch("main.create_default_senior_agent") as create_agent_mock,
            self.assertRaises(SystemExit) as exit_info,
        ):
            cli_main.main()

        self.assertEqual(exit_info.exception.code, 2)
        create_agent_mock.assert_not_called()

    def test_main_serve_mode_dispatches_to_web_api(self) -> None:
        run_server_mock = mock.Mock()
        fake_module = types.ModuleType("senior_agent.web_api")
        fake_module.run_server = run_server_mock

        with (
            mock.patch.dict(sys.modules, {"senior_agent.web_api": fake_module}),
            mock.patch.object(
                sys,
                "argv",
                [
                    "main.py",
                    "--serve",
                    "--provider",
                    "codex",
                    "--workspace",
                    ".",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ],
            ),
            mock.patch("main.create_default_senior_agent") as create_agent_mock,
        ):
            cli_main.main()

        create_agent_mock.assert_not_called()
        run_server_mock.assert_called_once_with(
            host="127.0.0.1",
            port=8000,
            provider="codex",
            workspace=".",
            verbose=False,
            api_key=None,
            allow_unsecure=False,
        )


if __name__ == "__main__":
    unittest.main()
