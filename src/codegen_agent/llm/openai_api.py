import asyncio
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional, AsyncIterator
from .protocol import LLMClient, LLMError


# Models served by the Responses API (/v1/responses) rather than Chat Completions
_RESPONSES_API_MODELS = {"codex-mini-latest", "codex-mini", "o3", "o4-mini"}


def _uses_responses_api(model: str) -> bool:
    return model in _RESPONSES_API_MODELS or model.startswith("codex")


@dataclass(frozen=True)
class OpenAIClient(LLMClient):
    api_key: Optional[str] = None
    model: str = "gpt-4o"
    timeout_seconds: int = 180
    max_tokens: int = 8192

    async def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        return await asyncio.to_thread(self._generate_sync, prompt, system_prompt)

    def _generate_sync(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        api_key = self.api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LLMError("OPENAI_API_KEY not set.")

        if _uses_responses_api(self.model):
            return self._responses_api(api_key, prompt, system_prompt)
        return self._chat_completions(api_key, prompt, system_prompt)

    def _responses_api(self, api_key: str, prompt: str, system_prompt: Optional[str]) -> str:
        """Codex / o-series models — OpenAI Responses API (/v1/responses)."""
        # Build the input: system instruction prepended as plain text if provided
        full_input = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt

        data = {
            "model": self.model,
            "input": full_input,
            "max_output_tokens": self.max_tokens,
        }

        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                # Responses API: result["output"][0]["content"][0]["text"]
                # Also available as result["output_text"] on recent versions
                if "output_text" in result:
                    return result["output_text"]
                return result["output"][0]["content"][0]["text"]
        except Exception as exc:
            raise LLMError(f"OpenAI Responses API request failed: {exc}") from exc

    def _chat_completions(self, api_key: str, prompt: str, system_prompt: Optional[str]) -> str:
        """Standard GPT models — Chat Completions API (/v1/chat/completions)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        data = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"]
        except Exception as exc:
            raise LLMError(f"OpenAI Chat Completions request failed: {exc}") from exc

    async def astream(self, prompt: str, system_prompt: Optional[str] = None) -> AsyncIterator[str]:
        """Single-chunk fallback — OpenAI streaming requires SSE which urllib doesn't handle easily."""
        yield await self.generate(prompt, system_prompt)
