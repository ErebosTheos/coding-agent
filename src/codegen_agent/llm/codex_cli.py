from __future__ import annotations
import asyncio
import json
import os
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

    async def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        return await asyncio.to_thread(self._generate_sync, prompt, system_prompt)

    def _generate_sync(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        import subprocess

        # Prepend system prompt as plain text — codex exec has no --system-prompt flag
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

        workspace_path = str(Path(self.workspace).resolve())

        # Write the last agent message to a temp file so we get clean text output
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="codex_out_"
        ) as f:
            output_file = f.name

        command = [
            self.binary, "exec",
            "--full-auto",          # auto-approve, no interactive prompts
            "--ephemeral",          # don't persist session to disk
            "--skip-git-repo-check",
            "--sandbox", "read-only",  # allow reading workspace; block writes (we write files)
            "-o", output_file,      # capture final reply here
            "-C", workspace_path,
        ]
        if self.model:
            command.extend(["-m", self.model])
        command.append(full_prompt)

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            if result.returncode != 0:
                raise LLMError(
                    f"Codex CLI failed (exit {result.returncode}): "
                    f"{(result.stdout + result.stderr).strip()[:500]}"
                )

            content = Path(output_file).read_text().strip()
            if not content:
                raise LLMError("Codex CLI returned an empty response.")
            return content

        except subprocess.TimeoutExpired:
            raise LLMTimeoutError(f"Codex CLI timed out after {self.timeout_seconds}s")
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
        """Stream via codex exec --json, yielding text from agent_message events."""
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        workspace_path = str(Path(self.workspace).resolve())

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
            )
            assert process.stdout is not None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout_seconds

            async for raw_line in process.stdout:
                if loop.time() > deadline:
                    process.kill()
                    raise LLMTimeoutError(f"Codex CLI stream timed out after {self.timeout_seconds}s")

                line = raw_line.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Yield text only from the final agent reply
                if event.get("type") == "agent_message" and event.get("role") == "assistant":
                    content = event.get("content", "")
                    if content:
                        yield content

            await process.wait()
            if process.returncode not in (0, None):
                stderr_data = await process.stderr.read() if process.stderr else b""
                raise LLMError(
                    f"Codex CLI failed (exit {process.returncode}): "
                    f"{stderr_data.decode().strip()[:300]}"
                )

        except (LLMTimeoutError, LLMError):
            raise
        except Exception as exc:
            raise LLMError(f"Codex CLI stream failed: {exc}") from exc
