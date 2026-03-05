import asyncio
import os
from pathlib import Path

from codegen_agent.executor import (
    Executor,
    _ensure_async_sessionmaker_guardrail,
    _fix_relative_imports,
    _sanitize_source_text,
)
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


def test_fix_relative_imports_converts_package_imports():
    content = (
        "from src import crud, models, schemas\n"
        "from src.crud import get_task\n"
        "from src.models import Task\n"
        "import os\n"
    )
    result = _fix_relative_imports("src/main.py", content)
    assert "from . import crud, models, schemas" in result
    assert "from .crud import get_task" in result
    assert "from .models import Task" in result
    assert "import os" in result  # unrelated import unchanged


def test_fix_relative_imports_top_level_unchanged():
    content = "from src import utils\n"
    # top-level file — not inside any package dir
    assert _fix_relative_imports("main.py", content) == content


def test_fix_relative_imports_non_python_unchanged():
    content = "from src import utils\n"
    assert _fix_relative_imports("src/file.js", content) == content


def test_fix_relative_imports_nested_submodule():
    content = "from app.db.models import User\n"
    result = _fix_relative_imports("app/main.py", content)
    assert result == "from .db.models import User\n"


def test_fix_relative_imports_js_import_from():
    content = "import { db } from 'src/db'\nimport express from 'express'\n"
    result = _fix_relative_imports("src/main.js", content)
    assert "from './db'" in result
    assert "from 'express'" in result  # external package unchanged


def test_fix_relative_imports_ts_require():
    content = "const db = require('src/database')\nconst _ = require('lodash')\n"
    result = _fix_relative_imports("src/server.ts", content)
    assert "require('./database')" in result
    assert "require('lodash')" in result  # external unchanged


def test_fix_relative_imports_php_unchanged():
    # PHP uses namespaces / require_once — no conversion needed
    content = "use App\\Models\\User;\nrequire_once '../db.php';\n"
    result = _fix_relative_imports("src/controller.php", content)
    assert result == content


def test_async_sessionmaker_guardrail_injects_expire_on_commit():
    content = (
        "from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker\n"
        "SessionLocal = async_sessionmaker(engine, class_=AsyncSession)\n"
    )
    result = _ensure_async_sessionmaker_guardrail("src/database.py", content)
    assert "expire_on_commit=False" in result
    assert "async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)" in result


def test_async_sessionmaker_guardrail_preserves_existing_setting():
    content = (
        "SessionLocal = async_sessionmaker(\n"
        "    engine,\n"
        "    class_=AsyncSession,\n"
        "    expire_on_commit=False,\n"
        ")\n"
    )
    result = _ensure_async_sessionmaker_guardrail("src/database.py", content)
    assert result == content


def test_sanitize_source_text_removes_zero_width_chars_for_python():
    content = "x = 1\u200b\n\ufeffy = 2\n"
    result = _sanitize_source_text("src/main.py", content)
    assert "\u200b" not in result
    assert "\ufeff" not in result
    assert "x = 1" in result
    assert "y = 2" in result


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


def test_executor_env_zero_bulk_disables_bulk_generation(tmp_path):
    old = os.environ.get("CODEGEN_EXECUTOR_MAX_BULK_FILES")
    os.environ["CODEGEN_EXECUTOR_MAX_BULK_FILES"] = "0"
    try:
        llm = DummyLLM('{"app/main.py":"print(1)"}')
        executor = Executor(llm_client=llm, workspace=str(tmp_path), max_bulk_files=-1)
        assert executor.max_bulk_files == 0
    finally:
        if old is None:
            del os.environ["CODEGEN_EXECUTOR_MAX_BULK_FILES"]
        else:
            os.environ["CODEGEN_EXECUTOR_MAX_BULK_FILES"] = old


def test_calculate_waves_cycle_falls_back_to_deterministic_waves(tmp_path):
    llm = DummyLLM("{}")
    executor = Executor(llm_client=llm, workspace=str(tmp_path), concurrency=2, max_bulk_files=1)
    nodes = [
        ExecutionNode(node_id="a", file_path="src/a.py", purpose="a", depends_on=["b"]),
        ExecutionNode(node_id="b", file_path="src/b.py", purpose="b", depends_on=["a"]),
    ]

    waves = executor._calculate_waves(nodes)
    flattened = [n.node_id for wave in waves for n in wave]
    assert set(flattened) == {"a", "b"}
    assert len(flattened) == 2


def test_calculate_waves_cycle_strict_mode_raises(tmp_path):
    old = os.environ.get("CODEGEN_STRICT_DEP_GRAPH")
    os.environ["CODEGEN_STRICT_DEP_GRAPH"] = "1"
    try:
        llm = DummyLLM("{}")
        executor = Executor(llm_client=llm, workspace=str(tmp_path), concurrency=2, max_bulk_files=1)
        nodes = [
            ExecutionNode(node_id="a", file_path="src/a.py", purpose="a", depends_on=["b"]),
            ExecutionNode(node_id="b", file_path="src/b.py", purpose="b", depends_on=["a"]),
        ]

        import pytest
        with pytest.raises(ValueError, match="Cycle detected"):
            executor._calculate_waves(nodes)
    finally:
        if old is None:
            del os.environ["CODEGEN_STRICT_DEP_GRAPH"]
        else:
            os.environ["CODEGEN_STRICT_DEP_GRAPH"] = old
