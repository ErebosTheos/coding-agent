import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.llm_client import (
    CodexCLIClient,
    CommandExecutionResult,
    GeminiCLIClient,
    LLMClientError,
    LLMRateLimitError,
    LLMTimeoutError,
)


class LLMClientTests(unittest.TestCase):
    def test_codex_client_reads_output_file_response(self) -> None:
        captured_env: dict[str, str] = {}

        def runner(
            command: list[str],
            stdin_data: str,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
        ) -> CommandExecutionResult:
            captured_env.update(env)
            output_index = command.index("--output-last-message") + 1
            output_file = Path(command[output_index])
            output_file.write_text("print('fixed')\n", encoding="utf-8")
            return CommandExecutionResult(return_code=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            client = CodexCLIClient(
                api_key="test-key",
                workspace=tmp_dir,
                runner=runner,
            )

            result = client.generate_fix("fix this")

        self.assertEqual(result, "print('fixed')")
        self.assertEqual(captured_env["OPENAI_API_KEY"], "test-key")

    def test_codex_client_raises_rate_limit_error(self) -> None:
        def runner(
            command: list[str],
            stdin_data: str,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
        ) -> CommandExecutionResult:
            return CommandExecutionResult(return_code=1, stdout="", stderr="429 rate limit")

        with tempfile.TemporaryDirectory() as tmp_dir:
            client = CodexCLIClient(workspace=tmp_dir, runner=runner)
            with self.assertRaises(LLMRateLimitError):
                client.generate_fix("fix this")

    def test_gemini_client_raises_timeout_error(self) -> None:
        def runner(
            command: list[str],
            stdin_data: str,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
        ) -> CommandExecutionResult:
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds)

        with tempfile.TemporaryDirectory() as tmp_dir:
            client = GeminiCLIClient(workspace=tmp_dir, runner=runner)
            with self.assertRaises(LLMTimeoutError):
                client.generate_fix("fix this")

    def test_gemini_client_returns_stdout_response(self) -> None:
        captured_command: list[str] = []
        captured_env: dict[str, str] = {}

        def runner(
            command: list[str],
            stdin_data: str,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
        ) -> CommandExecutionResult:
            captured_command.extend(command)
            captured_env.update(env)
            return CommandExecutionResult(return_code=0, stdout="patched content\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            client = GeminiCLIClient(
                api_key="gemini-key",
                model="gemini-2.5-pro",
                workspace=tmp_dir,
                runner=runner,
            )

            result = client.generate_fix("fix this")

        self.assertEqual(result, "patched content")
        self.assertIn("--prompt", captured_command)
        self.assertIn("--model", captured_command)
        self.assertEqual(captured_env["GEMINI_API_KEY"], "gemini-key")

    def test_gemini_client_rejects_overly_large_prompt(self) -> None:
        def runner(
            command: list[str],
            stdin_data: str,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
        ) -> CommandExecutionResult:
            raise AssertionError("runner should not be called for oversized prompt")

        with tempfile.TemporaryDirectory() as tmp_dir:
            client = GeminiCLIClient(
                workspace=tmp_dir,
                max_prompt_chars=10,
                runner=runner,
            )
            with self.assertRaisesRegex(LLMClientError, "prompt is too large"):
                client.generate_fix("x" * 11)


if __name__ == "__main__":
    unittest.main()
