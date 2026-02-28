from codegen_agent.llm.codex_cli import CodexCLIClient
import asyncio
from unittest.mock import patch

import pytest


def test_extract_stream_text_item_completed_agent_message():
    event = {
        "type": "item.completed",
        "item": {"id": "item_1", "type": "agent_message", "text": "hello"},
    }
    assert CodexCLIClient._extract_stream_text(event) == "hello"


def test_extract_stream_text_legacy_agent_message_shape():
    event = {
        "type": "agent_message",
        "role": "assistant",
        "content": "legacy content",
    }
    assert CodexCLIClient._extract_stream_text(event) == "legacy content"


def test_extract_stream_text_unknown_event_returns_none():
    event = {"type": "turn.completed", "usage": {"output_tokens": 12}}
    assert CodexCLIClient._extract_stream_text(event) is None


def test_generate_cancellation_terminates_process():
    class _Proc:
        returncode = None
        pid = 12345

        async def communicate(self):
            await asyncio.sleep(10)

        async def wait(self):
            return 0

        def kill(self):
            self.returncode = -9

    async def _run():
        with patch("asyncio.create_subprocess_exec", return_value=_Proc()):
            with patch("os.killpg") as killpg:
                task = asyncio.create_task(CodexCLIClient().generate("hello"))
                await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                killpg.assert_called()

    asyncio.run(_run())


def test_astream_cancellation_terminates_process():
    class _Stdout:
        async def readline(self):
            await asyncio.sleep(10)
            return b""

    class _Proc:
        returncode = None
        pid = 12345
        stdout = _Stdout()
        stderr = None

        async def wait(self):
            return 0

        def kill(self):
            self.returncode = -9

    async def _run():
        with patch("asyncio.create_subprocess_exec", return_value=_Proc()):
            with patch("os.killpg") as killpg:
                client = CodexCLIClient()

                async def _consume():
                    async for _ in client.astream("hello"):
                        pass

                task = asyncio.create_task(_consume())
                await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                killpg.assert_called()

    asyncio.run(_run())
