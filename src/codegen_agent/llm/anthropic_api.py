import asyncio
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional, AsyncIterator
from .protocol import LLMClient, LLMError

@dataclass(frozen=True)
class AnthropicAPIClient(LLMClient):
    api_key: Optional[str] = None
    model: str = "claude-sonnet-4-6"
    timeout_seconds: int = 180
    max_tokens: int = 8192

    async def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Generate a response using the Anthropic API via HTTP (truly async)."""
        return await asyncio.to_thread(self._generate_sync, prompt, system_prompt)

    def _generate_sync(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY not found.")

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        messages = [{"role": "user", "content": prompt}]
        data = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if system_prompt:
            data["system"] = system_prompt

        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result["content"][0]["text"]
        except Exception as exc:
            raise LLMError(f"Anthropic API request failed: {str(exc)}") from exc

    async def astream(self, prompt: str, system_prompt: Optional[str] = None) -> AsyncIterator[str]:
        """Yield the complete response as a single chunk (no true SSE streaming yet)."""
        yield await self.generate(prompt, system_prompt)
