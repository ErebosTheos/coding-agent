from typing import AsyncIterator, Optional
from .protocol import LLMClient
from .cache import LLMCache


class CachingLLMClient:
    """Wraps an LLMClient and caches generate() responses.

    astream() is NOT cached — streaming responses are passed through directly.
    The cache key includes the system_prompt so different system contexts
    produce distinct entries.
    """

    def __init__(
        self,
        client: LLMClient,
        cache: LLMCache,
        provider: str,
        model: Optional[str],
    ):
        self._client = client
        self._cache = cache
        self._provider = provider
        self._model = model

    def _cache_key(self, prompt: str, system_prompt: str) -> str:
        return f"{system_prompt}\n---\n{prompt}" if system_prompt else prompt

    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        key = self._cache_key(prompt, system_prompt)
        cached = self._cache.get(key, self._provider, self._model)
        if cached is not None:
            return cached
        response = await self._client.generate(prompt, system_prompt=system_prompt)
        self._cache.set(key, self._provider, self._model, response)
        return response

    async def astream(self, prompt: str, system_prompt: str = "") -> AsyncIterator[str]:
        async for chunk in self._client.astream(prompt, system_prompt=system_prompt):
            yield chunk
