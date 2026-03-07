"""Manifest — project source of truth.

Three responsibilities:
1. Parse the manifest from the planner's JSON output.
2. Update it from disk after Layer 2 (models) to lock in real column names.
3. Render it as a prompt constraint block injected into every LLM call.
"""
from __future__ import annotations

import ast
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .models import (
    ManifestAuth,
    ManifestModel,
    ManifestRoute,
    ManifestSchema,
    ProjectManifest,
)


# ── Parse from planner JSON ───────────────────────────────────────────────

def parse_manifest(raw: dict[str, Any]) -> ProjectManifest:
    """Build a ProjectManifest from the planner's raw JSON dict."""
    auth_raw = raw.get("auth", {})
    auth = ManifestAuth(
        sub_field=auth_raw.get("sub_field", "email"),
        login_endpoint=auth_raw.get("login_endpoint", "/api/v1/auth/login"),
        token_type=auth_raw.get("token_type", "bearer"),
    )

    models: dict[str, ManifestModel] = {}
    for class_name, m in raw.get("models", {}).items():
        models[class_name] = ManifestModel(
            class_name=class_name,
            table_name=m.get("table", class_name.lower() + "s"),
            columns=m.get("columns", {}),
        )

    schemas: dict[str, ManifestSchema] = {}
    for class_name, s in raw.get("schemas", {}).items():
        schemas[class_name] = ManifestSchema(
            class_name=class_name,
            fields=s if isinstance(s, dict) else s.get("fields", {}),
        )

    routes: list[ManifestRoute] = []
    for r in raw.get("routes", []):
        routes.append(ManifestRoute(
            method=r.get("method", "GET").upper(),
            path=r.get("path", "/"),
            auth_required=r.get("auth", r.get("auth_required", False)),
            summary=r.get("summary", ""),
        ))

    return ProjectManifest(
        project_name=raw.get("project_name", "project"),
        stack=raw.get("stack", "fastapi"),
        api_prefix=raw.get("api_prefix", "/api/v1"),
        auth=auth,
        models=models,
        schemas=schemas,
        routes=routes,
        db_url_default=raw.get("db_url_default", "sqlite+aiosqlite:///./app.db"),
        accessibility_required=bool(raw.get("accessibility_required", False)),
        modules=list(raw.get("modules", [])),
    )


# ── Update from disk after Layer 2 ───────────────────────────────────────

def update_from_disk(manifest: ProjectManifest, workspace: str) -> ProjectManifest:
    """AST-scan written model files and update manifest columns to ground truth.

    After Layer 2 executes, the ORM models are on disk. We scan them to lock in
    the real column names and types so every subsequent layer uses exact names.
    """
    updated_models: dict[str, ManifestModel] = dict(manifest.models)

    src_dir = Path(workspace)
    py_files = list(src_dir.rglob("*.py"))

    for py_file in py_files:
        # Only scan model files
        if not any(p in str(py_file) for p in ("model", "Model")):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except Exception:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            # Detect ORM class: has __tablename__
            tablename: str | None = None
            cols: dict[str, str] = {}
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if isinstance(t, ast.Name) and t.id == "__tablename__":
                            if isinstance(stmt.value, ast.Constant):
                                tablename = stmt.value.value
                        # Column assignments
                        if (
                            isinstance(t, ast.Name)
                            and not t.id.startswith("_")
                            and isinstance(stmt.value, ast.Call)
                        ):
                            func = stmt.value.func
                            func_name = (
                                func.id if isinstance(func, ast.Name)
                                else func.attr if isinstance(func, ast.Attribute)
                                else ""
                            )
                            if func_name in ("Column", "mapped_column") and stmt.value.args:
                                col_type = ast.unparse(stmt.value.args[0])
                                cols[t.id] = col_type

            if tablename and cols:
                updated_models[node.name] = ManifestModel(
                    class_name=node.name,
                    table_name=tablename,
                    columns=cols,
                )

    return replace(manifest, models=updated_models)


# ── Render constraint block ───────────────────────────────────────────────

def render_constraint_block(manifest: ProjectManifest) -> str:
    """Render the manifest as a strict constraint block for LLM prompts.

    This block is prepended to every file generation prompt so the LLM
    cannot deviate from the planned models, schemas, routes, or auth convention.
    """
    lines = [
        "=" * 72,
        "PROJECT SOURCE OF TRUTH — DO NOT DEVIATE FROM ANY DEFINITION BELOW",
        f"Project: {manifest.project_name}  |  Stack: {manifest.stack}  |  API prefix: {manifest.api_prefix}",
        "=" * 72,
    ]

    # Auth
    lines += [
        "",
        f"AUTH CONVENTION:",
        f"  JWT sub claim  : {manifest.auth.sub_field}   ← set at login, read at every authenticated endpoint",
        f"  Login endpoint : {manifest.auth.login_endpoint}",
        f"  Token type     : {manifest.auth.token_type}",
        f"  RULE: get_current_user MUST query by {manifest.auth.sub_field}. Never mix email/username lookups.",
    ]

    # Models
    if manifest.models:
        lines += ["", "ORM MODELS (SQLAlchemy — exact column names and types):"]
        for cls, m in manifest.models.items():
            lines.append(f"  class {cls}:  # table='{m.table_name}'")
            for col, typ in m.columns.items():
                lines.append(f"    {col} = Column({typ})")
        lines += [
            "  RULES:",
            "  • NEVER reference a column not listed above.",
            "  • NEVER change a column's type.",
            "  • NEVER add columns not in this list without also updating schemas.",
        ]

    # Schemas
    if manifest.schemas:
        lines += ["", "PYDANTIC SCHEMAS (exact field names and Python types):"]
        for cls, s in manifest.schemas.items():
            lines.append(f"  class {cls}(BaseModel):")
            for fname, ftype in s.fields.items():
                lines.append(f"    {fname}: {ftype}")
        lines += [
            "  RULES:",
            "  • Schema field names MUST match ORM column names exactly.",
            "  • No invented fields. No type mismatches.",
        ]

    # Routes
    if manifest.routes:
        lines += ["", "API ROUTES (every route that must exist — method, path, auth):"]
        for r in manifest.routes:
            auth_tag = "[AUTH]" if r.auth_required else "[PUBLIC]"
            lines.append(f"  {r.method:<6} {r.path:<45} {auth_tag}  {r.summary}")
        lines += [
            "  RULES:",
            "  • Route paths MUST match exactly — no extra/missing prefixes.",
            f"  • All paths start with {manifest.api_prefix}.",
        ]

    # Modules
    if manifest.modules:
        lines += [
            "",
            f"PLANNED MODULES ({len(manifest.modules)} total — implement ALL of them):",
            *[f"  • {m}" for m in manifest.modules],
            "  Every module must have its own model, schema, and router files.",
            "  Do not merge modules or skip any.",
        ]

    # Accessibility
    if manifest.accessibility_required:
        lines += [
            "",
            "ACCESSIBILITY REQUIREMENTS (WCAG 2.1 AA — mandatory for this project):",
            "  HTML: Every <img> has alt. Every <input> has <label>. Every <button> has text or aria-label.",
            "  HTML: Semantic elements — <nav>, <main>, <header>, <footer>, <section>, <article>.",
            "  HTML: Skip-to-content link at top of every page: <a href='#main' class='skip-link'>Skip to main content</a>.",
            "  HTML: ARIA roles and aria-label on all interactive components.",
            "  CSS: Focus indicators visible — :focus { outline: 2px solid var(--primary); outline-offset: 2px; }",
            "  CSS: Colour contrast ratio ≥ 4.5:1 for body text, ≥ 3:1 for large text and UI components.",
            "  CSS: .skip-link positioned off-screen by default, visible on :focus.",
            "  JS: Announce dynamic content changes via aria-live='polite' regions.",
            "  JS: Full keyboard navigation — Tab/Shift-Tab/Enter/Escape for all modals, dropdowns, menus.",
        ]

    # Import path rule
    lines += [
        "",
        "IMPORT PATH RULE:",
        "  Derive Python import from file path: replace '/' with '.', drop '.py'.",
        "  src/api/routers/auth.py → from .api.routers import auth  (relative from src/)",
        "  src/models/user.py → from ..models.user import User  (from src/api/routers/)",
        "  Count dots carefully. Never guess — derive mechanically from the file tree.",
        "",
        "PACKAGE VERSIONS (use >= pins, not == pins):",
        "  fastapi>=0.115  uvicorn[standard]>=0.30  sqlalchemy>=2.0  alembic>=1.13",
        "  pydantic>=2.10  pydantic-settings>=2.5  python-jose[cryptography]>=3.3",
        "  bcrypt>=4.1  httpx>=0.27  pytest>=8.0  pytest-asyncio>=0.23  anyio>=4.4",
        "  aiosqlite>=0.20  email-validator>=2.1  python-multipart>=0.0.9  greenlet>=3.0",
        "  python-multipart is MANDATORY — FastAPI requires it for any Form/File endpoint.",
        "  greenlet is MANDATORY — SQLAlchemy async requires it.",
        "  aiosqlite is MANDATORY — required for SQLite async engine.",
        "  (add asyncpg>=0.29 only if using PostgreSQL)",
        "",
        "ASYNC DB RULES:",
        "  Use AsyncSession + async_sessionmaker (NOT sessionmaker).",
        "  Engine: create_async_engine(DATABASE_URL, echo=False).",
        "  Always await session.execute(), session.commit(), session.refresh().",
        "  get_db: AsyncGenerator[AsyncSession, None] — yield only, no commit.",
        "",
        "SECRET_KEY: os.getenv('SECRET_KEY', 'dev-secret-change-in-production')",
        "get_db: yield session — NO commit in get_db. Endpoints commit themselves.",
        "",
        "PYDANTIC V2 — FORBIDDEN PATTERN (causes DeprecationWarning → test failure):",
        "  NEVER write:  class Config:  env_file = ...  ← Pydantic v1, deprecated",
        "  ALWAYS write: model_config = ConfigDict(env_file='.env', extra='ignore')",
        "  Import:       from pydantic import ConfigDict",
        "  ORM models:   model_config = ConfigDict(from_attributes=True)  in response schemas",
        "=" * 72,
        "",
    ]

    return "\n".join(lines)


# ── Persist to disk ───────────────────────────────────────────────────────

def save(manifest: ProjectManifest, workspace: str) -> None:
    """Write project_manifest.json to the workspace root."""
    path = Path(workspace) / "project_manifest.json"
    data = {
        "project_name": manifest.project_name,
        "stack": manifest.stack,
        "api_prefix": manifest.api_prefix,
        "auth": {
            "sub_field": manifest.auth.sub_field,
            "login_endpoint": manifest.auth.login_endpoint,
            "token_type": manifest.auth.token_type,
        },
        "models": {
            cls: {"table": m.table_name, "columns": m.columns}
            for cls, m in manifest.models.items()
        },
        "schemas": {
            cls: s.fields
            for cls, s in manifest.schemas.items()
        },
        "routes": [
            {"method": r.method, "path": r.path, "auth": r.auth_required, "summary": r.summary}
            for r in manifest.routes
        ],
        "db_url_default": manifest.db_url_default,
        "accessibility_required": manifest.accessibility_required,
        "modules": manifest.modules,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(workspace: str) -> ProjectManifest | None:
    """Load manifest from workspace if it exists."""
    path = Path(workspace) / "project_manifest.json"
    if not path.exists():
        return None
    try:
        return parse_manifest(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None
