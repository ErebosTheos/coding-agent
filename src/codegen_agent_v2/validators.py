"""Deterministic post-generation validators.

These run AFTER LLM generation — no LLM calls, pure AST/regex checks.
They catch issues that prompts alone can't guarantee.

check_schema_imports  — finds schema classes imported but not defined
check_frontend_quality — finds HTML/CSS/JS files missing required structure
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from dataclasses import dataclass


@dataclass
class SchemaIssue:
    schema_path: str          # e.g. "src/schemas/course.py"
    missing_classes: set[str] # classes imported from this file but not defined


@dataclass
class FrontendIssue:
    file_path: str
    issues: list[str]


# ── Schema import validator ────────────────────────────────────────────────

def check_schema_imports(workspace: str) -> list[SchemaIssue]:
    """Scan every Python file for schema imports, verify all names exist.

    Walks src/ looking for `from ..schemas.X import A, B, C`.
    For each such import, checks that A, B, C are defined in src/schemas/X.py.
    Returns a list of SchemaIssue for every file with missing classes.
    """
    src_dir = Path(workspace) / "src"
    if not src_dir.exists():
        return []

    # required[schema_rel_path] = set of class names needed
    required: dict[str, set[str]] = {}

    for py_file in src_dir.rglob("*.py"):
        # Skip schema files themselves — they define, not import
        if "schemas" in py_file.parts:
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content)
        except Exception:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if "schemas" not in module:
                continue
            # Extract the schema module name: e.g. "..schemas.course" → "course"
            parts = module.split(".")
            try:
                idx = next(i for i, p in enumerate(parts) if p == "schemas")
                if idx + 1 >= len(parts):
                    continue
                schema_name = parts[idx + 1]
            except StopIteration:
                continue

            schema_path = f"src/schemas/{schema_name}.py"
            names = {alias.name for alias in node.names if alias.name != "*"}
            if names:
                required.setdefault(schema_path, set()).update(names)

    result: list[SchemaIssue] = []
    for schema_path, needed in required.items():
        full_path = Path(workspace) / schema_path
        defined: set[str] = set()
        if full_path.exists():
            try:
                tree = ast.parse(full_path.read_text(encoding="utf-8", errors="replace"))
                defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
            except Exception:
                pass
        missing = needed - defined
        if missing:
            result.append(SchemaIssue(schema_path=schema_path, missing_classes=missing))

    return result


# ── Frontend quality validator ─────────────────────────────────────────────

def check_frontend_quality(workspace: str, files: list) -> list[FrontendIssue]:
    """Check frontend files for required modular structure.

    Modular architecture: style.css holds ALL styles, app.js holds ALL JS logic.
    HTML files are pure structure — they just link to style.css and app.js.
    """
    result: list[FrontendIssue] = []

    for gf in files:
        path = gf.file_path
        content = gf.content
        issues: list[str] = []

        # ── HTML files ────────────────────────────────────────────────────
        if path.endswith(".html"):
            # Must link to shared CSS and JS
            if "style.css" not in content:
                issues.append("missing <link> to style.css")
            if "app.js" not in content:
                issues.append("missing <script src> pointing to app.js")
            # Must have real HTML structure
            if "<body" not in content:
                issues.append("missing <body> tag")
            if content.count("\n") < 60:
                issues.append(f"too short ({content.count(chr(10))} lines) for an HTML page")
            # Dashboard-specific checks
            if "dashboard" in path.lower():
                has_nav = any(kw in content.lower() for kw in ["sidebar", "nav", "menu"])
                if not has_nav:
                    issues.append("dashboard missing sidebar/nav structure in HTML")
                if content.count("\n") < 120:
                    issues.append(f"dashboard too short ({content.count(chr(10))} lines) — needs ≥120 lines of HTML structure")

        # ── style.css ─────────────────────────────────────────────────────
        elif path.endswith(".css"):
            if "--" not in content:
                issues.append("no CSS custom properties (--var) — design tokens missing")
            if ":root" not in content:
                issues.append("no :root block — CSS variables must be defined here")
            missing_components = []
            for component in ["sidebar", "button", "card", "table"]:
                if component not in content.lower():
                    missing_components.append(component)
            if missing_components:
                issues.append(f"missing CSS for: {', '.join(missing_components)}")
            if content.count("\n") < 200:
                issues.append(f"too short ({content.count(chr(10))} lines) — needs ≥200 lines")

        # ── app.js ────────────────────────────────────────────────────────
        elif path.endswith(".js") and "app" in path.lower():
            if "localStorage" not in content:
                issues.append("no localStorage — JWT token won't persist")
            if "Authorization" not in content:
                issues.append("no Authorization header — authenticated API calls will fail")
            if "fetch(" not in content:
                issues.append("no fetch() calls — no API communication")
            if "login" not in content.lower():
                issues.append("no login handler — users can't authenticate")
            if content.count("\n") < 100:
                issues.append(f"too short ({content.count(chr(10))} lines) — needs ≥100 lines")

        if issues:
            result.append(FrontendIssue(file_path=path, issues=issues))

    return result
