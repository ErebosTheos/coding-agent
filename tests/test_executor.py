import asyncio
from pathlib import Path

from codegen_agent.executor import Executor, _fix_relative_imports
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
