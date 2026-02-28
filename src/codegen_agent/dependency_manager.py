from __future__ import annotations
import os
import logging
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any
from .models import CommandResult, GeneratedFile, Plan
from .utils import run_shell_command
from .llm.protocol import LLMClient

logger = logging.getLogger(__name__)

_PYTHON_MISSING_MODULE_PATTERNS = (
    re.compile(
        r"""ModuleNotFoundError:\s*No module named ['"](?P<name>[A-Za-z0-9_.-]+)['"]"""
    ),
    re.compile(
        r"""ImportError:\s*No module named ['"]?(?P<name>[A-Za-z0-9_.-]+)['"]?"""
    ),
)
_NODE_MISSING_MODULE_PATTERN = re.compile(
    r"""(?:Error:\s*)?Cannot find module ['"](?P<name>[^'"]+)['"]""",
    re.IGNORECASE,
)
_ALLOWED_DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9._/@-]+$")
# Python dev tools safe to auto-install when referenced in validation commands.
_VALIDATION_TOOL_WHITELIST: frozenset[str] = frozenset({
    "ruff", "black", "mypy", "isort", "flake8", "pylint",
    "bandit", "pyright", "pyflakes", "pep8", "autopep8",
})
# Top-level import name → pip package name for common frameworks.
# Used when no requirements.txt/pyproject.toml is present.
_FRAMEWORK_IMPORT_MAP: dict[str, str] = {
    "fastapi": "fastapi[standard]",
    "uvicorn": "uvicorn",
    "flask": "flask",
    "django": "django",
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "pydantic": "pydantic",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
    "celery": "celery",
    "redis": "redis",
    "pymongo": "pymongo",
    "motor": "motor",
    "boto3": "boto3",
    "requests": "requests",
    "starlette": "starlette",
}


@dataclass(frozen=True)
class DependencyCandidate:
    name: str
    ecosystem: str


class DependencyManager:
    """Attempt to install missing runtime dependencies when validation fails."""

    def __init__(self, llm_client: Optional[LLMClient] = None, workspace: str = "."):
        self.llm_client = llm_client
        self.workspace = workspace
        self.executor = run_shell_command

    async def resolve_and_install(
        self,
        generated_files: List[GeneratedFile],
        plan: Plan,
        validation_commands: List[str] | None = None,
    ) -> Dict[str, Any]:
        """Stage 4: Analyze generated files and install inferred dependencies."""
        import asyncio
        workspace_root = Path(self.workspace).resolve()

        tasks: list = []
        labels: list[str] = []

        if (workspace_root / "package.json").exists():
            print("  [DependencyManager] Installing Node.js dependencies...")
            tasks.append(asyncio.to_thread(self.executor, "npm install", str(workspace_root)))
            labels.append("package.json")

        if (workspace_root / "requirements.txt").exists():
            print("  [DependencyManager] Installing Python dependencies (requirements.txt)...")
            cmd = f"{shlex.quote(sys.executable)} -m pip install -r requirements.txt"
            tasks.append(asyncio.to_thread(self.executor, cmd, str(workspace_root)))
            labels.append("requirements.txt")

        if (workspace_root / "pyproject.toml").exists():
            print("  [DependencyManager] Installing Python dependencies (pyproject.toml)...")
            cmd = f"{shlex.quote(sys.executable)} -m pip install ."
            tasks.append(asyncio.to_thread(self.executor, cmd, str(workspace_root)))
            labels.append("pyproject.toml")

        results: Dict[str, Any] = {"installed_manifests": [], "errors": []}
        if tasks:
            cmd_results = await asyncio.gather(*tasks, return_exceptions=True)
            for label, res in zip(labels, cmd_results):
                if isinstance(res, Exception):
                    results["errors"].append(f"{label} install raised: {res}")
                elif res.exit_code == 0:
                    results["installed_manifests"].append(label)
                else:
                    results["errors"].append(f"{label} install failed: {res.stderr}")

        # Install any whitelisted validation tools that are missing from the environment.
        if validation_commands:
            workspace_root = Path(self.workspace).resolve()
            for cmd in validation_commands:
                parts = cmd.strip().split()
                if not parts:
                    continue
                tool = parts[0]
                if tool not in _VALIDATION_TOOL_WHITELIST:
                    continue
                if shutil.which(tool) is not None:
                    continue   # already installed
                print(f"  [DependencyManager] Installing missing validation tool: {tool}")
                install_cmd = f"{shlex.quote(sys.executable)} -m pip install {shlex.quote(tool)}"
                try:
                    res = await asyncio.to_thread(self.executor, install_cmd, str(workspace_root))
                    if res.exit_code == 0:
                        print(f"  [DependencyManager] Installed: {tool}")
                    else:
                        print(
                            f"  [DependencyManager] Failed to install {tool}: "
                            f"{res.stderr.strip()[:200]}"
                        )
                        results["errors"].append(f"tool install failed: {tool}")
                except Exception as exc:
                    results["errors"].append(f"tool install crashed: {tool}: {exc}")

        # If no manifest was found, scan imports and install known frameworks.
        has_manifest = bool(
            (workspace_root / "requirements.txt").exists()
            or (workspace_root / "pyproject.toml").exists()
            or (workspace_root / "package.json").exists()
        )
        if not has_manifest:
            framework_installs = await self._install_inferred_frameworks(
                generated_files, workspace_root
            )
            results["framework_installs"] = framework_installs

        # Ensure root-level Python modules are importable by pytest.
        injected = self._ensure_conftest(workspace_root, generated_files)
        if injected:
            print("  [DependencyManager] Wrote conftest.py to make root modules importable.")
        results["conftest_injected"] = injected

        return results

    async def _install_inferred_frameworks(
        self,
        generated_files: List[GeneratedFile],
        workspace_root: Path,
    ) -> list[str]:
        """Scan generated Python files for known framework imports and install them."""
        import asyncio
        import importlib.util

        needed: dict[str, str] = {}  # import_name → pip_package
        for f in generated_files:
            if not f.file_path.endswith(".py"):
                continue
            for line in f.content.splitlines():
                line = line.strip()
                if not (line.startswith("import ") or line.startswith("from ")):
                    continue
                # Extract top-level module name
                parts = line.split()
                top = parts[1].split(".")[0] if len(parts) > 1 else ""
                if top in _FRAMEWORK_IMPORT_MAP and top not in needed:
                    # Only install if not already importable
                    if importlib.util.find_spec(top) is None:
                        needed[top] = _FRAMEWORK_IMPORT_MAP[top]

        if not needed:
            return []

        installed: list[str] = []
        packages = list(needed.values())
        print(f"  [DependencyManager] Installing inferred frameworks: {packages}")
        install_cmd = (
            f"{shlex.quote(sys.executable)} -m pip install "
            + " ".join(shlex.quote(p) for p in packages)
        )
        try:
            res = await asyncio.to_thread(self.executor, install_cmd, str(workspace_root))
            if res.exit_code == 0:
                installed = packages
                print(f"  [DependencyManager] Installed: {packages}")
            else:
                print(
                    f"  [DependencyManager] Framework install failed: "
                    f"{res.stderr.strip()[:300]}"
                )
        except Exception as exc:
            print(f"  [DependencyManager] Framework install crashed: {exc}")

        return installed

    def check_and_fix_dependencies(
        self,
        result: CommandResult,
        workspace: str,
    ) -> bool:
        workspace_root = Path(workspace).resolve()
        if not workspace_root.exists() or not workspace_root.is_dir():
            logger.warning("DependencyManager skipped invalid workspace: %s", workspace_root)
            return False

        candidate = self._extract_candidate(result.stderr)
        if candidate is None:
            return False

        install_command = self._build_install_command(candidate, workspace_root)
        if install_command is None:
            logger.info(
                "DependencyManager found missing dependency but no matching install environment: %s",
                candidate.name,
            )
            return False

        logger.info(
            "DependencyManager attempting install: ecosystem=%s dependency=%s command=%s",
            candidate.ecosystem,
            candidate.name,
            install_command,
        )
        try:
            install_result = self.executor(install_command, str(workspace_root))
        except Exception as exc:
            logger.exception("Dependency installation command crashed: %s", exc)
            return False

        if install_result.exit_code != 0:
            logger.error(
                "Dependency install failed: dependency=%s command=%s code=%s stderr=%s",
                candidate.name,
                install_command,
                install_result.exit_code,
                install_result.stderr.strip(),
            )
            return False

        logger.info("Dependency installed successfully: %s", candidate.name)
        return True

    def _extract_candidate(self, stderr: str) -> DependencyCandidate | None:
        text = stderr or ""

        for pattern in _PYTHON_MISSING_MODULE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            module_name = match.group("name").strip()
            if not module_name:
                continue
            package_name = module_name.split(".", 1)[0]
            if self._is_allowed_dependency_name(package_name):
                return DependencyCandidate(name=package_name, ecosystem="python")

        node_match = _NODE_MISSING_MODULE_PATTERN.search(text)
        if node_match:
            package_name = node_match.group("name").strip()
            if self._is_allowed_dependency_name(package_name):
                return DependencyCandidate(name=package_name, ecosystem="node")

        return None

    @staticmethod
    def _build_install_command(
        candidate: DependencyCandidate,
        workspace_root: Path,
    ) -> str | None:
        has_node_project = (workspace_root / "package.json").exists()
        has_python_project = (
            (workspace_root / "requirements.txt").exists()
            or (workspace_root / "pyproject.toml").exists()
        )
        dependency = shlex.quote(candidate.name)

        if candidate.ecosystem == "node" and has_node_project:
            return f"npm install {dependency}"
        if candidate.ecosystem == "python" and has_python_project:
            return f"{shlex.quote(sys.executable)} -m pip install {dependency}"

        # Fallback to environment inference when ecosystem hints are weak.
        if has_node_project:
            return f"npm install {dependency}"
        if has_python_project:
            return f"{shlex.quote(sys.executable)} -m pip install {dependency}"
        return None

    @staticmethod
    def _is_allowed_dependency_name(name: str) -> bool:
        return bool(name) and bool(_ALLOWED_DEPENDENCY_NAME.match(name))

    @staticmethod
    def _ensure_conftest(
        workspace_root: Path,
        generated_files: List[GeneratedFile],
        extra_test_paths: Optional[List[str]] = None,
    ) -> bool:
        """Write a minimal conftest.py if root-level Python source modules exist
        alongside a tests/ subdirectory and no conftest.py is already present.

        This makes flat-package projects (prime.py, utils.py at root) importable
        when the test runner is invoked as ``pytest tests/`` without a proper
        pyproject.toml [project] section or pythonpath setting.
        """
        conftest_path = workspace_root / "conftest.py"
        if conftest_path.exists():
            return False

        # Root-level .py files that are not test files or conftest itself
        root_modules = [
            f for f in generated_files
            if f.file_path.endswith(".py")
            and "/" not in f.file_path
            and not f.file_path.startswith("test_")
            and f.file_path != "conftest.py"
        ]

        def _is_subdir_test(path: str) -> bool:
            normalized = path.replace("\\", "/")
            name = os.path.basename(normalized)
            return (
                "/" in normalized
                and normalized.endswith(".py")
                and (
                    name.startswith("test_")
                    or name.endswith("_test.py")
                    or "/tests/" in normalized
                )
            )

        # Any Python test file living in a subdirectory.
        has_test_subdir = any(_is_subdir_test(f.file_path) for f in generated_files)
        if not has_test_subdir and extra_test_paths:
            has_test_subdir = any(_is_subdir_test(path) for path in extra_test_paths)
        if not has_test_subdir:
            tests_dir = workspace_root / "tests"
            if tests_dir.is_dir():
                has_test_subdir = any(
                    py.is_file()
                    and (py.name.startswith("test_") or py.name.endswith("_test.py"))
                    for py in tests_dir.rglob("*.py")
                )

        # src/ layout: Python files live under src/, tests under tests/
        has_src_layout = (workspace_root / "src").is_dir() and any(
            f.file_path.startswith("src/") and f.file_path.endswith(".py")
            for f in generated_files
        )

        if (root_modules or has_src_layout) and has_test_subdir:
            extra_paths = []
            if has_src_layout:
                extra_paths.append(
                    "SRC = os.path.join(ROOT, 'src')\n"
                    "if SRC not in sys.path:\n"
                    "    sys.path.insert(0, SRC)\n"
                )
            conftest_path.write_text(
                "import os\n"
                "import sys\n\n"
                "ROOT = os.path.dirname(os.path.abspath(__file__))\n"
                "if ROOT not in sys.path:\n"
                "    sys.path.insert(0, ROOT)\n"
                + "".join(extra_paths)
            )
            return True

        return False
