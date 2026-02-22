from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


class LLMClient(Protocol):
    def generate_fix(self, prompt: str) -> str:
        """Return model-generated fix content for the provided prompt."""
        ...


@dataclass(frozen=True)
class CommandExecutionResult:
    return_code: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[
    [Sequence[str], str, Path, Mapping[str, str], int], CommandExecutionResult
]


class LLMClientError(RuntimeError):
    pass


class LLMTimeoutError(LLMClientError):
    pass


class LLMRateLimitError(LLMClientError):
    pass


def _default_runner(
    command: Sequence[str],
    stdin_data: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
) -> CommandExecutionResult:
    completed = subprocess.run(
        list(command),
        input=stdin_data,
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandExecutionResult(
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _is_rate_limit_error(text: str) -> bool:
    lower = text.lower()
    hints = (
        "rate limit",
        "too many requests",
        "resource exhausted",
        "quota exceeded",
        "429",
    )
    return any(hint in lower for hint in hints)


def _build_env(api_key: str | None, api_key_env_name: str) -> dict[str, str]:
    env = dict(os.environ)
    if api_key:
        env[api_key_env_name] = api_key
    return env


def _run_cli_request(
    *,
    runner: CommandRunner,
    command: Sequence[str],
    stdin_data: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: int,
    timeout_message: str,
    rate_limit_message: str,
    failure_label: str,
    empty_response_message: str,
    output_file: Path | None = None,
) -> str:
    try:
        result = runner(
            command=command,
            stdin_data=stdin_data,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )
        combined = f"{result.stdout}\n{result.stderr}".strip()
        if result.return_code != 0:
            if _is_rate_limit_error(combined):
                raise LLMRateLimitError(rate_limit_message)
            raise LLMClientError(
                f"{failure_label} failed with exit code {result.return_code}: {combined}"
            )

        candidate = ""
        if output_file and output_file.exists():
            candidate = output_file.read_text(encoding="utf-8").strip()
        if not candidate:
            candidate = result.stdout.strip()
        if not candidate:
            raise LLMClientError(empty_response_message)
        return candidate
    except subprocess.TimeoutExpired as exc:
        raise LLMTimeoutError(timeout_message) from exc
    finally:
        if output_file and output_file.exists():
            output_file.unlink(missing_ok=True)


@dataclass(frozen=True)
class CodexCLIClient:
    api_key: str | None = None
    model: str | None = None
    workspace: str | Path = "."
    timeout_seconds: int = 180
    binary: str = "codex"
    runner: CommandRunner = _default_runner

    def generate_fix(self, prompt: str) -> str:
        """Generate a fix using the Codex CLI binary."""
        workspace_path = Path(self.workspace).resolve()
        env = _build_env(self.api_key, "OPENAI_API_KEY")

        temp_output_path: Path | None = None
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            dir=str(workspace_path),
        ) as handle:
            temp_output_path = Path(handle.name)

        command: list[str] = [
            self.binary,
            "exec",
            "--cd",
            str(workspace_path),
            "--output-last-message",
            str(temp_output_path),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")

        return _run_cli_request(
            runner=self.runner,
            command=command,
            stdin_data=prompt,
            cwd=workspace_path,
            env=env,
            timeout_seconds=self.timeout_seconds,
            timeout_message="Codex CLI timed out while generating a fix.",
            rate_limit_message="Codex CLI request hit a rate or quota limit.",
            failure_label="Codex CLI",
            empty_response_message="Codex CLI returned an empty response.",
            output_file=temp_output_path,
        )

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")


@dataclass(frozen=True)
class GeminiCLIClient:
    api_key: str | None = None
    model: str | None = None
    workspace: str | Path = "."
    timeout_seconds: int = 180
    max_prompt_chars: int = 32000
    binary: str = "gemini"
    runner: CommandRunner = _default_runner

    def generate_fix(self, prompt: str) -> str:
        """Generate a fix using the Gemini CLI binary."""
        if len(prompt) > self.max_prompt_chars:
            raise LLMClientError(
                "Gemini CLI prompt is too large for safe command-line transport: "
                f"{len(prompt)} chars exceeds limit {self.max_prompt_chars}."
            )
        workspace_path = Path(self.workspace).resolve()
        env = _build_env(self.api_key, "GEMINI_API_KEY")

        command: list[str] = [
            self.binary,
            "--prompt",
            prompt,
            "--output-format",
            "text",
        ]
        if self.model:
            command.extend(["--model", self.model])

        return _run_cli_request(
            runner=self.runner,
            command=command,
            stdin_data="",
            cwd=workspace_path,
            env=env,
            timeout_seconds=self.timeout_seconds,
            timeout_message="Gemini CLI timed out while generating a fix.",
            rate_limit_message="Gemini CLI request hit a rate or quota limit.",
            failure_label="Gemini CLI",
            empty_response_message="Gemini CLI returned an empty response.",
        )

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")
        if self.max_prompt_chars < 1:
            raise ValueError("max_prompt_chars must be >= 1")
