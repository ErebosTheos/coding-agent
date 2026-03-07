#!/usr/bin/env python3
"""Repo quality gate with fast local mode and strict CI mode.

This script is intentionally deterministic and shell-free so both local
pre-push hooks and CI use identical checks.
"""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Keep this list small and high-signal. These modules exercise startup paths.
SMOKE_IMPORTS = [
    "codegen_agent.main",
    "codegen_agent.executor",
    "codegen_agent.healer",
    "codegen_agent.pytest_parser",
    "codegen_agent.dashboard.server",
]


def _run(cmd: list[str], *, env: dict[str, str]) -> None:
    print(f"\n[quality-gate] $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _require_tools(tools: list[str]) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(
            "[quality-gate] Missing required tool(s): "
            f"{joined}. Install dev dependencies and retry."
        )


def _smoke_imports() -> None:
    sys.path.insert(0, str(SRC))
    failures: list[str] = []
    for module_name in SMOKE_IMPORTS:
        try:
            importlib.import_module(module_name)
            print(f"[quality-gate] import OK: {module_name}")
        except Exception as exc:  # pragma: no cover - explicit failure path
            failures.append(f"{module_name}: {type(exc).__name__}: {exc}")

    if failures:
        raise SystemExit(
            "[quality-gate] Smoke import failures:\n- " + "\n- ".join(failures)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("fast", "ci"),
        default="fast",
        help="fast = local pre-push checks, ci = full verification",
    )
    args = parser.parse_args()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")

    _require_tools(["pytest"])

    # Syntax-only check to catch malformed generated edits quickly.
    _run([sys.executable, "-m", "compileall", "-q", "src", "tests"], env=env)

    # Catches missing modules and broken startup imports.
    _smoke_imports()

    if args.mode == "ci":
        _run(["pytest", "-q"], env=env)

    print(f"\n[quality-gate] PASS ({args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
