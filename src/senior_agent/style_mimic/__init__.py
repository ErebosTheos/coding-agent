from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from senior_agent.utils import is_within_workspace

logger = logging.getLogger(__name__)

_SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cpp",
    ".c",
    ".h",
}
_EXCLUDED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
}
_MAX_SCAN_FILES = 1000
_MAX_STYLE_SAMPLES = 5
_MAX_ARCHITECTURE_SIGNALS: Final[int] = 4


@dataclass
class StyleMimic:
    """Infer concise repository style guidance for LLM prompt injection."""

    def infer_project_style(self, workspace: Path) -> str:
        workspace_root = Path(workspace).resolve()
        if not workspace_root.exists() or not workspace_root.is_dir():
            logger.warning("StyleMimic fallback: invalid workspace %s", workspace_root)
            return "Style: preserve existing conventions."
        if not is_within_workspace(workspace_root, workspace_root):
            logger.warning("StyleMimic fallback: workspace boundary check failed for %s", workspace_root)
            return "Style: preserve existing conventions."

        source_files = self._collect_source_files(workspace_root)
        if not source_files:
            return "Style: preserve existing conventions."

        primary_extension = self._detect_primary_extension(source_files)
        sampled = self._sample_files(source_files, primary_extension)

        snippets: list[str] = []
        for file_path in sampled:
            try:
                snippets.append(file_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue

        if not snippets:
            return "Style: preserve existing conventions."

        indentation = self._infer_indentation(snippets)
        naming = self._infer_naming(snippets)
        quotes = self._infer_quote_style(snippets)
        framework = self._detect_framework(workspace_root, sampled, snippets)
        architecture = self._infer_architecture_patterns(
            workspace_root=workspace_root,
            snippets=snippets,
        )

        architecture_summary = ", ".join(architecture) if architecture else "general layering"
        return (
            f"Style: {indentation}, {naming} names, {quotes} quotes, "
            f"{framework} patterns. "
            f"Architecture: {architecture_summary}."
        )

    @staticmethod
    def _collect_source_files(workspace_root: Path) -> list[Path]:
        source_files: list[Path] = []
        for file_path in workspace_root.rglob("*"):
            if len(source_files) >= _MAX_SCAN_FILES:
                break
            if not file_path.is_file():
                continue
            if any(part in _EXCLUDED_DIR_NAMES for part in file_path.parts):
                continue
            if file_path.suffix.lower() not in _SOURCE_EXTENSIONS:
                continue
            source_files.append(file_path)
        return source_files

    @staticmethod
    def _detect_primary_extension(source_files: list[Path]) -> str:
        counts = Counter(path.suffix.lower() for path in source_files)
        if not counts:
            return ".py"
        return counts.most_common(1)[0][0]

    @staticmethod
    def _sample_files(source_files: list[Path], primary_extension: str) -> list[Path]:
        primary = [path for path in source_files if path.suffix.lower() == primary_extension]
        non_primary = [path for path in source_files if path.suffix.lower() != primary_extension]
        sampled = primary[:_MAX_STYLE_SAMPLES]
        if len(sampled) < _MAX_STYLE_SAMPLES:
            sampled.extend(non_primary[: _MAX_STYLE_SAMPLES - len(sampled)])
        return sampled

    @staticmethod
    def _infer_indentation(snippets: list[str]) -> str:
        tab_lines = 0
        space_indents: list[int] = []

        for content in snippets:
            for line in content.splitlines():
                stripped = line.lstrip(" \t")
                if not stripped:
                    continue
                prefix = line[: len(line) - len(stripped)]
                if not prefix:
                    continue
                if prefix.startswith("\t"):
                    tab_lines += 1
                elif prefix.startswith(" "):
                    space_indents.append(len(prefix))

        if tab_lines > len(space_indents):
            return "tab indentation"
        if not space_indents:
            return "4-space indentation"

        width = Counter(space_indents).most_common(1)[0][0]
        if width <= 0:
            width = 4
        return f"{width}-space indentation"

    @staticmethod
    def _infer_quote_style(snippets: list[str]) -> str:
        single_quotes = 0
        double_quotes = 0
        for content in snippets:
            single_quotes += len(re.findall(r"'[^'\n]{0,120}'", content))
            double_quotes += len(re.findall(r'"[^"\n]{0,120}"', content))

        if single_quotes > double_quotes:
            return "single"
        return "double"

    @staticmethod
    def _infer_naming(snippets: list[str]) -> str:
        snake = 0
        camel = 0
        pascal = 0

        for content in snippets:
            identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", content)
            for name in identifiers:
                if "_" in name and name.lower() == name:
                    snake += 1
                    continue
                if re.match(r"^[a-z]+(?:[A-Z][a-z0-9]*)+$", name):
                    camel += 1
                    continue
                if re.match(r"^[A-Z][A-Za-z0-9]+$", name):
                    pascal += 1

        counts = {"snake_case": snake, "camelCase": camel, "PascalCase": pascal}
        naming, total = max(counts.items(), key=lambda item: item[1])
        if total == 0:
            return "snake_case"
        return naming

    @staticmethod
    def _detect_framework(
        workspace_root: Path,
        sampled_files: list[Path],
        snippets: list[str],
    ) -> str:
        merged = "\n".join(snippets)

        if "FastAPI(" in merged or "from fastapi import" in merged:
            return "FastAPI"
        if "import django" in merged or "from django" in merged:
            return "Django"
        if any(path.suffix.lower() in {".jsx", ".tsx"} for path in sampled_files):
            return "React"
        if "from 'react'" in merged or 'from "react"' in merged:
            return "React"
        if any(path.suffix.lower() == ".vue" for path in sampled_files):
            return "Vue"
        if "from 'vue'" in merged or 'from "vue"' in merged:
            return "Vue"

        package_json = workspace_root / "package.json"
        if package_json.exists():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                deps: dict[str, str] = {}
                for key in ("dependencies", "devDependencies"):
                    value = payload.get(key)
                    if isinstance(value, dict):
                        deps.update({str(k): str(v) for k, v in value.items()})
                if "react" in deps:
                    return "React"
                if "vue" in deps:
                    return "Vue"

        return "general"

    @classmethod
    def _infer_architecture_patterns(
        cls,
        *,
        workspace_root: Path,
        snippets: list[str],
    ) -> list[str]:
        merged = "\n".join(snippets)
        lower_merged = merged.lower()
        patterns: list[str] = []

        def add(label: str) -> None:
            if label not in patterns:
                patterns.append(label)

        # Python architecture signals.
        if (
            "from pydantic import" in lower_merged
            or "import pydantic" in lower_merged
            or re.search(r"class\s+\w+\((?:\w+, )*(?:basemodel|sqlmodel)\)", merged)
        ):
            add("Pydantic data models")
        if "@dataclass" in lower_merged:
            add("dataclass-oriented domain models")
        if (
            "apirouter(" in lower_merged
            or "depends(" in lower_merged
            or "from fastapi import" in lower_merged
            or "fastapi(" in lower_merged
            or re.search(r"@(?:app|router)\.(get|post|put|patch|delete)\(", lower_merged)
        ):
            add("FastAPI router/dependency flow")
        if (
            "sqlalchemy" in lower_merged
            or "from sqlalchemy import" in lower_merged
            or "declarative_base" in lower_merged
        ):
            add("SQLAlchemy ORM layers")
        if "pytest.fixture" in lower_merged or "@pytest.fixture" in lower_merged:
            add("fixture-driven test composition")

        # Frontend architecture signals.
        if (
            "usestate(" in lower_merged
            or "useeffect(" in lower_merged
            or "usememo(" in lower_merged
            or "usecallback(" in lower_merged
        ):
            add("React hooks-first components")
        if "createcontext(" in lower_merged or "usecontext(" in lower_merged:
            add("React context state sharing")
        if "usereducer(" in lower_merged:
            add("reducer-based state transitions")
        if (
            "createslice(" in lower_merged
            or "@reduxjs/toolkit" in lower_merged
            or "configurestore(" in lower_merged
        ):
            add("Redux toolkit state modules")

        # Repository-level architecture signals.
        if (workspace_root / "docker-compose.yml").exists() or (workspace_root / "docker-compose.yaml").exists():
            add("container-orchestrated services")
        if (workspace_root / "pnpm-workspace.yaml").exists() or (workspace_root / "turbo.json").exists():
            add("workspace monorepo structure")

        if not patterns:
            return []
        return patterns[:_MAX_ARCHITECTURE_SIGNALS]


__all__ = ["StyleMimic"]
