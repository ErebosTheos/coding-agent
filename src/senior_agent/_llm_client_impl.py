from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Callable, Iterable, Iterator, Mapping, Protocol, Sequence


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


DEFAULT_TRANSPORT_SYSTEM_PROMPT = (
    "You are the Senior Developer Agent for this repository. "
    "Return only the artifact requested by the user prompt."
)
DEFAULT_TRANSPORT_SAFETY_PROMPT = (
    "- Keep edits scoped to repository files only.\n"
    "- Preserve behavior outside the requested fix scope.\n"
    "- Do not include destructive shell commands.\n"
    "- Do not include markdown fences unless explicitly requested."
)
_FENCED_CODE_PATTERN = re.compile(
    r"```(?:[a-zA-Z0-9_+-]+)?\n(?P<code>[\s\S]*?)```",
    re.MULTILINE,
)
_TRANSPORT_PROMPT_TEMPLATE = (
    "<<SYSTEM>>\n"
    "{system_prompt}\n"
    "<</SYSTEM>>\n\n"
    "<<SAFETY>>\n"
    "{safety_prompt}\n"
    "<</SAFETY>>\n\n"
    "<<USER_PROMPT>>\n"
    "{user_prompt}\n"
    "<</USER_PROMPT>>"
)


def build_transport_prompt(
    prompt: str,
    *,
    system_prompt: str = DEFAULT_TRANSPORT_SYSTEM_PROMPT,
    safety_prompt: str = DEFAULT_TRANSPORT_SAFETY_PROMPT,
) -> str:
    user_prompt = prompt.strip()
    if not user_prompt:
        raise ValueError("prompt must not be empty.")

    normalized_system = (
        system_prompt.strip() if system_prompt.strip() else DEFAULT_TRANSPORT_SYSTEM_PROMPT
    )
    normalized_safety = (
        safety_prompt.strip() if safety_prompt.strip() else DEFAULT_TRANSPORT_SAFETY_PROMPT
    )
    return _TRANSPORT_PROMPT_TEMPLATE.format(
        system_prompt=normalized_system,
        safety_prompt=normalized_safety,
        user_prompt=user_prompt,
    )


@dataclass
class SpeculativeResponseParser:
    """Incrementally parse streamed model output into best-effort code content."""

    _chunks: list[str] = field(default_factory=list)
    _latest_candidate: str | None = None

    def ingest(self, fragment: str) -> str | None:
        if not fragment:
            return self._latest_candidate
        self._chunks.append(fragment)
        combined = "".join(self._chunks)
        candidate = self._extract_candidate(combined)
        if candidate:
            self._latest_candidate = candidate
        return self._latest_candidate

    def finalize(self) -> str:
        if self._latest_candidate:
            return self._latest_candidate
        combined = "".join(self._chunks).strip()
        if not combined:
            raise LLMClientError("LLM stream returned no usable content.")
        return combined

    @staticmethod
    def _extract_candidate(text: str) -> str | None:
        matches = list(_FENCED_CODE_PATTERN.finditer(text))
        if matches:
            fenced = matches[-1].group("code")
            if fenced.strip():
                return fenced

        stripped = text.strip()
        if not stripped:
            return None
        return stripped


def parse_streamed_response(
    fragments: Iterable[str],
    *,
    on_fragment: Callable[[str], None] | None = None,
) -> str:
    parser = SpeculativeResponseParser()
    saw_fragment = False
    for fragment in fragments:
        saw_fragment = True
        text_fragment = str(fragment)
        if on_fragment is not None:
            on_fragment(text_fragment)
        parser.ingest(text_fragment)
    if not saw_fragment:
        raise LLMClientError("LLM stream returned no fragments.")
    return parser.finalize()


def _stream_or_generate(
    client: LLMClient,
    prompt: str,
    *,
    prefer_streaming: bool,
    on_fragment: Callable[[str], None] | None = None,
) -> str:
    if not prefer_streaming:
        return client.generate_fix(prompt)

    stream_method = getattr(client, "stream_fix", None)
    if callable(stream_method):
        stream_output = stream_method(prompt)
        if isinstance(stream_output, str):
            fragments: Iterable[str] = (stream_output,)
        else:
            fragments = stream_output
        return parse_streamed_response(fragments, on_fragment=on_fragment)

    output = client.generate_fix(prompt)
    if on_fragment is not None:
        on_fragment(output)
    return output


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
    system_prompt: str = DEFAULT_TRANSPORT_SYSTEM_PROMPT
    safety_prompt: str = DEFAULT_TRANSPORT_SAFETY_PROMPT
    binary: str = "codex"
    runner: CommandRunner = _default_runner

    def generate_fix(self, prompt: str) -> str:
        """Generate a fix using the Codex CLI binary."""
        transport_prompt = build_transport_prompt(
            prompt,
            system_prompt=self.system_prompt,
            safety_prompt=self.safety_prompt,
        )
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
            stdin_data=transport_prompt,
            cwd=workspace_path,
            env=env,
            timeout_seconds=self.timeout_seconds,
            timeout_message="Codex CLI timed out while generating a fix.",
            rate_limit_message="Codex CLI request hit a rate or quota limit.",
            failure_label="Codex CLI",
            empty_response_message="Codex CLI returned an empty response.",
            output_file=temp_output_path,
        )

    def stream_fix(self, prompt: str) -> Iterator[str]:
        yield self.generate_fix(prompt)

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
    system_prompt: str = DEFAULT_TRANSPORT_SYSTEM_PROMPT
    safety_prompt: str = DEFAULT_TRANSPORT_SAFETY_PROMPT
    binary: str = "gemini"
    runner: CommandRunner = _default_runner

    def generate_fix(self, prompt: str) -> str:
        """Generate a fix using the Gemini CLI binary."""
        transport_prompt = build_transport_prompt(
            prompt,
            system_prompt=self.system_prompt,
            safety_prompt=self.safety_prompt,
        )
        if len(transport_prompt) > self.max_prompt_chars:
            raise LLMClientError(
                "Gemini CLI prompt is too large for safe command-line transport: "
                f"{len(transport_prompt)} chars exceeds limit {self.max_prompt_chars}."
            )
        workspace_path = Path(self.workspace).resolve()
        env = _build_env(self.api_key, "GEMINI_API_KEY")

        command: list[str] = [
            self.binary,
            "--prompt",
            transport_prompt,
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

    def stream_fix(self, prompt: str) -> Iterator[str]:
        yield self.generate_fix(prompt)

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")
        if self.max_prompt_chars < 1:
            raise ValueError("max_prompt_chars must be >= 1")


@dataclass(frozen=True)
class LocalOffloadClient:
    """Local LLM offload client (for Ollama-style local inference)."""

    model: str = "deepseek-coder:latest"
    workspace: str | Path = "."
    timeout_seconds: int = 60
    system_prompt: str = DEFAULT_TRANSPORT_SYSTEM_PROMPT
    safety_prompt: str = DEFAULT_TRANSPORT_SAFETY_PROMPT
    binary: str = "ollama"
    runner: CommandRunner = _default_runner

    def generate_fix(self, prompt: str) -> str:
        transport_prompt = build_transport_prompt(
            prompt,
            system_prompt=self.system_prompt,
            safety_prompt=self.safety_prompt,
        )
        workspace_path = Path(self.workspace).resolve()
        command = [self.binary, "run", self.model]
        return _run_cli_request(
            runner=self.runner,
            command=command,
            stdin_data=transport_prompt,
            cwd=workspace_path,
            env=dict(os.environ),
            timeout_seconds=self.timeout_seconds,
            timeout_message="Local offload model timed out while generating a fix.",
            rate_limit_message="Local offload model reported a quota/rate-style error.",
            failure_label="Local offload model",
            empty_response_message="Local offload model returned an empty response.",
        )

    def stream_fix(self, prompt: str) -> Iterator[str]:
        yield self.generate_fix(prompt)

    def __post_init__(self) -> None:
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be >= 1")
        if not self.model.strip():
            raise ValueError("model must not be empty")


@dataclass
class MultiCloudRouter:
    """Rotate cloud providers and optionally route low-complexity prompts to local LLM."""

    cloud_clients: tuple[LLMClient, ...]
    local_client: LLMClient | None = None
    local_complexity_threshold: int = 3
    cloud_speculative_threshold: int = 4
    enable_speculative_racing: bool = True
    max_race_clients: int = 2
    race_timeout_seconds: float = 30.0
    local_latency_fallback_seconds: float = 30.0
    session_budget_usd: float = 2.0
    estimated_cloud_request_cost_usd: float = 0.02
    budget_guard_band_requests: int = 3
    max_cloud_failures_before_local_downgrade: int = 3

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _round_robin_index: int = 0
    _session_cost_usd: float = 0.0
    _cloud_failures: int = 0
    _force_local_mode: bool = False

    def __post_init__(self) -> None:
        if not self.cloud_clients:
            raise ValueError("cloud_clients must contain at least one client.")
        if self.local_complexity_threshold < 1:
            raise ValueError("local_complexity_threshold must be >= 1")
        if self.cloud_speculative_threshold < 1:
            raise ValueError("cloud_speculative_threshold must be >= 1")
        if self.max_race_clients < 1:
            raise ValueError("max_race_clients must be >= 1")
        if self.race_timeout_seconds <= 0:
            raise ValueError("race_timeout_seconds must be > 0")
        if self.local_latency_fallback_seconds <= 0:
            raise ValueError("local_latency_fallback_seconds must be > 0")
        if self.session_budget_usd <= 0:
            raise ValueError("session_budget_usd must be > 0")
        if self.estimated_cloud_request_cost_usd < 0:
            raise ValueError("estimated_cloud_request_cost_usd must be >= 0")
        if self.budget_guard_band_requests < 1:
            raise ValueError("budget_guard_band_requests must be >= 1")
        if self.max_cloud_failures_before_local_downgrade < 1:
            raise ValueError("max_cloud_failures_before_local_downgrade must be >= 1")

    def generate_fix(self, prompt: str) -> str:
        return self._generate_with_mode(prompt, prefer_streaming=False)

    def generate_fix_stream(
        self,
        prompt: str,
        *,
        on_fragment: Callable[[str], None] | None = None,
    ) -> str:
        return self._generate_with_mode(
            prompt,
            prefer_streaming=True,
            on_fragment=on_fragment,
        )

    async def generate_fix_stream_async(
        self,
        prompt: str,
        *,
        on_fragment: Callable[[str], None] | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self.generate_fix_stream,
            prompt,
            on_fragment=on_fragment,
        )

    def stream_fix(self, prompt: str) -> Iterator[str]:
        yield self.generate_fix_stream(prompt)

    def _generate_with_mode(
        self,
        prompt: str,
        *,
        prefer_streaming: bool,
        on_fragment: Callable[[str], None] | None = None,
    ) -> str:
        complexity = self.estimate_prompt_complexity(prompt)
        if self.local_client is not None and (
            self._is_force_local_mode() or complexity <= self.local_complexity_threshold
        ):
            local_start = time.monotonic()
            try:
                local_response = _stream_or_generate(
                    self.local_client,
                    prompt,
                    prefer_streaming=prefer_streaming,
                    on_fragment=on_fragment,
                )
            except Exception:
                local_response = None
            else:
                elapsed = time.monotonic() - local_start
                if elapsed <= self.local_latency_fallback_seconds or self._is_force_local_mode():
                    return local_response

        if self._would_exceed_budget() and self.local_client is not None:
            return _stream_or_generate(
                self.local_client,
                prompt,
                prefer_streaming=prefer_streaming,
                on_fragment=on_fragment,
            )
        if self._would_exceed_budget():
            raise LLMClientError(
                "Economic circuit breaker opened: cloud budget threshold reached."
            )
        self._apply_budget_guard_band()

        cloud_clients = self._ordered_cloud_clients()
        if (
            self.enable_speculative_racing
            and len(cloud_clients) > 1
            and complexity >= self.cloud_speculative_threshold
        ):
            raced_clients = cloud_clients[: min(self.max_race_clients, len(cloud_clients))]
            raced_response, raced_errors = self._run_cloud_race(
                raced_clients,
                prompt,
                prefer_streaming=prefer_streaming,
            )
            if raced_response is not None:
                self._record_cloud_success()
                self._increment_session_cost(
                    self.estimated_cloud_request_cost_usd * len(raced_clients)
                )
                return raced_response
            for _ in raced_errors:
                self._increment_cloud_failures()
            cloud_errors = list(raced_errors)
            fallback_clients = cloud_clients[min(self.max_race_clients, len(cloud_clients)) :]
            for client in fallback_clients:
                try:
                    response = _stream_or_generate(
                        client,
                        prompt,
                        prefer_streaming=prefer_streaming,
                        on_fragment=on_fragment,
                    )
                except Exception as exc:
                    cloud_errors.append(str(exc))
                    self._increment_cloud_failures()
                    continue
                self._record_cloud_success()
                self._increment_session_cost(self.estimated_cloud_request_cost_usd)
                return response
            if self.local_client is not None:
                return _stream_or_generate(
                    self.local_client,
                    prompt,
                    prefer_streaming=prefer_streaming,
                    on_fragment=on_fragment,
                )
            joined = " | ".join(cloud_errors) if cloud_errors else "all cloud clients failed."
            raise LLMClientError(f"MultiCloudRouter failed across providers: {joined}")

        cloud_errors: list[str] = []
        for client in cloud_clients:
            try:
                response = _stream_or_generate(
                    client,
                    prompt,
                    prefer_streaming=prefer_streaming,
                    on_fragment=on_fragment,
                )
            except Exception as exc:
                cloud_errors.append(str(exc))
                self._increment_cloud_failures()
                continue
            self._record_cloud_success()
            self._increment_session_cost(self.estimated_cloud_request_cost_usd)
            return response

        if self.local_client is not None:
            return _stream_or_generate(
                self.local_client,
                prompt,
                prefer_streaming=prefer_streaming,
                on_fragment=on_fragment,
            )
        joined = " | ".join(cloud_errors) if cloud_errors else "all cloud clients failed."
        raise LLMClientError(f"MultiCloudRouter failed across providers: {joined}")

    @property
    def session_cost_usd(self) -> float:
        with self._lock:
            return self._session_cost_usd

    @staticmethod
    def estimate_prompt_complexity(prompt: str) -> int:
        score = 1
        prompt_length = len(prompt)
        if prompt_length > 2000:
            score += 1
        if prompt_length > 8000:
            score += 1
        if prompt_length > 16000:
            score += 1
        weighted_keywords = (
            "architecture",
            "dependency graph",
            "orchestrator",
            "distributed",
            "performance",
            "security",
            "cross-file",
        )
        lower = prompt.lower()
        if any(keyword in lower for keyword in weighted_keywords):
            score += 1
        return score

    def _ordered_cloud_clients(self) -> tuple[LLMClient, ...]:
        with self._lock:
            size = len(self.cloud_clients)
            start = self._round_robin_index % size
            ordered = self.cloud_clients[start:] + self.cloud_clients[:start]
            self._round_robin_index = (self._round_robin_index + 1) % size
            return ordered

    def _run_cloud_race(
        self,
        clients: Sequence[LLMClient],
        prompt: str,
        *,
        prefer_streaming: bool = False,
    ) -> tuple[str | None, list[str]]:
        if not clients:
            return None, ["No cloud clients were available for speculative race."]
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=len(clients)) as executor:
            futures = {
                executor.submit(
                    _stream_or_generate,
                    client,
                    prompt,
                    prefer_streaming=prefer_streaming,
                ): client
                for client in clients
            }
            try:
                for future in as_completed(futures, timeout=self.race_timeout_seconds):
                    try:
                        output = future.result()
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc))
                        continue
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    return output, errors
            except FuturesTimeoutError:
                errors.append(
                    "Speculative race timed out before any cloud provider returned a response."
                )
            finally:
                for pending in futures:
                    pending.cancel()
        return None, errors

    def _is_force_local_mode(self) -> bool:
        with self._lock:
            return self._force_local_mode

    def _would_exceed_budget(self) -> bool:
        with self._lock:
            return (
                self._session_cost_usd + self.estimated_cloud_request_cost_usd
                > self.session_budget_usd
            )

    def _apply_budget_guard_band(self) -> None:
        with self._lock:
            remaining = self.session_budget_usd - self._session_cost_usd
            threshold = self.estimated_cloud_request_cost_usd * self.budget_guard_band_requests
            if (
                self.local_client is not None
                and remaining <= threshold
            ):
                self._force_local_mode = True

    def _increment_session_cost(self, amount: float) -> None:
        with self._lock:
            self._session_cost_usd += max(0.0, amount)

    def _increment_cloud_failures(self) -> None:
        with self._lock:
            self._cloud_failures += 1
            if (
                self.local_client is not None
                and self._cloud_failures >= self.max_cloud_failures_before_local_downgrade
            ):
                self._force_local_mode = True

    def _record_cloud_success(self) -> None:
        with self._lock:
            self._cloud_failures = 0
