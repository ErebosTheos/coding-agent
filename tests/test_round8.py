import asyncio
import os
import tempfile
from unittest.mock import patch

from codegen_agent.llm.protocol import LLMTimeoutError
from codegen_agent.llm.router import _RetryingLLMClient


def test_retrying_client_retries_on_timeout():
    calls = []

    class _Primary:
        async def generate(self, prompt, system_prompt=""):
            calls.append(1)
            if len(calls) == 1:
                raise LLMTimeoutError("timeout")
            return "ok"

        async def astream(self, prompt, system_prompt=""):
            yield "ok"

    client = _RetryingLLMClient(_Primary(), role="test", max_retries=2)
    with patch("asyncio.sleep"):
        result = asyncio.run(client.generate("prompt"))

    assert result == "ok"
    assert len(calls) == 2


def test_retrying_client_uses_fallback_after_primary_exhausted():
    class _AlwaysFail:
        async def generate(self, prompt, system_prompt=""):
            raise LLMTimeoutError("always")

        async def astream(self, prompt, system_prompt=""):
            yield ""

    class _Fallback:
        async def generate(self, prompt, system_prompt=""):
            return "fallback_ok"

        async def astream(self, prompt, system_prompt=""):
            yield "fallback_ok"

    client = _RetryingLLMClient(_AlwaysFail(), role="test", fallback=_Fallback(), max_retries=1)
    with patch("asyncio.sleep"):
        result = asyncio.run(client.generate("prompt"))

    assert result == "fallback_ok"


def test_status_no_checkpoint_returns_zero():
    with tempfile.TemporaryDirectory() as d:
        from codegen_agent.main import _run_status_check
        code = _run_status_check(d)
        assert code == 0
