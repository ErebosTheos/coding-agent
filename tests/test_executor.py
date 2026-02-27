import asyncio
from pathlib import Path

from codegen_agent.executor import Executor
from codegen_agent.models import Architecture, ExecutionNode, ExecutionResult


class DummyLLM:
    def __init__(self, response: str):
        self.response = response

    async def generate(self, prompt: str, system_prompt: str | None = None) -> str:
        return self.response


def test_execute_bulk_falls_back_when_response_is_missing_files(tmp_path):
    llm = DummyLLM('{"app/main.py":"print(1)"}')
    executor = Executor(llm_client=llm, workspace=str(tmp_path), max_bulk_files=99)
    architecture = Architecture(
        file_tree=["app/main.py", "app/utils.py"],
        nodes=[
            ExecutionNode(node_id="main", file_path="app/main.py", purpose="main"),
            ExecutionNode(node_id="utils", file_path="app/utils.py", purpose="utils"),
        ],
        global_validation_commands=[],
    )

    sentinel = ExecutionResult(generated_files=[], failed_nodes=["fallback"])
    called = {"value": False}

    async def fake_fallback(arch):
        called["value"] = True
        return sentinel

    executor._execute_wave_fallback = fake_fallback  # type: ignore[method-assign]
    result = asyncio.run(executor._execute_bulk(architecture))

    assert called["value"] is True
    assert result == sentinel


def test_execute_skips_directory_nodes(tmp_path):
    llm = DummyLLM('{"pkg/__init__.py": ""}')
    executor = Executor(llm_client=llm, workspace=str(tmp_path), max_bulk_files=99)
    architecture = Architecture(
        file_tree=["pkg/", "pkg/__init__.py"],
        nodes=[
            ExecutionNode(node_id="pkg_dir", file_path="pkg/", purpose="directory"),
            ExecutionNode(node_id="pkg_init", file_path="pkg/__init__.py", purpose="init"),
        ],
        global_validation_commands=[],
    )

    result = asyncio.run(executor.execute(architecture))

    assert result.failed_nodes == []
    assert "pkg_dir" in result.skipped_nodes
    assert (Path(tmp_path) / "pkg" / "__init__.py").exists()
