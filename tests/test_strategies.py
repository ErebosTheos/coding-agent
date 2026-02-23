import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.llm_client import LLMClientError
from senior_agent.models import CommandResult, FailureContext, FailureType
from senior_agent.strategies import (
    LLMStrategy,
    RegexReplaceStrategy,
    RepoRegexReplaceStrategy,
)


class FakeLLMClient:
    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self.response = response or ""
        self.error = error
        self.prompts: list[str] = []

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.response


class DelayedLLMClient:
    def __init__(self, *, delay_seconds: float, response: str | None = None, error: Exception | None = None) -> None:
        self.delay_seconds = delay_seconds
        self.response = response or ""
        self.error = error
        self.prompts: list[str] = []

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        time.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return self.response


class StreamingLLMClient:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls = 0

    def generate_fix(self, prompt: str) -> str:
        raise AssertionError("streaming test should use stream_fix instead of generate_fix")

    def stream_fix(self, prompt: str):
        self.calls += 1
        for chunk in self.chunks:
            yield chunk


class LLMStrategyTests(unittest.TestCase):
    def test_overwrites_detected_file_with_llm_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_dir = workspace / "pkg"
            target_dir.mkdir()
            target_file = target_dir / "main.py"
            target_file.write_text("print('broken')\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="print('fixed')\n")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult(
                    "python -m pytest",
                    1,
                    "",
                    "pkg/main.py:3: error: NameError",
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "print('fixed')\n")
            self.assertEqual(len(outcome.changed_files), 1)
            self.assertIn("pkg/main.py", outcome.note)
            self.assertEqual(len(llm_client.prompts), 1)
            self.assertTrue(outcome.diff_summary)
            self.assertIn("pkg/main.py", outcome.diff_summary[0])
            self.assertTrue(any("Byte delta" in entry for entry in outcome.diff_summary))

    def test_extracts_code_from_markdown_fence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            llm_client = FakeLLMClient(
                response="```python\nx = 2\n```\nSome explanation",
            )
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "file.py:1: error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 2\n")
            self.assertTrue(outcome.diff_summary)

    def test_returns_not_applied_when_no_paths_in_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            llm_client = FakeLLMClient(response="print('fixed')\n")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "generic failure"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("No candidate source files", outcome.note)

    def test_blocks_out_of_workspace_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "repo"
            workspace.mkdir()
            outside_file = root / "outside.py"
            outside_file.write_text("print('outside')\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="print('patched')\n")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult(
                    "python app.py",
                    1,
                    "",
                    "../outside.py:1: error: NameError",
                ),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("outside workspace or missing", outcome.note)
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "print('outside')\n")

    def test_propagates_llm_client_failure_as_not_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            llm_client = FakeLLMClient(error=LLMClientError("rate limited"))
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "file.py:1: error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("LLM error", outcome.note)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 1\n")

    def test_parallel_fallback_uses_first_successful_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            slow_failing = DelayedLLMClient(
                delay_seconds=0.35,
                error=LLMClientError("primary unavailable"),
            )
            fast_success = DelayedLLMClient(
                delay_seconds=0.05,
                response="x = 2\n",
            )
            strategy = LLMStrategy(
                llm_client=slow_failing,
                fallback_llm_clients=(fast_success,),
            )
            context = FailureContext(
                command_result=CommandResult(
                    "python file.py",
                    1,
                    "",
                    "file.py:1: error",
                ),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            started = time.monotonic()
            outcome = strategy.apply(context)
            elapsed = time.monotonic() - started

            self.assertTrue(outcome.applied)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 2\n")
            self.assertEqual(len(slow_failing.prompts), 1)
            self.assertEqual(len(fast_success.prompts), 1)
            self.assertLess(elapsed, 0.3)

    def test_streaming_speculative_parsing_uses_client_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            streaming = StreamingLLMClient(
                chunks=["```python\n", "x = 2\n", "```\n"],
            )
            strategy = LLMStrategy(llm_client=streaming)
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "file.py:1: error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 2\n")
            self.assertEqual(streaming.calls, 1)

    def test_response_cache_reuses_identical_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="x = 2\n")
            strategy = LLMStrategy(
                llm_client=llm_client,
                enable_response_cache=True,
                response_cache_max_entries=16,
            )
            context = FailureContext(
                command_result=CommandResult(
                    "python file.py",
                    1,
                    "",
                    "file.py:1: error",
                ),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            first = strategy.apply(context)
            target_file.write_text("x = 1\n", encoding="utf-8")
            second = strategy.apply(context)

            self.assertTrue(first.applied)
            self.assertTrue(second.applied)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 2\n")
            self.assertEqual(len(llm_client.prompts), 1)

    def test_truncates_error_output_in_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")
            long_error = ("E" * 2000) + "\nfile.py:1: error"

            llm_client = FakeLLMClient(response="x = 2\n")
            strategy = LLMStrategy(
                llm_client=llm_client,
                max_error_chars=256,
            )
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", long_error),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            prompt = llm_client.prompts[0]
            self.assertIn("[TRUNCATED: error output exceeded 256 characters]", prompt)

    def test_includes_top_three_files_in_prompt_context_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            file_one = workspace / "one.py"
            file_two = workspace / "two.py"
            file_three = workspace / "three.py"
            file_four = workspace / "four.py"
            file_one.write_text("x = 1\n", encoding="utf-8")
            file_two.write_text("y = 2\n", encoding="utf-8")
            file_three.write_text("z = 3\n", encoding="utf-8")
            file_four.write_text("w = 4\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="x = 10\n")
            strategy = LLMStrategy(llm_client=llm_client, max_context_files=3)
            context = FailureContext(
                command_result=CommandResult(
                    "python -m pytest",
                    1,
                    "",
                    (
                        "one.py:1: error: first\n"
                        "two.py:1: error: second\n"
                        "three.py:1: error: third\n"
                        "four.py:1: error: fourth"
                    ),
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            self.assertEqual(file_one.read_text(encoding="utf-8"), "x = 10\n")
            self.assertEqual(file_two.read_text(encoding="utf-8"), "y = 2\n")
            self.assertEqual(file_three.read_text(encoding="utf-8"), "z = 3\n")
            prompt = llm_client.prompts[0]
            self.assertIn("Primary Target: one.py", prompt)
            self.assertIn("Additional Context: two.py", prompt)
            self.assertIn("Additional Context: three.py", prompt)
            self.assertNotIn("Additional Context: four.py", prompt)

    def test_uses_line_chunk_prompt_and_merges_chunk_fix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "target.py"
            target_file.write_text(
                "".join(f"line {line_number}\n" for line_number in range(1, 121)),
                encoding="utf-8",
            )

            llm_client = FakeLLMClient(
                response=(
                    "line 58\n"
                    "line 59\n"
                    "line 60 fixed\n"
                    "line 61\n"
                    "line 62\n"
                )
            )
            strategy = LLMStrategy(llm_client=llm_client, context_chunk_radius=2)
            context = FailureContext(
                command_result=CommandResult(
                    "python -m pytest",
                    1,
                    "",
                    "target.py:60:1: error: SyntaxError",
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            updated_lines = target_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(updated_lines[59], "line 60 fixed")
            prompt = llm_client.prompts[0]
            self.assertIn("Primary Target: target.py", prompt)
            self.assertIn("Return ONLY the corrected code excerpt", prompt)
            self.assertIn("Target snippet lines: 58-62 of 120", prompt)
            self.assertIn("line 58", prompt)
            self.assertIn("line 62", prompt)
            self.assertNotIn("line 10\n", prompt)
            self.assertNotIn("line 110\n", prompt)

    def test_preserves_full_target_file_when_prompt_context_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "target.py"
            target_file.write_text(
                "".join(f"line {line_number}\n" for line_number in range(1, 201)),
                encoding="utf-8",
            )

            llm_client = FakeLLMClient(
                response=(
                    "line 149\n"
                    "line 150 fixed\n"
                    "line 151\n"
                )
            )
            strategy = LLMStrategy(
                llm_client=llm_client,
                context_chunk_radius=1,
                max_file_chars=80,
            )
            context = FailureContext(
                command_result=CommandResult(
                    "python -m pytest",
                    1,
                    "",
                    "target.py:150:1: error: SyntaxError",
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            updated_lines = target_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(updated_lines), 200)
            self.assertEqual(updated_lines[149], "line 150 fixed")
            self.assertEqual(updated_lines[0], "line 1")
            self.assertEqual(updated_lines[-1], "line 200")

    def test_blocks_chunk_mode_when_llm_returns_full_file_instead_of_snippet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "target.py"
            original = "".join(f"line {line_number}\n" for line_number in range(1, 51))
            target_file.write_text(original, encoding="utf-8")

            llm_client = FakeLLMClient(response=original)
            strategy = LLMStrategy(
                llm_client=llm_client,
                context_chunk_radius=1,
                max_chunk_line_multiplier=2.0,
            )
            context = FailureContext(
                command_result=CommandResult(
                    "python -m pytest",
                    1,
                    "",
                    "target.py:10:1: error: SyntaxError",
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("expected a snippet near", outcome.note)
            self.assertEqual(target_file.read_text(encoding="utf-8"), original)

    def test_uses_full_file_prompt_when_line_number_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "sample.py"
            target_file.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="alpha = 1\nbeta = 3\n")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult(
                    "python sample.py",
                    1,
                    "",
                    "sample.py: error: NameError",
                ),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            prompt = llm_client.prompts[0]
            self.assertIn("Primary Target: sample.py", prompt)
            self.assertIn("Return ONLY the full corrected file content", prompt)
            self.assertIn("--- Code for sample.py ---\nalpha = 1\nbeta = 2\n", prompt)

    def test_includes_symbol_aware_context_with_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "target.py"
            dependent_file = workspace / "consumer.py"
            target_file.write_text(
                "def process(value: int) -> int:\n"
                "    return value + 1\n",
                encoding="utf-8",
            )
            dependent_file.write_text(
                "from target import process\n"
                "VALUE = process(5)\n",
                encoding="utf-8",
            )

            llm_client = FakeLLMClient(
                response=(
                    "def process(value: int) -> int:\n"
                    "    return value + 2\n"
                )
            )
            strategy = LLMStrategy(
                llm_client=llm_client,
                context_chunk_radius=1,
                enable_symbol_context=True,
                max_symbol_targets=2,
            )
            context = FailureContext(
                command_result=CommandResult(
                    "python -m pytest",
                    1,
                    "",
                    "target.py:2:1: error: AssertionError",
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            prompt = llm_client.prompts[0]
            self.assertIn("Symbol-Aware Context:", prompt)
            self.assertIn("Target Definition: process", prompt)
            self.assertIn("Immediate Dependent: consumer.py", prompt)
            self.assertIn("LSP-Injected Context:", prompt)

    def test_lsp_context_gracefully_falls_back_when_pyright_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "target.py"
            target_file.write_text("x = 1\n", encoding="utf-8")
            llm_client = FakeLLMClient(response="x = 2\n")
            strategy = LLMStrategy(llm_client=llm_client, enable_lsp_context=True)
            context = FailureContext(
                command_result=CommandResult(
                    "python -m pytest",
                    1,
                    "",
                    "target.py:1: error",
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            with mock.patch("shutil.which", return_value=None):
                outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            prompt = llm_client.prompts[0]
            self.assertIn("LSP-Injected Context:", prompt)
            self.assertIn("\nNone\n", prompt)

    def test_extract_candidate_paths_supports_multiple_languages(self) -> None:
        stderr = (
            "src/app.js:12: error\n"
            "core/engine.go:7: error\n"
            "native/main.rs:3: error\n"
            "vm/runtime.kt:9: error\n"
            "src/ignore.txt:1: error"
        )

        paths = LLMStrategy._extract_candidate_paths(stderr)

        self.assertIn("src/app.js", paths)
        self.assertIn("core/engine.go", paths)
        self.assertIn("native/main.rs", paths)
        self.assertIn("vm/runtime.kt", paths)
        self.assertNotIn("src/ignore.txt", paths)

    def test_extract_file_references_prefers_line_hint_from_later_duplicate(self) -> None:
        stderr = (
            "src/app.py: error: first mention without line\n"
            "src/app.py:42:3: error: second mention with line"
        )

        references = LLMStrategy._extract_file_references(stderr)

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].path, "src/app.py")
        self.assertEqual(references[0].line_number, 42)

    def test_applies_fix_for_non_python_language_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "engine.go"
            target_file.write_text("package main\n\nfunc main() {}\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="package main\n\nfunc main(){ }\n")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult(
                    "go test ./...",
                    1,
                    "",
                    "engine.go:3:1: error: formatting issue",
                ),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            self.assertIn("engine.go", outcome.note)
            self.assertEqual(
                target_file.read_text(encoding="utf-8"),
                "package main\n\nfunc main(){ }\n",
            )

    def test_blocks_explosive_output_growth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            llm_client = FakeLLMClient(response=("y = 2\n" * 2000))
            strategy = LLMStrategy(llm_client=llm_client, max_growth_factor=5.0)
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "file.py: error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("output grew", outcome.note)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 1\n")

    def test_blocks_destructive_shrink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            original = ("line = 'keep'\n" * 100)
            target_file.write_text(original, encoding="utf-8")

            llm_client = FakeLLMClient(response="x = 1\n")
            strategy = LLMStrategy(
                llm_client=llm_client,
                min_retention_ratio=0.2,
                min_original_chars_for_retention_check=100,
            )
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "file.py: error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("output shrank", outcome.note)
            self.assertEqual(target_file.read_text(encoding="utf-8"), original)

    def test_blocks_non_text_llm_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            llm_client = FakeLLMClient(response=b"\x00\x01")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "file.py:1: error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("non-text output", outcome.note)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 1\n")

    def test_blocks_binary_like_control_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "file.py"
            target_file.write_text("x = 1\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="ok\n" + ("\x01" * 100))
            strategy = LLMStrategy(llm_client=llm_client, max_control_char_ratio=0.01)
            context = FailureContext(
                command_result=CommandResult("python file.py", 1, "", "file.py:1: error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("appears non-text", outcome.note)
            self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 1\n")

    def test_rejects_symlink_that_resolves_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "repo"
            workspace.mkdir()
            outside_file = root / "outside.py"
            outside_file.write_text("print('outside')\n", encoding="utf-8")
            link_file = workspace / "linked.py"
            try:
                link_file.symlink_to(outside_file)
            except OSError:
                self.skipTest("Symlinks are not supported in this environment.")

            llm_client = FakeLLMClient(response="print('patched')\n")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult(
                    "python app.py",
                    1,
                    "",
                    "linked.py:1: error: NameError",
                ),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("outside workspace or missing", outcome.note)
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "print('outside')\n")

    def test_handles_permission_error_when_reading_context_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            denied_file = workspace / "denied.py"
            denied_file.write_text("x = 1\n", encoding="utf-8")

            llm_client = FakeLLMClient(response="x = 2\n")
            strategy = LLMStrategy(llm_client=llm_client)
            context = FailureContext(
                command_result=CommandResult(
                    "python denied.py",
                    1,
                    "",
                    "denied.py:1: error: NameError",
                ),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            original_read_text = Path.read_text

            def patched_read_text(path_obj: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
                if path_obj.resolve() == denied_file.resolve():
                    raise PermissionError("permission denied")
                return original_read_text(path_obj, *args, **kwargs)

            with mock.patch("pathlib.Path.read_text", autospec=True, side_effect=patched_read_text):
                outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("Unable to read target file denied.py", outcome.note)
            self.assertEqual(len(llm_client.prompts), 0)


class RegexReplaceStrategyTests(unittest.TestCase):
    def test_replaces_text_when_pattern_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target = workspace / "sample.txt"
            target.write_text("value = buggy\n", encoding="utf-8")

            strategy = RegexReplaceStrategy(
                name="replace-buggy",
                target_file="sample.txt",
                pattern=r"buggy",
                replacement="fixed",
            )
            context = FailureContext(
                command_result=CommandResult("cmd", 1, "", "error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            resolved_changed_files = {path.resolve() for path in outcome.changed_files}
            self.assertIn(target.resolve(), resolved_changed_files)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = fixed\n")
            self.assertTrue(outcome.diff_summary)
            self.assertIn("sample.txt", outcome.diff_summary[0])

    def test_skips_when_failure_type_is_not_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target = workspace / "sample.txt"
            target.write_text("value = buggy\n", encoding="utf-8")

            strategy = RegexReplaceStrategy(
                name="replace-buggy",
                target_file="sample.txt",
                pattern=r"buggy",
                replacement="fixed",
                allowed_failures={FailureType.TEST_FAILURE},
            )
            context = FailureContext(
                command_result=CommandResult("cmd", 1, "", "error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertEqual(target.read_text(encoding="utf-8"), "value = buggy\n")

    def test_blocks_out_of_workspace_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workspace = root / "repo"
            workspace.mkdir()
            outside_file = root / "outside.txt"
            outside_file.write_text("value = buggy\n", encoding="utf-8")

            strategy = RegexReplaceStrategy(
                name="escape-attempt",
                target_file="../outside.txt",
                pattern=r"buggy",
                replacement="fixed",
            )
            context = FailureContext(
                command_result=CommandResult("cmd", 1, "", "error"),
                failure_type=FailureType.RUNTIME_EXCEPTION,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("outside workspace", outcome.note)
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "value = buggy\n")

    def test_rejects_invalid_regex_pattern_at_init(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid regex pattern"):
            RegexReplaceStrategy(
                name="bad-pattern",
                target_file="sample.txt",
                pattern=r"(unclosed",
                replacement="fixed",
            )


class RepoRegexReplaceStrategyTests(unittest.TestCase):
    def test_applies_replacement_across_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            a_file = workspace / "a.py"
            b_file = workspace / "b.py"
            ignored_dir = workspace / "__pycache__"
            ignored_dir.mkdir()
            ignored_file = ignored_dir / "c.py"

            a_file.write_text("value = buggy\n", encoding="utf-8")
            b_file.write_text("other = buggy\n", encoding="utf-8")
            ignored_file.write_text("cache = buggy\n", encoding="utf-8")

            strategy = RepoRegexReplaceStrategy(
                name="repo-fix",
                pattern=r"buggy",
                replacement="fixed",
            )
            context = FailureContext(
                command_result=CommandResult("cmd", 1, "", "error"),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            self.assertEqual(a_file.read_text(encoding="utf-8"), "value = fixed\n")
            self.assertEqual(b_file.read_text(encoding="utf-8"), "other = fixed\n")
            self.assertEqual(ignored_file.read_text(encoding="utf-8"), "cache = buggy\n")
            self.assertEqual(len(outcome.changed_files), 2)
            self.assertTrue(outcome.diff_summary)

    def test_rejects_invalid_regex_pattern_at_init(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid regex pattern"):
            RepoRegexReplaceStrategy(
                name="bad-repo-pattern",
                pattern=r"(unclosed",
                replacement="fixed",
            )

    def test_logs_each_modified_file_in_repo_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            a_file = workspace / "a.py"
            b_file = workspace / "b.py"
            a_file.write_text("value = buggy\n", encoding="utf-8")
            b_file.write_text("other = buggy\n", encoding="utf-8")

            strategy = RepoRegexReplaceStrategy(
                name="repo-fix",
                pattern=r"buggy",
                replacement="fixed",
            )
            context = FailureContext(
                command_result=CommandResult("cmd", 1, "", "error"),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            with self.assertLogs("senior_agent.strategies", level="INFO") as captured:
                outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            changed_logs = [
                entry for entry in captured.output if "Repo regex changed file" in entry
            ]
            self.assertGreaterEqual(len(changed_logs), 2)

    def test_skips_unwritable_file_and_continues_other_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            blocked_file = workspace / "a.py"
            writable_file = workspace / "b.py"
            blocked_file.write_text("value = buggy\n", encoding="utf-8")
            writable_file.write_text("other = buggy\n", encoding="utf-8")

            strategy = RepoRegexReplaceStrategy(
                name="repo-fix",
                pattern=r"buggy",
                replacement="fixed",
            )
            context = FailureContext(
                command_result=CommandResult("cmd", 1, "", "error"),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            original_write_text = Path.write_text

            def patched_write_text(path_obj: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
                if path_obj.resolve() == blocked_file.resolve():
                    raise PermissionError("permission denied")
                return original_write_text(path_obj, *args, **kwargs)

            with mock.patch("pathlib.Path.write_text", autospec=True, side_effect=patched_write_text):
                outcome = strategy.apply(context)

            self.assertTrue(outcome.applied)
            self.assertIn("Skipped 1 file(s) due to I/O or permission errors.", outcome.note)
            resolved_changed_files = {path.resolve() for path in outcome.changed_files}
            self.assertNotIn(blocked_file.resolve(), resolved_changed_files)
            self.assertIn(writable_file.resolve(), resolved_changed_files)
            self.assertEqual(blocked_file.read_text(encoding="utf-8"), "value = buggy\n")
            self.assertEqual(writable_file.read_text(encoding="utf-8"), "other = fixed\n")
            self.assertTrue(
                any(
                    "Skipped 1 file(s) due to I/O or permission errors." in line
                    for line in outcome.diff_summary
                )
            )

    def test_reports_io_skip_when_no_replacements_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "a.py"
            target_file.write_text("value = buggy\n", encoding="utf-8")

            strategy = RepoRegexReplaceStrategy(
                name="repo-fix",
                pattern=r"buggy",
                replacement="fixed",
            )
            context = FailureContext(
                command_result=CommandResult("cmd", 1, "", "error"),
                failure_type=FailureType.TEST_FAILURE,
                workspace=workspace,
                attempt_number=1,
            )

            original_read_text = Path.read_text

            def patched_read_text(path_obj: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
                if path_obj.resolve() == target_file.resolve():
                    raise PermissionError("permission denied")
                return original_read_text(path_obj, *args, **kwargs)

            with mock.patch("pathlib.Path.read_text", autospec=True, side_effect=patched_read_text):
                outcome = strategy.apply(context)

            self.assertFalse(outcome.applied)
            self.assertIn("No matching patterns found in repository scope.", outcome.note)
            self.assertIn("Skipped 1 file(s) due to I/O or permission errors.", outcome.note)


if __name__ == "__main__":
    unittest.main()
