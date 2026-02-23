import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.llm_client import (
    CodexCLIClient,
    CommandExecutionResult,
    DEFAULT_TRANSPORT_SAFETY_PROMPT,
    DEFAULT_TRANSPORT_SYSTEM_PROMPT,
    GeminiCLIClient,
    LocalOffloadClient,
    LLMClientError,
    LLMRateLimitError,
    LLMTimeoutError,
    MultiCloudRouter,
    build_transport_prompt,
    parse_streamed_response,
)


class LLMClientTests(unittest.TestCase):
    def test_codex_client_reads_output_file_response(self) -> None:
        captured_env: dict[str, str] = {}
        captured_stdin: list[str] = []

        def runner(
            command: list[str],
            stdin_data: str,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
        ) -> CommandExecutionResult:
            captured_env.update(env)
            captured_stdin.append(stdin_data)
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
        self.assertIn("<<SYSTEM>>", captured_stdin[0])
        self.assertIn("<<SAFETY>>", captured_stdin[0])
        self.assertIn("fix this", captured_stdin[0])

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

    def test_local_offload_client_uses_ollama_run_contract(self) -> None:
        captured: dict[str, object] = {}

        def runner(
            command: list[str],
            stdin_data: str,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: int,
        ) -> CommandExecutionResult:
            captured["command"] = command
            captured["stdin"] = stdin_data
            return CommandExecutionResult(return_code=0, stdout="local fix\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            client = LocalOffloadClient(
                model="deepseek-coder:7b",
                workspace=tmp_dir,
                runner=runner,
            )
            result = client.generate_fix("fix locally")

        self.assertEqual(result, "local fix")
        assert isinstance(captured["command"], list)
        self.assertEqual(captured["command"][:2], ["ollama", "run"])
        assert isinstance(captured["stdin"], str)
        self.assertIn("fix locally", captured["stdin"])
        self.assertIn("<<USER_PROMPT>>", captured["stdin"])

    def test_multi_cloud_router_uses_local_for_low_complexity(self) -> None:
        class StaticClient:
            def __init__(self, response: str) -> None:
                self.response = response
                self.calls = 0

            def generate_fix(self, prompt: str) -> str:
                self.calls += 1
                return self.response

        local = StaticClient("local")
        cloud = StaticClient("cloud")
        router = MultiCloudRouter(
            cloud_clients=(cloud,),
            local_client=local,
            local_complexity_threshold=10,
        )

        result = router.generate_fix("small prompt")

        self.assertEqual(result, "local")
        self.assertEqual(local.calls, 1)
        self.assertEqual(cloud.calls, 0)

    def test_multi_cloud_router_falls_back_to_cloud_after_local_failure(self) -> None:
        class FailClient:
            def generate_fix(self, prompt: str) -> str:
                raise LLMClientError("local failed")

        class StaticClient:
            def __init__(self, response: str) -> None:
                self.response = response
                self.calls = 0

            def generate_fix(self, prompt: str) -> str:
                self.calls += 1
                return self.response

        cloud = StaticClient("cloud")
        router = MultiCloudRouter(
            cloud_clients=(cloud,),
            local_client=FailClient(),
            local_complexity_threshold=10,
        )

        result = router.generate_fix("small prompt")

        self.assertEqual(result, "cloud")
        self.assertEqual(cloud.calls, 1)

    def test_multi_cloud_router_enforces_budget_circuit_breaker(self) -> None:
        class StaticClient:
            def generate_fix(self, prompt: str) -> str:
                return "cloud"

        router = MultiCloudRouter(
            cloud_clients=(StaticClient(),),
            local_client=None,
            session_budget_usd=0.01,
            estimated_cloud_request_cost_usd=0.02,
        )

        with self.assertRaisesRegex(LLMClientError, "circuit breaker"):
            router.generate_fix("prompt")

    def test_multi_cloud_router_speculative_race_uses_fastest_cloud_client(self) -> None:
        class SlowClient:
            def __init__(self) -> None:
                self.calls = 0

            def generate_fix(self, prompt: str) -> str:
                import time

                self.calls += 1
                time.sleep(0.2)
                return "slow"

        class FastClient:
            def __init__(self) -> None:
                self.calls = 0

            def generate_fix(self, prompt: str) -> str:
                self.calls += 1
                return "fast"

        slow = SlowClient()
        fast = FastClient()
        router = MultiCloudRouter(
            cloud_clients=(slow, fast),
            local_client=None,
            enable_speculative_racing=True,
            cloud_speculative_threshold=1,
            max_race_clients=2,
            race_timeout_seconds=5.0,
        )

        result = router.generate_fix("complex architecture prompt")

        self.assertEqual(result, "fast")
        self.assertEqual(fast.calls, 1)
        self.assertEqual(slow.calls, 1)

    def test_multi_cloud_router_budget_guard_band_forces_local_mode(self) -> None:
        class StaticClient:
            def __init__(self, response: str) -> None:
                self.response = response
                self.calls = 0

            def generate_fix(self, prompt: str) -> str:
                self.calls += 1
                return self.response

        cloud = StaticClient("cloud")
        local = StaticClient("local")
        router = MultiCloudRouter(
            cloud_clients=(cloud,),
            local_client=local,
            local_complexity_threshold=1,
            session_budget_usd=0.05,
            estimated_cloud_request_cost_usd=0.02,
            budget_guard_band_requests=3,
        )
        # First call can use cloud.
        self.assertEqual(router.generate_fix("this is a very large architecture prompt"), "cloud")
        # Guard band should force subsequent calls to local.
        self.assertEqual(router.generate_fix("another very large architecture prompt"), "local")
        self.assertEqual(cloud.calls, 1)
        self.assertEqual(local.calls, 1)

    def test_build_transport_prompt_has_deterministic_sections(self) -> None:
        rendered = build_transport_prompt("  patch this file  ")
        self.assertIn(DEFAULT_TRANSPORT_SYSTEM_PROMPT, rendered)
        self.assertIn(DEFAULT_TRANSPORT_SAFETY_PROMPT, rendered)
        self.assertIn("patch this file", rendered)
        self.assertTrue(rendered.startswith("<<SYSTEM>>"))

    def test_parse_streamed_response_prefers_closed_code_fence(self) -> None:
        fragments = [
            "```python\n",
            "def handler():\n",
            "    return 1\n",
            "``` trailing",
        ]
        parsed = parse_streamed_response(fragments)
        self.assertEqual(parsed, "def handler():\n    return 1\n")

    def test_multi_cloud_router_generate_fix_stream_parses_fragments(self) -> None:
        class StreamingClient:
            def generate_fix(self, prompt: str) -> str:
                return "fallback"

            def stream_fix(self, prompt: str):
                yield "```py\n"
                yield "x = 1\n"
                yield "```\n"

        fragments: list[str] = []
        router = MultiCloudRouter(
            cloud_clients=(StreamingClient(),),
            local_client=None,
            enable_speculative_racing=False,
        )
        result = router.generate_fix_stream(
            "stream this",
            on_fragment=fragments.append,
        )
        self.assertEqual(result, "x = 1\n")
        self.assertEqual(fragments, ["```py\n", "x = 1\n", "```\n"])

    def test_multi_cloud_router_generate_fix_stream_async(self) -> None:
        class StreamingClient:
            def generate_fix(self, prompt: str) -> str:
                return "fallback"

            def stream_fix(self, prompt: str):
                yield "part one "
                yield "part two"

        router = MultiCloudRouter(
            cloud_clients=(StreamingClient(),),
            local_client=None,
            enable_speculative_racing=False,
        )
        result = asyncio.run(router.generate_fix_stream_async("prompt"))
        self.assertEqual(result, "part one part two")


if __name__ == "__main__":
    unittest.main()
