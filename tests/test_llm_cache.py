import asyncio
import tempfile

from codegen_agent.llm.cache import LLMCache
from codegen_agent.llm.caching_client import CachingLLMClient


class _FakeClient:
    def __init__(self):
        self.calls = 0

    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        self.calls += 1
        return f"response:{system_prompt}:{prompt}"

    async def astream(self, prompt, system_prompt=""):
        yield await self.generate(prompt, system_prompt)
        return


def test_cache_miss_returns_none():
    with tempfile.TemporaryDirectory() as d:
        cache = LLMCache(d)
        assert cache.get("hello", "gemini_cli", None) is None


def test_cache_hit_after_set():
    with tempfile.TemporaryDirectory() as d:
        cache = LLMCache(d)
        cache.set("hello", "gemini_cli", None, "world")
        assert cache.get("hello", "gemini_cli", None) == "world"


def test_different_model_is_different_key():
    with tempfile.TemporaryDirectory() as d:
        cache = LLMCache(d)
        cache.set("hello", "gemini_cli", None, "world")
        assert cache.get("hello", "gemini_cli", "flash") is None


def test_different_system_prompt_is_different_entry():
    with tempfile.TemporaryDirectory() as d:
        cache = LLMCache(d)
        fake = _FakeClient()
        client = CachingLLMClient(fake, cache, "gemini_cli", None)

        r1 = asyncio.run(client.generate("hello", system_prompt="sys-a"))
        r2 = asyncio.run(client.generate("hello", system_prompt="sys-b"))

        k1 = "sys-a\n---\nhello"
        k2 = "sys-b\n---\nhello"
        assert cache.get(k1, "gemini_cli", None) == r1
        assert cache.get(k2, "gemini_cli", None) == r2
        assert k1 != k2

        _ = asyncio.run(client.generate("hello", system_prompt="sys-a"))
        assert fake.calls == 2
