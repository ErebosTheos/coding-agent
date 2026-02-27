from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, AsyncIterator
from .protocol import LLMClient, LLMError, LLMTimeoutError

DEFAULT_TRANSPORT_SYSTEM_PROMPT = (
    "You are the Senior Developer Agent for this repository. "
    "Return only the artifact requested by the user prompt."
)
DEFAULT_TRANSPORT_SAFETY_PROMPT = (
    "- Keep edits scoped to repository files only.\n"
    "- Preserve behavior outside the requested fix scope.\n"
    "- Do not include destructive shell commands.\n"
    "- Do not include markdown fences unless explicitly requested.\n"
    "- Do NOT use file-reading tools, search tools, or any other tools. Use only the context in this prompt.\n"
    "- Output ONLY the requested artifact. No reasoning, no 'I will...' text, no explanations."
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

import asyncio

@dataclass(frozen=True)
class GeminiCLIClient(LLMClient):
    api_key: Optional[str] = None
    model: Optional[str] = None
    workspace: str | Path = "."
    timeout_seconds: int = 180
    max_prompt_chars: int = 32000
    system_prompt: str = DEFAULT_TRANSPORT_SYSTEM_PROMPT
    safety_prompt: str = DEFAULT_TRANSPORT_SAFETY_PROMPT
    binary: str = "gemini"

    async def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Generate a response using the Gemini CLI binary."""
        transport_prompt = build_transport_prompt(
            prompt,
            system_prompt=system_prompt if system_prompt else self.system_prompt,
            safety_prompt=self.safety_prompt,
        )
        if len(transport_prompt) > self.max_prompt_chars:
            raise LLMError(
                "Gemini CLI prompt is too large for safe command-line transport: "
                f"{len(transport_prompt)} chars exceeds limit {self.max_prompt_chars}."
            )
        
        workspace_path = Path(self.workspace).resolve()
        env = dict(os.environ)
        if self.api_key:
            env["GEMINI_API_KEY"] = self.api_key

        command: list[str] = [
            self.binary,
            "--prompt",
            transport_prompt,
            "--output-format",
            "text",
        ]
        if self.model:
            command.extend(["--model", self.model])

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
                    timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError:
                process.kill()
                raise LLMTimeoutError(f"Gemini CLI timed out after {self.timeout_seconds}s")
            
            stdout_str = stdout.decode().strip()
            stderr_str = stderr.decode().strip()
            combined = f"{stdout_str}\n{stderr_str}".strip()
            
            if process.returncode != 0:
                raise LLMError(
                    f"Gemini CLI failed with exit code {process.returncode}: {combined}"
                )

            if not stdout_str:
                raise LLMError("Gemini CLI returned an empty response.")
            return stdout_str
        except LLMTimeoutError:
            raise
        except Exception as exc:
            if not isinstance(exc, LLMError):
                raise LLMError(f"Gemini CLI execution failed: {str(exc)}") from exc
            raise

    async def astream(self, prompt: str, system_prompt: Optional[str] = None) -> AsyncIterator[str]:
        """Stream Gemini CLI stdout incrementally, yielding 4 KB chunks as they arrive."""
        transport_prompt = build_transport_prompt(
            prompt,
            system_prompt=system_prompt if system_prompt else self.system_prompt,
            safety_prompt=self.safety_prompt,
        )
        if len(transport_prompt) > self.max_prompt_chars:
            raise LLMError(
                f"Gemini CLI prompt is too large: {len(transport_prompt)} chars "
                f"exceeds limit {self.max_prompt_chars}."
            )

        workspace_path = Path(self.workspace).resolve()
        env = dict(os.environ)
        if self.api_key:
            env["GEMINI_API_KEY"] = self.api_key

        command: list[str] = [self.binary, "--prompt", transport_prompt, "--output-format", "text"]
        if self.model:
            command.extend(["--model", self.model])

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout_seconds

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    process.kill()
                    raise LLMTimeoutError(f"Gemini CLI stream timed out after {self.timeout_seconds}s")
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=remaining)
                except asyncio.TimeoutError:
                    process.kill()
                    raise LLMTimeoutError(f"Gemini CLI stream timed out after {self.timeout_seconds}s")
                if not chunk:
                    break
                yield chunk.decode()

            await process.wait()
            if process.returncode != 0:
                stderr_data = await process.stderr.read() if process.stderr else b""
                raise LLMError(
                    f"Gemini CLI failed with exit code {process.returncode}: "
                    f"{stderr_data.decode().strip()}"
                )

        except (LLMTimeoutError, LLMError):
            raise
        except Exception as exc:
            raise LLMError(f"Gemini CLI stream failed: {str(exc)}") from exc
