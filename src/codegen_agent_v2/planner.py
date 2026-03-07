"""V2 Planner — single LLM call that produces manifest + layered file plan."""
from __future__ import annotations

import json
import re
from typing import Any

from ..codegen_agent.llm.protocol import LLMClient
from ..codegen_agent.utils import find_json_in_text
from .models import FileSpec, LayeredPlan
from .manifest import parse_manifest

# ── Layer assignment guide (injected into prompt) ─────────────────────────

LAYER_GUIDE = """
LAYER ASSIGNMENT RULES — assign every file a layer integer 1-6:

Layer 1 — Foundation (no deps):
  requirements.txt, pyproject.toml, pytest.ini, alembic.ini,
  src/__init__.py, src/*/_{_}init_{_}_.py (all __init__ files), src/config.py

Layer 2 — Models (depends on Layer 1):
  src/models/*.py, src/database.py, alembic/env.py, alembic/versions/*.py

Layer 3 — Schemas (depends on Layer 2):
  src/schemas/*.py

Layer 4 — Core & Services (depends on Layers 2-3):
  src/core/*.py, src/services/*.py, src/repositories/*.py, src/utils/*.py,
  src/middleware/*.py

Layer 5 — API & Entry (depends on Layers 2-4):
  src/api/**/*.py, src/main.py, run.py

Layer 6 — Frontend & Tests (depends on Layers 1-5):
  static/*.html, static/*.css, static/*.js, tests/*.py,
  tests/**/*.py, seed.py, conftest.py
""".replace("_{_}init_{_}_", "__init__")

SYSTEM_PROMPT = """You are a Senior Software Architect. Given a user request, produce a complete
project manifest and layered file plan in a single JSON response.

The manifest is the PROJECT SOURCE OF TRUTH — every file will be generated from it.
Think carefully: define every model column, every schema field, every route.
Incomplete or vague manifest entries cause cascading import errors.

OUTPUT FORMAT — respond with ONLY this JSON, no markdown fences:
{
  "manifest": {
    "project_name": "string",
    "stack": "fastapi",
    "api_prefix": "/api/v1",
    "auth": {
      "sub_field": "email",
      "login_endpoint": "/api/v1/auth/login",
      "token_type": "bearer"
    },
    "models": {
      "User": {
        "table": "users",
        "columns": {
          "id": "Integer, primary_key=True, autoincrement=True",
          "email": "String(255), unique=True, nullable=False, index=True",
          "hashed_password": "String(255), nullable=False",
          "is_active": "Boolean, default=True",
          "created_at": "DateTime, default=func.now()"
        }
      }
    },
    "schemas": {
      "UserCreate": {"email": "str", "password": "str"},
      "UserResponse": {"id": "int", "email": "str", "is_active": "bool"}
    },
    "routes": [
      {"method": "POST", "path": "/api/v1/auth/login", "auth": false, "summary": "Login"},
      {"method": "GET",  "path": "/api/v1/users/me",   "auth": true,  "summary": "Get profile"}
    ],
    "db_url_default": "sqlite+aiosqlite:///./app.db",
    "accessibility_required": false,
    "modules": ["auth", "users"]
  },
  "files": [
    {
      "file_path": "requirements.txt",
      "purpose": "Python dependencies",
      "layer": 1,
      "priority": "low",
      "exports": [],
      "depends_on": []
    },
    {
      "file_path": "src/models/user.py",
      "purpose": "User SQLAlchemy ORM model",
      "layer": 2,
      "priority": "medium",
      "exports": ["User", "Base"],
      "depends_on": ["src/__init__.py"]
    }
  ],
  "validation_commands": ["pytest tests/ -q --tb=short"]
}

MANIFEST RULES:
- Define EVERY model mentioned in the brief with ALL columns (id, timestamps, FKs, status fields).
- Define request AND response schemas for every resource — BOTH XxxCreate AND XxxResponse for EVERY model.
- Every schema referenced in a route MUST be defined in the manifest. Never reference a schema that isn't listed.
- List every API route including auth, CRUD, and any special endpoints.
- auth.sub_field MUST match a column in the User model (e.g. if sub_field="email", User must have email column).
- SCHEMA COMPLETENESS: for every model Foo, define FooCreate (input fields) and FooResponse (all fields + id + timestamps). Missing schemas cause import errors.
- modules: list every distinct feature module (e.g. ["auth", "courses", "reporting", "notifications"]). Each module must be fully implemented.
- accessibility_required: set to true if the brief mentions accessibility, WCAG, a11y, screen reader, or disabled users.

FILE RULES:
- NEVER create directory nodes — only real files.
- ALWAYS include __init__.py for every package directory.
- File count: as many as the project genuinely needs (up to 100 files). Never merge files just to reduce count.
- One router file per resource, EXCEPT: closely related small resources may share a router (e.g. Question+Answer in assessments_router.py).
- RESOURCE COMPLETENESS: for every resource named in the brief, include model + schema + router files.
- Never generate a UI panel for a feature without its backend model + router.
- For large projects (>10 resources): merge models into domain files (e.g. src/models/course.py holds Course+Module+Lesson). Merge schemas similarly. Never merge routers.
- Never sacrifice completeness for file count. A missing page or feature is worse than a large file list.

PAGE SEPARATION RULES (critical for frontend correctness):
- PUBLIC pages (no login): index/landing page, login page, registration page, public info pages.
- AUTHENTICATED pages: create one dashboard HTML per role (e.g. dashboard_admin.html, dashboard_student.html).
- NEVER mix public and authenticated content in the same HTML file.
- List public pages first in the file plan, then authenticated pages per role.
- Login page redirects to the role-specific dashboard after successful authentication.

FILE PRIORITY RULES — assign every file a "priority" based on its complexity and criticality:
- "low"    : Files with little or no real logic. Package markers, config declarations, dependency lists,
             migration scaffolding, simple entry points. Generated all together in one bulk call.
- "medium" : Files with moderate logic that share patterns with siblings. Data models, Pydantic schemas,
             service/repository classes, utility helpers, test files. Generated in batches of 4 per call.
- "high"   : Files that are unique, complex, or load-bearing. App entrypoint, database engine setup,
             every API router (each has distinct business logic), all frontend files (UI, styles, scripts).
             Each gets its own dedicated LLM call for maximum quality and attention.
Think about each file individually — assign priority based on its actual role in THIS project.

""" + LAYER_GUIDE + """

FRONTEND RULES:
- For any project with a UI: include static/index.html, static/login.html, static/style.css, static/app.js.
- Add one dashboard HTML per distinct user role mentioned (e.g. static/dashboard_admin.html).
- Frontend is always Layer 6.
- static/app.js handles ALL API calls: login (saves JWT to localStorage), and authenticated fetch() with Authorization header.
- Each dashboard HTML is self-contained: sidebar nav, topbar, content panels with real data tables/cards.
- static/style.css defines CSS variables, layout classes for sidebar+main, cards, tables, buttons — used by ALL pages.

PYDANTIC V2 RULE — mandatory for ALL files:
- NEVER use class-based Config (class Config: ...) — it is deprecated in Pydantic v2 and causes test failures.
- ALWAYS use model_config = ConfigDict(...) instead.
- For BaseSettings: model_config = ConfigDict(env_file=".env", extra="ignore")
- For BaseModel response schemas: model_config = ConfigDict(from_attributes=True)

REQUIREMENTS.TXT RULE:
- Use >= pins: fastapi>=0.115, uvicorn[standard]>=0.30, sqlalchemy>=2.0, alembic>=1.13,
  pydantic>=2.10, pydantic-settings>=2.5, python-jose[cryptography]>=3.3,
  bcrypt>=4.1, httpx>=0.27, pytest>=8.0, pytest-asyncio>=0.23, anyio>=4.4,
  python-multipart>=0.0.9, aiosqlite>=0.20, email-validator>=2.1, greenlet>=3.0
- ALWAYS include python-multipart (required by FastAPI for any Form/File endpoint).
- ALWAYS include aiosqlite (required for SQLite async engine).
- ALWAYS include greenlet (required by SQLAlchemy async).

Respond ONLY with the raw JSON. No commentary, no markdown fences."""

USER_PROMPT = """User Request: {brief}

Be thorough: extract EVERY resource named in the request and give each a model + schema + router.
Remember: separate PUBLIC pages from AUTHENTICATED pages. List modules in the manifest.
Produce the manifest and file plan now."""

# ── Module extractor (pre-processing for complex briefs) ──────────────────

_MODULE_EXTRACTOR_SYSTEM = """Extract the distinct feature modules from this software project brief.
Output ONLY a JSON array of short module names (1-3 words each).
Example: ["auth", "user management", "courses", "assessments", "reporting", "notifications"]
Extract between 3 and 12 modules. No commentary, no fences — just the raw JSON array."""


class PlannerV2:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def plan(self, brief: str, file_tree: str = "") -> LayeredPlan:
        # Pre-processing: identify modules for complex briefs
        modules: list[str] = []
        if len(brief) > 400:
            modules = await self._extract_modules(brief)

        user_prompt = USER_PROMPT.format(brief=brief)

        if modules:
            user_prompt += (
                "\n\nPRE-IDENTIFIED MODULES — plan files for ALL of these, none may be skipped:\n"
                + "\n".join(f"  • {m}" for m in modules)
            )

        if file_tree:
            user_prompt += (
                "\n\nPRESCRIBED FILE STRUCTURE (from the project brief) — "
                "your file plan MUST include every file listed here. "
                "Use the exact file paths shown:\n\n"
                + file_tree
            )

        raw_text = await self.llm.generate(user_prompt, system_prompt=SYSTEM_PROMPT)
        return self._parse(raw_text)

    async def _extract_modules(self, brief: str) -> list[str]:
        """Quick pre-call: extract distinct feature modules from the brief."""
        try:
            raw = await self.llm.generate(brief[:3000], system_prompt=_MODULE_EXTRACTOR_SYSTEM)
            extracted = find_json_in_text(raw)
            if isinstance(extracted, list):
                return [str(m).strip() for m in extracted[:12] if str(m).strip()]
        except Exception:
            pass
        return []

    def _parse(self, text: str) -> LayeredPlan:
        # find_json_in_text returns a parsed dict directly — use it if available
        extracted = find_json_in_text(text)
        if isinstance(extracted, dict):
            data = extracted
        else:
            # Fall back to string-based extraction
            json_text = text.strip()
            json_text = re.sub(r'^```[a-z]*\n?', '', json_text, flags=re.MULTILINE)
            json_text = re.sub(r'\n?```$', '', json_text, flags=re.MULTILINE)
            try:
                data = json.loads(json_text)
            except json.JSONDecodeError:
                m = re.search(r'\{[\s\S]+\}', json_text)
                if m:
                    data = json.loads(m.group(0))
                else:
                    raise ValueError(f"Planner returned non-JSON output: {text[:200]}")

        manifest_raw = data.get("manifest", data)
        manifest = parse_manifest(manifest_raw)

        files: list[FileSpec] = []
        seen_paths: set[str] = set()
        for f in data.get("files", []):
            path = f.get("file_path", "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            priority = f.get("priority") or "medium"
            files.append(FileSpec(
                file_path=path,
                purpose=f.get("purpose", ""),
                layer=int(f.get("layer", 1)),
                exports=f.get("exports", []),
                depends_on=f.get("depends_on", []),
                priority=priority,
            ))

        validation_commands: list[str] = data.get("validation_commands", ["pytest tests/ -q --tb=short"])

        return LayeredPlan(
            manifest=manifest,
            files=files,
            validation_commands=validation_commands,
        )
