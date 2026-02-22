from __future__ import annotations

import logging
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from senior_agent.engine import Executor, run_shell_command
from senior_agent.models import CommandResult
from senior_agent.utils import is_within_workspace

logger = logging.getLogger(__name__)

_PYTHON_MISSING_MODULE_PATTERNS = (
    re.compile(
        r"ModuleNotFoundError:\s*No module named ['\"](?P<name>[A-Za-z0-9_.-]+)['\"]"
    ),
    re.compile(
        r"ImportError:\s*No module named ['\"]?(?P<name>[A-Za-z0-9_.-]+)['\"]?"
    ),
)
_NODE_MISSING_MODULE_PATTERN = re.compile(
    r"(?:Error:\s*)?Cannot find module ['\"](?P<name>[^'\"]+)['\"]",
    re.IGNORECASE,
)
_ALLOWED_DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9._/@-]+$")


@dataclass(frozen=True)
class DependencyCandidate:
    name: str
    ecosystem: str


@dataclass
class DependencyManager:
    """Attempt to install missing runtime dependencies when validation fails."""

    executor: Executor = run_shell_command

    def check_and_fix_dependencies(
        self,
        result: CommandResult,
        workspace: Path,
    ) -> bool:
        workspace_root = Path(workspace).resolve()
        if not workspace_root.exists() or not workspace_root.is_dir():
            logger.warning("DependencyManager skipped invalid workspace: %s", workspace_root)
            return False
        if not is_within_workspace(workspace_root, workspace_root):
            logger.error("DependencyManager blocked workspace outside boundary: %s", workspace_root)
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
            install_result = self.executor(install_command, workspace_root)
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Dependency installation command crashed: %s", exc)
            return False

        if install_result.return_code != 0:
            logger.error(
                "Dependency install failed: dependency=%s command=%s code=%s stderr=%s",
                candidate.name,
                install_command,
                install_result.return_code,
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


__all__ = ["DependencyManager"]
