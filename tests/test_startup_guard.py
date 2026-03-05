"""Tests for StartupLifespanGuard — entry-point detection and import command generation."""
import sys
from pathlib import Path

import pytest

from codegen_agent.startup_guard import (
    build_import_check_command,
    detect_entry_point,
)
from codegen_agent.models import GeneratedFile


def _gf(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(file_path=path, content=content, node_id="n", sha256="x")


def _write(tmp_path: Path, rel: str, content: str = "") -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ── detect_entry_point ────────────────────────────────────────────────────────

def test_detects_run_py_for_fastapi_project(tmp_path):
    _write(tmp_path, "run.py", "import uvicorn")
    files = [_gf("src/main.py", "from fastapi import FastAPI\napp = FastAPI()")]
    assert detect_entry_point(files, str(tmp_path)) == "run.py"


def test_detects_src_main_when_no_run_py(tmp_path):
    _write(tmp_path, "src/main.py", "from fastapi import FastAPI")
    files = [_gf("src/main.py", "from fastapi import FastAPI\napp = FastAPI()")]
    assert detect_entry_point(files, str(tmp_path)) == "src/main.py"


def test_returns_none_for_non_web_project(tmp_path):
    _write(tmp_path, "run.py")
    files = [_gf("src/utils.py", "def add(a, b): return a + b")]
    assert detect_entry_point(files, str(tmp_path)) is None


def test_returns_none_when_no_entry_point_on_disk(tmp_path):
    # FastAPI project but no run.py / app.py etc. on disk
    files = [_gf("src/main.py", "from fastapi import FastAPI\napp = FastAPI()")]
    assert detect_entry_point(files, str(tmp_path)) is None


def test_detects_flask_project(tmp_path):
    _write(tmp_path, "app.py", "from flask import Flask")
    files = [_gf("app.py", "from flask import Flask\napp = Flask(__name__)")]
    assert detect_entry_point(files, str(tmp_path)) == "app.py"


def test_prefers_run_py_over_app_py(tmp_path):
    _write(tmp_path, "run.py", "")
    _write(tmp_path, "app.py", "")
    files = [_gf("src/main.py", "from fastapi import FastAPI")]
    assert detect_entry_point(files, str(tmp_path)) == "run.py"


# ── build_import_check_command ────────────────────────────────────────────────

def test_run_py_becomes_import_run():
    cmd = build_import_check_command("run.py")
    assert "import run" in cmd
    assert sys.executable in cmd or "python" in cmd


def test_src_main_py_becomes_import_src_main():
    cmd = build_import_check_command("src/main.py")
    assert "import src.main" in cmd


def test_nested_path_conversion():
    cmd = build_import_check_command("app/main.py")
    assert "import app.main" in cmd


def test_command_is_properly_quoted():
    """Command must be safe to pass to shell — no unquoted spaces."""
    cmd = build_import_check_command("src/main.py")
    # The -c argument must be quoted (shlex.quote wraps in single quotes)
    assert "'" in cmd or '"' in cmd
