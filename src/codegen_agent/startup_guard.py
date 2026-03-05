"""StartupLifespanGuard — deterministic app import smoke-check for web projects.

Detects the Python web app entry point and injects a ``python -c "import <module>"``
command into Stage 6 validation commands.  Any ImportError, SyntaxError, or
module-level TypeError (e.g. invalid ORM constructor call in a seed imported at
module level) surfaces as concrete healer input instead of a cryptic pytest failure.

Zero LLM cost.  Runs once per Stage 6 invocation.  Only activates for projects
that import a known Python web framework (FastAPI, Flask, Django, Starlette, aiohttp).
"""
import re
import shlex
import sys
from pathlib import Path

_FRAMEWORK_RE = re.compile(
    r"^(?:from|import)\s+(fastapi|flask|django|starlette|aiohttp)\b",
    re.MULTILINE,
)

# Candidate entry points in priority order.
# run.py is the conventional FastAPI launcher; src/main.py is the app factory.
_ENTRY_CANDIDATES = [
    "run.py",
    "app.py",
    "main.py",
    "src/main.py",
    "src/app.py",
    "src/run.py",
    "app/main.py",
    "app/app.py",
]


def detect_entry_point(generated_files, workspace: str) -> str | None:
    """Return the relative path of the web app entry point, or None.

    Only activates when at least one generated Python file imports a known
    web framework — avoids injecting import checks for CLI scripts, data
    pipelines, or non-Python projects.
    """
    is_web = any(
        _FRAMEWORK_RE.search(f.content)
        for f in generated_files
        if f.file_path.endswith(".py")
    )
    if not is_web:
        return None

    for candidate in _ENTRY_CANDIDATES:
        if Path(workspace, candidate).exists():
            return candidate

    return None


def build_import_check_command(entry_point: str) -> str:
    """Return ``python -c "import <module>"`` for the given entry-point path.

    Converts ``src/main.py`` → ``import src.main`` so the check runs from the
    project root (the cwd used by all validation commands).
    """
    module = entry_point.replace("\\", "/").replace("/", ".").removesuffix(".py")
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(f'import {module}')}"
