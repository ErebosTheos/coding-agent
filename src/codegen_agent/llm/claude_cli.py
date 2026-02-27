from __future__ import annotations
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, AsyncIterator
from .protocol import LLMClient, LLMError, LLMTimeoutError


@dataclass(frozen=True)
class ClaudeCLIClient(LLMClient):
    """LLM client that drives the local `claude` (Claude Code) CLI binary.

    Uses `claude --print` for non-interactive, text-only generation.
    No file tools are exposed — output is plain text returned to the caller.
    """
    model: Optional[str] = None
    workspace: str | Path = "."
    timeout_seconds: int = 180
    binary: str = "claude"

    async def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        workspace_path = Path(self.workspace).resolve()

        command: list[str] = [
            self.binary,
            "--print",
            "--output-format", "text",
            "--no-session-persistence",
            "--dangerously-skip-permissions",  # avoids interactive permission prompts
        ]
        if self.model:
            command.extend(["--model", self.model])
        if system_prompt:
            command.extend(["--system-prompt", system_prompt])
        command.append(prompt)

        import os
        # Claude Code refuses to launch inside another Claude Code session unless
        # the CLAUDECODE env var is unset in the child process.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                process.kill()
                raise LLMTimeoutError(f"Claude CLI timed out after {self.timeout_seconds}s")

            stdout_str = stdout.decode().strip()
            stderr_str = stderr.decode().strip()

            if process.returncode != 0:
                raise LLMError(
                    f"Claude CLI failed (exit {process.returncode}): "
                    f"{stdout_str or stderr_str}"
                )
            if not stdout_str:
                raise LLMError("Claude CLI returned an empty response.")
            return stdout_str

        except LLMTimeoutError:
            raise
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"Claude CLI execution failed: {exc}") from exc

    async def astream(self, prompt: str, system_prompt: Optional[str] = None) -> AsyncIterator[str]:
        """Yield the complete response as a single chunk (Claude CLI buffers output)."""
        yield await self.generate(prompt, system_prompt)
