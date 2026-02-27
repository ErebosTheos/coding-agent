from __future__ import annotations
import logging
import re
import shlex
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

    async def resolve_and_install(self, generated_files: List[GeneratedFile], plan: Plan) -> Dict[str, Any]:
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
        if not tasks:
            return results

        cmd_results = await asyncio.gather(*tasks, return_exceptions=True)
        for label, res in zip(labels, cmd_results):
            if isinstance(res, Exception):
                results["errors"].append(f"{label} install raised: {res}")
            elif res.exit_code == 0:
                results["installed_manifests"].append(label)
            else:
                results["errors"].append(f"{label} install failed: {res.stderr}")

        return results

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
