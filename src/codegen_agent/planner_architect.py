import json
import dataclasses
from .models import Plan, Feature, Architecture, ExecutionNode, Contract
from .llm.protocol import LLMClient
from .utils import find_json_in_text, extract_code_from_markdown

COMBINED_SYSTEM_PROMPT = """You are a Senior Software Architect.
Given a user request, return a single JSON object with exactly two keys: "plan" and "architecture".

"plan" schema:
{
  "project_name": string,
  "tech_stack": string,
  "features": [{"id": string, "title": string, "description": string, "priority": int}],
  "entry_point": string,
  "test_strategy": string
}

"architecture" schema:
{
  "file_tree": [string],
  "nodes": [{
    "node_id": string,
    "file_path": string,
    "purpose": string,
    "depends_on": [string],
    "contract": {"purpose": string, "inputs": [], "outputs": [], "public_api": ["ClassName", "function_name", "CONSTANT"], "invariants": []}
  }],
  "global_validation_commands": [string]
  // Shell commands that fully validate the project — must match the tech stack exactly.
  // Examples by stack:
  //   Python/pytest:   ["pytest tests/"]
  //   Django:          ["python manage.py test"]
  //   PHP/Laravel:     ["php artisan test"]
  //   PHP/PHPUnit:     ["./vendor/bin/phpunit"]
  //   Node/Jest:       ["npx jest"]
  //   Node/Mocha:      ["npx mocha"]
  //   Go:              ["go test ./..."]
  //   Rust:            ["cargo test"]
  //   Ruby/RSpec:      ["bundle exec rspec"]
  //   Ruby/Rails:      ["bin/rails test"]
  // Include ALL commands needed (lint + test). These are run verbatim in the project root.
}

ARCHITECTURE RULES:
- CRITICAL: For every node's contract.public_api, list the ACTUAL exported names (classes,
  functions, constants, types) that other files will import from this file. Do NOT leave
  public_api as an empty list — a populated public_api is the source of truth for import
  statements the executor will write. Example for a models file:
  "public_api": ["User", "Task", "Base", "get_db"]
- ALWAYS include the language dependency manifest as a node:
    Python   → requirements.txt
    Node/TS  → package.json  (include "scripts": {"start": ..., "test": ...})
    Go       → go.mod
    Rust     → Cargo.toml
    PHP      → composer.json
    Ruby     → Gemfile
  If the project has no dependencies yet, still include the manifest with minimal content.
- For web apps (FastAPI, Flask, Express, etc.) whose source lives inside a sub-directory,
  ALWAYS include a top-level entry point file (run.py for Python, index.js/server.js for Node)
  so users can start the server without knowing the internal package structure.
- Do NOT create directory placeholder nodes (e.g. node_id for "src/"). Only list real files.
- Python only: ALWAYS include an __init__.py node for every package directory
  (e.g. src/__init__.py, src/routers/__init__.py). These are required for imports to resolve.
- FastAPI + SQLAlchemy async projects:
  - Ensure session setup uses `async_sessionmaker(..., expire_on_commit=False)`.
  - API paths that serialize related ORM fields must plan eager-loading queries
    (e.g. `selectinload`) rather than relying on lazy loading at response time.
  - Avoid patterns that trigger runtime `MissingGreenlet` during response serialization.

PLAN RULES:
- For any project with a user-facing interface, the "features" list MUST include explicit frontend
  feature entries — not just backend features. Add entries like:
    {"id": "F-UI-1", "title": "Public Landing Page", "description": "Full landing page: hero, programs, stats, testimonials, contact form, footer.", "priority": 1}
    {"id": "F-UI-2", "title": "Authentication UI", "description": "Login page with role selector, error handling, redirect to dashboard.", "priority": 1}
    {"id": "F-UI-3", "title": "Student Dashboard UI", "description": "Sidebar layout: courses, tests, results, attendance, notifications.", "priority": 2}
    {"id": "F-UI-4", "title": "Teacher Dashboard UI", "description": "Lesson builder, test creator, grading panel, student performance table.", "priority": 2}
    {"id": "F-UI-5", "title": "Admin Dashboard UI", "description": "User management, analytics charts, audit logs, course management.", "priority": 2}
  Add whichever UI features are relevant. Frontend features must appear in the plan so the
  architect knows to include them as architecture nodes.

FRONTEND RULES (mandatory for any project with a user-facing interface):
- If the request mentions: website, portal, dashboard, frontend, UI, public page, login page,
  admin panel, CMS, landing page, or any visual user interface — you MUST generate a complete
  frontend. DO NOT generate a pure API with no HTML.
- For Python backends (FastAPI/Flask): serve the frontend via StaticFiles + Jinja2 templates OR
  a fully self-contained static/ folder. ALWAYS include:
    static/index.html      — public landing page with navigation, hero section, feature cards
    static/style.css       — complete responsive CSS (dark/light theme, mobile-first)
    static/app.js          — vanilla JS for interactivity (login modal, API calls, routing)
    static/dashboard.html  — authenticated dashboard (loads after login)
    templates/base.html    — optional Jinja2 base template if using server-side rendering
  AND wire them in main.py / app.py:
    app.mount("/static", StaticFiles(directory="static"), name="static")
    @app.get("/", response_class=HTMLResponse) → serve static/index.html
- For Node/Express backends: serve static/ with express.static() and include the same files.
- The HTML must be real, complete, and production-quality — NOT a placeholder or stub.
  Include: navbar, hero section, features/cards section, login form, footer.
  Use semantic HTML with ARIA labels. Link to style.css and app.js.
- app.js must implement: JWT auth flow (login → store token → show dashboard),
  fetch wrappers for API calls, navigation between pages, error handling.
- style.css must implement: CSS custom properties, responsive grid/flex layout,
  dark theme, button/form/card styles, accessibility focus indicators.
- If the spec mentions WCAG / accessibility: add skip-to-content link, proper ARIA roles,
  high-contrast mode toggle (data-theme attribute), keyboard navigation support.
- NEVER skip the frontend for a website project. A backend-only submission for a website
  brief is an incomplete, failing deliverable.
- The frontend must be production-quality, visually stunning, and feel like a real SaaS product.
  NOT a tutorial demo. Think Stripe, Linear, Vercel-level polish.

Respond ONLY with the raw JSON object. No markdown fences, no commentary."""

COMBINED_USER_PROMPT = """User Request: {prompt}

IMPORTANT — BE AMBITIOUS AND THOROUGH:
- Do NOT produce a minimal feature set. Extract EVERY possible feature implied by the request.
- If the request mentions a dashboard, plan ALL the panels (stats, tables, charts, forms, filters).
- If the request mentions users/roles, plan ALL CRUD operations, profile pages, settings per role.
- If the request mentions courses/content, plan lesson viewer, progress tracking, certificates, bookmarks.
- If the request mentions tests/assessments, plan question types, timer, auto-submit, result breakdown, leaderboard.
- If the request mentions analytics, plan charts, export, filters, date ranges.
- Aim for 8-15 backend features and 5-8 frontend UI features MINIMUM. More is always better.
- Every role gets its own dashboard HTML page as a planned architecture node.
- NEVER merge multiple routers into one file — each resource gets its own router file.
- NEVER skip seed files, config files, utility modules, middleware, or migration scripts.

MANDATORY FRONTEND ARCHITECTURE:
For ANY project with a UI, the architecture MUST include ALL of these nodes:
  static/index.html     — public landing page (hero, features, stats, testimonials, footer)
  static/login.html     — login page with role selector and JWT auth
  static/style.css      — complete design system (300+ lines, dark theme, all components)
  static/app.js         — all interactivity (auth flow, API calls, charts, tables, 200+ lines)
  static/dashboard.html — main authenticated dashboard
  AND separate dashboard pages for each role mentioned (student, teacher, admin, staff, parent).

The frontend plan features MUST include explicit entries:
  F-UI-1: Public Landing Page, F-UI-2: Authentication UI, F-UI-3..N: each role's dashboard.

Produce the project plan and full file architecture in a single JSON response."""


class PlannerArchitect:
    """Combines the Planner and Architect into a single LLM call, saving one full round-trip."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def plan_and_architect(self, prompt: str) -> tuple[Plan, Architecture]:
        user_prompt = COMBINED_USER_PROMPT.format(prompt=prompt)
        response = await self.llm_client.generate(user_prompt, system_prompt=COMBINED_SYSTEM_PROMPT)

        # Try JSON block extraction first, then raw parse
        json_blocks = extract_code_from_markdown(response, "json")
        try:
            data = json.loads(json_blocks[0]) if json_blocks else (find_json_in_text(response) or json.loads(response))
        except (json.JSONDecodeError, TypeError):
            raise ValueError(f"PlannerArchitect: failed to parse combined response: {response[:500]}")

        plan = self._parse_plan(data.get("plan", data))
        architecture = self._parse_architecture(data.get("architecture", data))
        plan = self._inject_missing_frontend_features(plan, architecture)
        return plan, architecture

    @staticmethod
    def _parse_plan(d: dict) -> Plan:
        features = [Feature(**f) for f in d.get("features", [])]
        return Plan(
            project_name=d["project_name"],
            tech_stack=d["tech_stack"],
            features=features,
            entry_point=d["entry_point"],
            test_strategy=d["test_strategy"],
        )

    @staticmethod
    def _parse_architecture(d: dict) -> Architecture:
        nodes = []
        for n in d.get("nodes", []):
            contract = Contract(**n["contract"]) if n.get("contract") else None
            nodes.append(ExecutionNode(
                node_id=n["node_id"],
                file_path=n["file_path"],
                purpose=n["purpose"],
                depends_on=n.get("depends_on", []),
                contract=contract,
            ))

        # Synthesize nodes for any file_tree entry the LLM forgot to plan.
        # Common omissions: __init__.py, requirements.txt, *.ini, *.toml, README.md
        covered = {n.file_path for n in nodes}
        for path in d.get("file_tree", []):
            if path in covered:
                continue
            import os as _os
            name = _os.path.basename(path)
            purpose = (
                "Empty Python package marker" if name == "__init__.py" else
                "Project dependencies list" if name == "requirements.txt" else
                "Project configuration" if name.endswith((".toml", ".ini", ".cfg")) else
                "Project documentation" if name.endswith(".md") else
                f"Supporting file: {name}"
            )
            synthetic_id = path.replace("/", "_").replace(".", "_")
            nodes.append(ExecutionNode(
                node_id=synthetic_id,
                file_path=path,
                purpose=purpose,
                depends_on=[],
            ))

        return Architecture(
            file_tree=d["file_tree"],
            nodes=nodes,
            global_validation_commands=d.get("global_validation_commands", []),
        )

    @staticmethod
    def _inject_missing_frontend_features(plan: Plan, architecture: Architecture) -> Plan:
        """If the LLM omitted frontend features from the plan but planned frontend files,
        synthesize Feature entries so the UI and docs reflect the full scope."""
        existing_lower = {f.title.lower() for f in plan.features}
        has_frontend = any(
            k in existing_lower for k in ("ui", "frontend", "landing", "dashboard ui", "portal", "html", "css")
        )
        if has_frontend:
            return plan

        # Check architecture nodes for frontend files
        frontend_nodes = [
            n for n in (architecture.nodes or [])
            if any(n.file_path.endswith(ext) for ext in (".html", ".css", ".js", ".ts", ".jsx", ".tsx"))
            or "static/" in n.file_path or "templates/" in n.file_path
        ]
        if not frontend_nodes:
            return plan

        # Synthesize feature entries from the actual frontend files planned
        _MAP = [
            (("index.html", "templates/index"), "F-UI-1", "Public Landing Page", "Full landing page: hero section, feature cards, navigation, footer."),
            (("login", "auth"), "F-UI-2", "Authentication UI", "Login page with role selector, JWT auth flow, error handling, redirect to dashboard."),
            (("student",), "F-UI-3", "Student Dashboard UI", "Student sidebar: courses, tests, results, progress tracking, notifications."),
            (("teacher", "instructor"), "F-UI-4", "Teacher Dashboard UI", "Lesson builder, test creator, grading panel, student performance table."),
            (("admin",), "F-UI-5", "Admin Control Panel UI", "User management, analytics charts, audit logs, course management."),
            (("staff",), "F-UI-6", "Staff Dashboard UI", "Staff-specific panels and workflows."),
            (("style.css", "theme"), "F-UI-7", "Responsive CSS Design System", "Mobile-first CSS with dark theme, grid layout, accessible components."),
            (("app.js", "main.js"), "F-UI-8", "Frontend JavaScript", "JWT auth, API fetch wrappers, navigation, real-time updates."),
        ]
        next_priority = max((f.priority for f in plan.features), default=2)
        synth_features = list(plan.features)
        seen_ids = {f.id for f in plan.features}
        for keywords, fid, title, desc in _MAP:
            if fid in seen_ids:
                continue
            if any(any(k in n.file_path.lower() for k in keywords) for n in frontend_nodes):
                synth_features.append(Feature(id=fid, title=title, description=desc, priority=next_priority))
                seen_ids.add(fid)

        return dataclasses.replace(plan, features=synth_features)
