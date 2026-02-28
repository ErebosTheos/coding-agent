from __future__ import annotations
import asyncio
import json
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, AsyncIterator
from .protocol import LLMClient, LLMError, LLMTimeoutError


@dataclass(frozen=True)
class CodexCLIClient(LLMClient):
    """LLM client that drives the local `codex` CLI in non-interactive mode.

    Uses `codex exec --full-auto --ephemeral` for headless execution.
    The agent's final reply is captured via `-o FILE` so internal tool calls
    (file reads, shell commands) don't pollute the returned text.
    """
    model: Optional[str] = None
    workspace: str | Path = "."
    timeout_seconds: int = 300   # codex can be slow on first run
    binary: str = "codex"

    @staticmethod
    def _stream_timeouts_from_env() -> tuple[int, int]:
        """Return (idle_timeout_s, max_timeout_s) for streaming calls.

        - Idle timeout defaults to CODEGEN_LLM_TIMEOUT (existing behavior signal).
        - Max timeout prevents infinite runs while still allowing large prompts.
        """
        idle_timeout = int(
            os.environ.get(
                "CODEGEN_LLM_STREAM_IDLE_TIMEOUT",
                os.environ.get("CODEGEN_LLM_TIMEOUT", "120"),
            )
        )
        max_timeout = int(
            os.environ.get(
                "CODEGEN_LLM_STREAM_MAX_TIMEOUT",
                str(max(idle_timeout * 6, 600)),
            )
        )
        return idle_timeout, max_timeout

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process is None:
            return
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            await process.wait()
        except Exception:
            pass

    async def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        timeout = int(os.environ.get("CODEGEN_LLM_TIMEOUT", "120"))
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        workspace_path = str(Path(self.workspace).resolve())
        process: asyncio.subprocess.Process | None = None

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="codex_out_") as f:
            output_file = f.name

        command = [
            self.binary, "exec",
            "--full-auto",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "-o", output_file,
            "-C", workspace_path,
        ]
        if self.model:
            command.extend(["-m", self.model])
        command.append(full_prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await self._terminate_process(process)
                raise LLMTimeoutError(f"Codex CLI timed out after {timeout}s")

            if process.returncode != 0:
                raise LLMError(
                    f"Codex CLI failed (exit {process.returncode}): "
                    f"{(stdout.decode() + stderr.decode()).strip()[:500]}"
                )

            content = Path(output_file).read_text().strip()
            if not content:
                raise LLMError("Codex CLI returned an empty response.")
            return content

        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        except (LLMError, LLMTimeoutError):
            raise
        except Exception as exc:
            raise LLMError(f"Codex CLI execution failed: {exc}") from exc
        finally:
            try:
                os.unlink(output_file)
            except OSError:
                pass

    async def astream(self, prompt: str, system_prompt: Optional[str] = None) -> AsyncIterator[str]:
        """Stream via codex exec --json, yielding assistant text from JSONL events."""
        idle_timeout, max_timeout = self._stream_timeouts_from_env()
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        workspace_path = str(Path(self.workspace).resolve())
        process: asyncio.subprocess.Process | None = None

        command = [
            self.binary, "exec",
            "--full-auto",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--json",               # JSONL events on stdout
            "-C", workspace_path,
        ]
        if self.model:
            command.extend(["-m", self.model])
        command.append(full_prompt)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdout is not None
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            last_activity = started_at
            yielded_any = False

            while True:
                now = loop.time()
                if now - started_at > max_timeout:
                    await self._terminate_process(process)
                    raise LLMTimeoutError(
                        f"Codex CLI stream timed out after {max_timeout}s (max timeout)."
                    )
                remaining_idle = idle_timeout - (now - last_activity)
                if remaining_idle <= 0:
                    await self._terminate_process(process)
                    raise LLMTimeoutError(f"Codex CLI stream timed out after {idle_timeout}s")
                try:
                    raw_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=remaining_idle,
                    )
                except asyncio.TimeoutError:
                    await self._terminate_process(process)
                    raise LLMTimeoutError(f"Codex CLI stream timed out after {idle_timeout}s")
                if raw_line == b"":
                    break
                last_activity = loop.time()
                line = raw_line.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                text = self._extract_stream_text(event)
                if text:
                    yielded_any = True
                    yield text

            await process.wait()
            stderr_data = await process.stderr.read() if process.stderr else b""
            if process.returncode not in (0, None):
                raise LLMError(
                    f"Codex CLI failed (exit {process.returncode}): "
                    f"{stderr_data.decode().strip()[:300]}"
                )
            if not yielded_any:
                stderr_msg = stderr_data.decode().strip()
                detail = f" stderr: {stderr_msg[:300]}" if stderr_msg else ""
                raise LLMError(f"Codex CLI stream returned no assistant text.{detail}")

        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        except (LLMTimeoutError, LLMError):
            raise
        except Exception as exc:
            raise LLMError(f"Codex CLI stream failed: {exc}") from exc

    @staticmethod
    def _extract_stream_text(event: dict) -> Optional[str]:
        """Extract assistant text from known Codex JSON stream event shapes."""
        # Legacy/top-level shape:
        # {"type":"agent_message","role":"assistant","content":"..."}
        if event.get("type") == "agent_message":
            role = event.get("role")
            if role in (None, "assistant"):
                for key in ("content", "text"):
                    value = event.get(key)
                    if isinstance(value, str) and value:
                        return value

        # Current codex CLI shape:
        # {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
        item = event.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    return text

                content = item.get("content")
                if isinstance(content, str) and content:
                    return content
                if isinstance(content, list):
                    parts: list[str] = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        part_text = part.get("text")
                        if isinstance(part_text, str) and part_text:
                            parts.append(part_text)
                    if parts:
                        return "".join(parts)

            # Defensive support for possible incremental delta formats.
            if item_type in {"agent_message_delta", "output_text_delta"}:
                delta = item.get("delta") or item.get("text")
                if isinstance(delta, str) and delta:
                    return delta

        # Additional defensive support for output_text.delta event families.
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type.endswith("output_text.delta"):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                return delta

        return None
