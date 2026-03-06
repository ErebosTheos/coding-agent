"""ProjectContextBuilder — generates a structured project context file from the
architecture plan, written to disk before code generation begins.

The context file (`project_context.json`) maps every planned file to:
  - its purpose
  - what it exports (from contract.public_api)
  - what it depends on and which names to import from each dependency
  - any API routes planned in its contract

This file is then injected into every LLM call (executor, frontend phase, healer)
so the model knows exactly what is available to import from every file.

Flow:
    architect produces Architecture
        → ProjectContextBuilder.build_from_architecture(arch, workspace)
        → writes project_context.json
        → executor reads it via .to_llm_context()
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .models import Architecture, ExecutionNode

log = logging.getLogger(__name__)

CONTEXT_FILENAME = "project_context.json"


class ProjectContextBuilder:

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self._manifest: dict[str, Any] = {}

    # ── Build from architecture plan (called before code gen) ────────────────

    def build_from_architecture(self, architecture: Architecture) -> None:
        """Build the context manifest from the architect's planned nodes and write to disk."""
        node_map: dict[str, ExecutionNode] = {n.node_id: n for n in architecture.nodes}

        for node in architecture.nodes:
            rel = node.file_path.replace("\\", "/")
            entry: dict[str, Any] = {"purpose": node.purpose}

            # Planned exports (from contract.public_api)
            if node.contract and node.contract.public_api:
                entry["exports"] = node.contract.public_api
            else:
                entry["exports"] = []

            # Build explicit import map: dependency file → names to import
            import_map: dict[str, list[str]] = {}
            for dep_id in node.depends_on:
                dep = node_map.get(dep_id)
                if not dep:
                    continue
                dep_exports = dep.contract.public_api if dep and dep.contract else []
                if dep_exports:
                    import_map[dep.file_path.replace("\\", "/")] = dep_exports

            if import_map:
                entry["import_from"] = import_map

            # Contract invariants as hints
            if node.contract and node.contract.invariants:
                entry["invariants"] = node.contract.invariants

            self._manifest[rel] = entry

        self._write()
        log.info("[ContextBuilder] Built context from architecture: %d nodes → %s",
                 len(architecture.nodes), CONTEXT_FILENAME)

    # ── Batch update from all generated files (post-generation refresh) ──────

    def build_from_generated_files(self, generated_files: list) -> None:
        """Scan all generated files and refresh exports in the context manifest.

        Call after execution completes so the healer / QA have accurate actual
        exports rather than the plan-time estimates from contract.public_api.
        """
        if not self._manifest:
            self._load()
        for gf in generated_files:
            if gf.content and gf.content.strip():
                self.update_from_file(gf.file_path, gf.content)
        log.info("[ContextBuilder] Refreshed context from %d generated files", len(generated_files))

    # ── Update a single file after it's written/healed ───────────────────────

    def update_from_file(self, file_path: str, content: str) -> None:
        """After a file is written, update its entry with actual exports scanned from content."""
        rel = file_path.replace("\\", "/")
        entry = self._manifest.get(rel, {})

        if file_path.endswith(".py"):
            actual_exports = _scan_python_exports(content)
            routes = _scan_routes(content)
            if actual_exports:
                entry["exports"] = actual_exports
            if routes:
                entry["routes"] = routes
        elif file_path.endswith((".js", ".ts", ".tsx")):
            actual_exports = _scan_js_exports(content)
            if actual_exports:
                entry["exports"] = actual_exports

        entry["lines"] = content.count("\n") + 1
        self._manifest[rel] = entry
        self._write()

    # ── LLM-ready string output ───────────────────────────────────────────────

    def to_llm_context(self, include_routes: bool = True) -> str:
        """Compact human-readable context string for injection into LLM prompts."""
        if not self._manifest and not self._load():
            return ""

        lines: list[str] = ["=== PROJECT STRUCTURE (source of truth for imports) ===\n"]

        for rel, info in self._manifest.items():
            exports = info.get("exports", [])
            import_from = info.get("import_from", {})
            purpose = info.get("purpose", "")
            routes = info.get("routes", [])

            lines.append(f"FILE: {rel}")
            if purpose:
                lines.append(f"  purpose: {purpose}")
            if exports:
                lines.append(f"  exports: {', '.join(exports)}")
            if import_from:
                for src_file, names in import_from.items():
                    lines.append(f"  imports from {src_file}: {', '.join(names)}")
            if routes and include_routes:
                lines.append(f"  routes: {', '.join(routes)}")
            lines.append("")

        if include_routes:
            all_routes = self.get_all_routes()
            if all_routes:
                lines.append("=== API ENDPOINTS ===")
                lines.extend(all_routes[:60])

        return "\n".join(lines)

    def get_all_routes(self) -> list[str]:
        if not self._manifest:
            self._load()
        routes: list[str] = []
        for info in self._manifest.values():
            routes.extend(info.get("routes", []))
        return routes

    def get_exports(self, file_path: str) -> list[str]:
        if not self._manifest:
            self._load()
        return self._manifest.get(file_path.replace("\\", "/"), {}).get("exports", [])

    # ── I/O helpers ──────────────────────────────────────────────────────────

    def _write(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        out = self.workspace / CONTEXT_FILENAME
        out.write_text(json.dumps(self._manifest, indent=2), encoding="utf-8")

    def _load(self) -> bool:
        ctx = self.workspace / CONTEXT_FILENAME
        if not ctx.exists():
            return False
        try:
            self._manifest = json.loads(ctx.read_text(encoding="utf-8"))
            return True
        except Exception:
            return False


# ── AST scanners for post-generation updates ─────────────────────────────────

def _scan_python_exports(content: str) -> list[str]:
    import ast as _ast
    try:
        tree = _ast.parse(content)
    except SyntaxError:
        return []
    exports: list[str] = []
    for node in _ast.iter_child_nodes(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            exports.append(node.name)
        elif isinstance(node, _ast.Assign):
            for t in node.targets:
                if isinstance(t, _ast.Name):
                    exports.append(t.id)
    return exports


def _scan_js_exports(content: str) -> list[str]:
    return re.findall(
        r'export\s+(?:default\s+)?(?:function|class|const|let|var)?\s*(\w+)',
        content,
    )


def _scan_routes(content: str) -> list[str]:
    return [
        f"{m.group(1).upper()} {m.group(2)}"
        for m in re.finditer(
            r'@(?:app|router)\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']',
            content, re.MULTILINE,
        )
    ]
