import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from codegen_agent.healer import Healer


def test_max_heals_zero_makes_no_llm_calls():
    with tempfile.TemporaryDirectory() as d:
        mock_llm = AsyncMock()
        healer = Healer(mock_llm, d, max_attempts=0)
        report = asyncio.run(healer.heal(["echo ok"]))
        assert report.attempts == []
        mock_llm.generate.assert_not_called()


def test_codex_cli_timeout_raises_lmmtimeouterror(monkeypatch):
    from codegen_agent.llm.codex_cli import CodexCLIClient
    from codegen_agent.llm.protocol import LLMTimeoutError

    monkeypatch.setenv("CODEGEN_LLM_TIMEOUT", "1")

    killed = []

    class _HangingProcess:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            killed.append(True)

        async def wait(self):
            pass

    with patch("asyncio.create_subprocess_exec", return_value=_HangingProcess()):
        client = CodexCLIClient()
        with pytest.raises(LLMTimeoutError):
            asyncio.run(client.generate("test prompt"))

    assert killed, "process.kill() must be called on timeout"


def test_llm_timeout_env_controls_all_cli_clients(monkeypatch):
    monkeypatch.setenv("CODEGEN_LLM_TIMEOUT", "45")
    timeout = int(os.environ.get("CODEGEN_LLM_TIMEOUT", "120"))
    assert timeout == 45

    monkeypatch.delenv("CODEGEN_LLM_TIMEOUT", raising=False)
    timeout = int(os.environ.get("CODEGEN_LLM_TIMEOUT", "120"))
    assert timeout == 120
