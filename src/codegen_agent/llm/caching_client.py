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
        char_counter=None,
    ):
        self._client = client
        self._cache = cache
        self._provider = provider
        self._model = model
        self._counter = char_counter

    def _track(self, prompt: str, response: str) -> None:
        if self._counter is not None:
            self._counter.total_prompt_chars += len(prompt)
            self._counter.total_response_chars += len(response)

    def _cache_key(self, prompt: str, system_prompt: str) -> str:
        return f"{system_prompt}\n---\n{prompt}" if system_prompt else prompt

    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        key = self._cache_key(prompt, system_prompt)
        cached = self._cache.get(key, self._provider, self._model)
        if cached is not None:
            return cached
        response = await self._client.generate(prompt, system_prompt=system_prompt)
        self._cache.set(key, self._provider, self._model, response)
        self._track(prompt, response)
        return response

    async def astream(self, prompt: str, system_prompt: str = "") -> AsyncIterator[str]:
        key = self._cache_key(prompt, system_prompt)
        cached = self._cache.get(key, self._provider, self._model)
        if cached is not None:
            yield cached
            return
        chunks: list[str] = []
        async for chunk in self._client.astream(prompt, system_prompt=system_prompt):
            chunks.append(chunk)
            yield chunk
        if chunks:
            self._cache.set(key, self._provider, self._model, "".join(chunks))
