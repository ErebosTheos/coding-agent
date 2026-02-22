from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from senior_agent.utils import is_within_workspace

logger = logging.getLogger(__name__)

_DEFAULT_SOURCE_EXTENSIONS = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".cxx",
    ".hpp",
    ".hh",
    ".kt",
    ".kts",
)

_DEFAULT_EXCLUDE_DIRS = (
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
)


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.definitions: set[str] = set()
        self.calls: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.definitions.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.definitions.add(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.definitions.add(node.name)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.definitions.add(child.name)
                self.definitions.add(f"{node.name}.{child.name}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        called_symbol = self._extract_called_symbol(node.func)
        if called_symbol:
            self.calls.add(called_symbol)
        self.generic_visit(node)

    @staticmethod
    def _extract_called_symbol(func_node: ast.AST) -> str | None:
        if isinstance(func_node, ast.Name):
            return func_node.id
        if isinstance(func_node, ast.Attribute):
            return func_node.attr
        return None


@dataclass
class SymbolGraph:
    """Build and query a repository-level symbol dependency graph."""

    source_extensions: tuple[str, ...] = _DEFAULT_SOURCE_EXTENSIONS
    exclude_dirs: tuple[str, ...] = _DEFAULT_EXCLUDE_DIRS
    max_files: int = 5000
    max_file_bytes: int = 1_000_000
    _workspace: Path | None = field(default=None, init=False, repr=False)
    _file_definitions: dict[Path, set[str]] = field(default_factory=dict, init=False, repr=False)
    _dependents_index: dict[tuple[Path, str], set[Path]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def build_graph(self, workspace: Path) -> None:
        workspace_root = Path(workspace).resolve()
        if not workspace_root.exists() or not workspace_root.is_dir():
            raise ValueError(f"Invalid workspace for symbol graph: {workspace_root}")

        file_definitions: dict[Path, set[str]] = {}
        file_calls: dict[Path, set[str]] = {}
        scanned_files = 0
        skipped_due_to_limit = False

        for source_file in self._iter_source_files(workspace_root):
            if scanned_files >= self.max_files:
                skipped_due_to_limit = True
                break
            scanned_files += 1

            if source_file.suffix.lower() != ".py":
                continue
            if self._is_too_large(source_file):
                continue

            parsed = self._parse_python_file(source_file)
            if parsed is None:
                continue
            definitions, calls = parsed
            file_definitions[source_file] = definitions
            file_calls[source_file] = calls

        symbol_callers: dict[str, set[Path]] = {}
        for file_path, calls in file_calls.items():
            for called_symbol in calls:
                symbol_callers.setdefault(called_symbol, set()).add(file_path)

        dependents_index: dict[tuple[Path, str], set[Path]] = {}
        for file_path, symbols in file_definitions.items():
            for symbol_name in symbols:
                callers = symbol_callers.get(symbol_name, set())
                dependents = {caller for caller in callers if caller != file_path}
                if dependents:
                    dependents_index[(file_path, symbol_name)] = dependents

        self._workspace = workspace_root
        self._file_definitions = file_definitions
        self._dependents_index = dependents_index

        logger.info(
            "Built symbol graph: workspace=%s scanned=%s python_files=%s symbols=%s edges=%s limited=%s",
            workspace_root,
            scanned_files,
            len(file_definitions),
            sum(len(symbols) for symbols in file_definitions.values()),
            len(dependents_index),
            skipped_due_to_limit,
        )

    def get_defined_symbols(self, file_path: Path) -> tuple[str, ...]:
        resolved = self._resolve_workspace_path(file_path)
        if resolved is None:
            return ()
        return tuple(sorted(self._file_definitions.get(resolved, set())))

    def get_dependents(self, file_path: Path, symbol_name: str) -> list[Path]:
        symbol = symbol_name.strip()
        if not symbol:
            return []

        resolved = self._resolve_workspace_path(file_path)
        if resolved is None:
            return []

        dependents = self._dependents_index.get((resolved, symbol), set())
        return sorted(dependents)

    def _resolve_workspace_path(self, file_path: Path) -> Path | None:
        if self._workspace is None:
            return None

        candidate = Path(file_path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._workspace / candidate).resolve()
        )
        if not is_within_workspace(self._workspace, resolved):
            return None
        return resolved

    def _iter_source_files(self, workspace_root: Path) -> Iterator[Path]:
        extensions = {suffix.lower() for suffix in self.source_extensions}
        for root, dirs, files in os.walk(workspace_root, topdown=True):
            dirs[:] = [directory for directory in dirs if directory not in self.exclude_dirs]
            root_path = Path(root)
            for file_name in files:
                suffix = Path(file_name).suffix.lower()
                if suffix not in extensions:
                    continue
                file_path = (root_path / file_name).resolve()
                if not is_within_workspace(workspace_root, file_path):
                    continue
                yield file_path

    def _is_too_large(self, source_file: Path) -> bool:
        try:
            file_size = source_file.stat().st_size
        except OSError:
            return True
        return file_size > self.max_file_bytes

    def _parse_python_file(self, source_file: Path) -> tuple[set[str], set[str]] | None:
        try:
            source_text = source_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        try:
            tree = ast.parse(source_text, filename=str(source_file))
        except (SyntaxError, ValueError):
            return None

        visitor = _PythonSymbolVisitor()
        visitor.visit(tree)
        return visitor.definitions, visitor.calls


__all__ = ["SymbolGraph"]
