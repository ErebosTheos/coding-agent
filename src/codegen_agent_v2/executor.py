"""V2 Executor — layer-gated, manifest-driven, parallel within layers.

Execution model:
  For each layer 1-6:
    1. Collect files assigned to this layer
    2. Sort into dependency waves (topological)
    3. For each wave: stream-bulk generate all files in one LLM call
    4. Run guards on each file (pre-write); queue failures for individual retry
    5. Write passing files to disk
    6. Retry failed files individually (up to MAX_RETRIES each)
    7. Validate the full layer
    8. On validation failure: run healer (up to layer_def.max_heal_rounds)
    9. If still failing: LayerGateError (hard_stop) or NeedsReviewError (needs_review)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import AsyncIterator, Callable, Any

from ..codegen_agent.llm.protocol import LLMClient
from ..codegen_agent.utils import find_json_in_text, ensure_directory, prune_prompt
from .guards import run_all as run_guards
from .manifest import render_constraint_block, update_from_disk
from .models import (
    FileSpec, GeneratedFile, LayerDef, LayerResult,
    LayerGateError, NeedsReviewError, ProjectManifest, LAYER_DEFS,
)

MAX_FILE_RETRIES = 2
_EMIT = Callable[[str, dict], Any]  # emit(event_type, data)


# ── Prompts ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Senior Software Engineer implementing production-quality source code.

ANTI-TRUNCATION MANDATE (enforced — truncated output is discarded and retried):
- Write EVERY file completely. Never end mid-function, mid-class, or mid-string.
- If low on tokens: FINISH the current file before starting the next JSON key.
- pass is forbidden in production code. Every function must have a real body.
- Minimum lines: HTML≥80, CSS≥150, JS≥80, Python≥15 (non-init).
- Dashboard HTML files must be ≥150 lines — full sidebar, header, data tables, modals.

FRONTEND QUALITY MANDATE (HTML/CSS/JS files):
- Design must look PROFESSIONAL: clean layout, consistent color scheme, proper spacing.
- Use a sidebar + topbar layout for dashboards. Never render a bare list of links.
- CSS: use CSS variables for colors, flexbox/grid for layout, hover states on all buttons.
- Every dashboard must show: sidebar nav, header with user info, main content area with cards/tables.
- JS: use async/await for all API calls. Store JWT in localStorage. Include Authorization header on every authenticated request.
- ALWAYS handle API errors gracefully: show a user-friendly message, never just "Failed to load X".
- API calls must read the JWT token: const token = localStorage.getItem('token'); then fetch(url, {headers: {Authorization: 'Bearer ' + token}}).
- Login page: on success, save token to localStorage and redirect to the correct dashboard based on user role.

IMPORT STYLE — derive from file path, count dots carefully:
- src/api/routers/auth.py importing src/database.py → from ...database import get_db
- src/services/user.py importing src/models/user.py → from ..models.user import User
- src/main.py importing src/api/routers/auth.py → from .api.routers import auth
- src/api/auth_router.py importing src/core/security.py → from ..core.security import ...
- NEVER guess — derive the relative path mechanically from the file tree.

__init__.py RULES:
- ALL __init__.py files MUST be empty (just a blank file or a single comment).
- NEVER import routers, models, or services inside __init__.py.
- __init__.py is only a package marker, nothing else.

SCHEMA COMPLETENESS RULES:
- Every schema file MUST define ALL classes that any router imports from it.
- For every model Foo: define FooCreate, FooUpdate (optional), AND FooResponse.
- FooResponse MUST include id and all timestamp fields.
- NEVER import a schema class that isn't defined in the same file.
- If a router does `from ..schemas.course import CourseResponse, ModuleResponse` — both MUST exist in schemas/course.py.

FASTAPI RULES:
- define app = FastAPI(...) BEFORE any app.include_router() calls.
- StaticFiles: directory=str(Path(__file__).parent / "static") — never hardcode "static".
- Use lifespan context manager, not @app.on_event.
- get_db: yield session only — NO commit. Endpoints commit themselves.
- bcrypt for passwords (not passlib): bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
- SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")

PYDANTIC V2 RULES (critical — class-based Config is DEPRECATED and will cause test warnings):
- NEVER use class Config inside a BaseSettings or BaseModel class.
- ALWAYS use model_config = ConfigDict(...) at class level instead.
- Example for BaseSettings:
    from pydantic_settings import BaseSettings
    from pydantic import ConfigDict
    class Settings(BaseSettings):
        model_config = ConfigDict(env_file=".env", extra="ignore")
        DATABASE_URL: str = "sqlite+aiosqlite:///./app.db"
- Example for BaseModel:
    from pydantic import BaseModel, ConfigDict
    class UserResponse(BaseModel):
        model_config = ConfigDict(from_attributes=True)
        id: int
        email: str
- NEVER write: class Config: env_file = ".env" — this is Pydantic v1 syntax, FORBIDDEN.

Output ONLY a JSON object: {"file_path": "complete_file_content", ...}
No markdown fences, no commentary, no reasoning text before the JSON."""

# ── Dedicated frontend system prompt ───────────────────────────────────────

FRONTEND_SYSTEM_PROMPT = """You are an expert Senior Frontend Engineer building production-quality web UIs.
You have unlimited budget — write complete, beautiful, fully-functional files. Never truncate.

MODULAR ARCHITECTURE (strictly enforced):
- style.css   → ALL visual styling. CSS variables, layout, components, responsive.
- app.js      → ALL JavaScript logic. Login, fetch calls, auth, role routing.
- HTML files  → Pure semantic structure ONLY. Link to style.css and app.js. No inline <style> or <script> blocks.

HTML RULES:
- Every HTML file links to style.css and app.js: <link rel="stylesheet" href="style.css"> and <script src="app.js"></script>
- No inline <style> blocks. No inline <script> blocks. No onclick= handlers.
- Dashboards: full sidebar with logo + nav links + user section. Topbar with title + user info.
- Content: stat cards row, data tables with headers, action buttons.
- Use semantic HTML: <nav>, <aside>, <main>, <section>, <article>, <header>.
- Every element has a meaningful id or class that app.js can target.
- Dashboards ≥ 120 lines of clean HTML. Regular pages ≥ 60 lines.
- Never leave empty sections. Every card/table shows its structure even if data loads dynamically.

CSS RULES (style.css only):
- Must be ≥ 250 lines.
- :root { --primary, --primary-dark, --bg, --surface, --surface-2, --text, --text-subtle, --border, --accent, --danger, --success }
- Layout: .app-shell (flex row), .sidebar (fixed width), .main-content (flex-grow).
- Sidebar: .sidebar-logo, .nav-link (with hover + .active state), .nav-icon, .sidebar-footer.
- Topbar: .topbar, .topbar-title, .topbar-actions, .user-avatar.
- Components: .card, .stat-card, .stat-value, .stat-label, .btn, .btn-primary, .btn-danger, .badge.
- Tables: .data-table, thead th, tbody tr:hover, .table-action.
- Forms: .form-group, .form-label, .form-input, .form-error.
- Utilities: .hidden, .loading-spinner, .alert, .alert-error, .alert-success.
- Responsive: sidebar collapses on mobile, hamburger toggle.

JS RULES (app.js only):
- Must be ≥ 150 lines.
- const API = '/api/v1'; — single base URL constant.
- async function apiFetch(path, options={}) — wraps fetch, adds Authorization header, handles 401.
- Login: POST to login endpoint, save {token, role, name} to localStorage.
- Role routing on login: window.location.href = `/dashboard/${role}.html`.
- logout() clears localStorage and redirects to login.html.
- Each page's init function is called on DOMContentLoaded: e.g. initDashboard(), initLogin().
- Loading states: show .loading-spinner before fetch, hide after.
- Error handling: show .alert-error with message on API failure. Never console.error only.
- 401 auto-redirect: if apiFetch gets 401, call logout().

Return ONLY the complete file content. No JSON wrapper for single-file generation."""

FRONTEND_USER_TEMPLATE = """{constraint_block}

MODULAR ARCHITECTURE REMINDER:
- HTML = structure only, links to style.css and app.js. NO inline style/script.
- CSS = all styles in style.css.
- JS = all logic in app.js.

API ROUTES (use these exact paths in app.js fetch calls):
{routes_block}

AUTH: token=localStorage.getItem('token'), role=localStorage.getItem('userRole')
Login endpoint: {login_endpoint}

Previously written files (for import/class reference):
{written_context}

IMPLEMENT THIS FILE COMPLETELY:
  file_path : {file_path}
  purpose   : {purpose}

No truncation. No placeholders. No TODO comments. Production-ready."""

BULK_USER_TEMPLATE = """{constraint_block}
Previously written files in this project (imports must match exactly):
{written_context}

Files to implement now:
{files_json}

Each file entry includes "exports" (what other files import from it) and "depends_on" (files it imports from).
Implement ALL files listed. Return JSON: {{"file_path": "complete content", ...}}"""

SINGLE_FILE_TEMPLATE = """{constraint_block}
Previously written files:
{written_context}

Implement this file:
  file_path: {file_path}
  purpose:   {purpose}
  exports:   {exports}
  depends_on: {depends_on}

Return ONLY the complete file content (no JSON wrapper for single-file retry)."""


# ── Utilities ──────────────────────────────────────────────────────────────

def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _topo_waves(files: list[FileSpec]) -> list[list[FileSpec]]:
    """Topological sort into waves. Files in the same wave have no cross-dependencies."""
    path_to_spec = {f.file_path: f for f in files}
    in_degree: dict[str, int] = {f.file_path: 0 for f in files}
    dependents: dict[str, list[str]] = {f.file_path: [] for f in files}

    for f in files:
        for dep in f.depends_on:
            if dep in in_degree:
                in_degree[f.file_path] += 1
                dependents[dep].append(f.file_path)

    queue = [f for f in files if in_degree[f.file_path] == 0]
    waves: list[list[FileSpec]] = []

    while queue:
        waves.append(list(queue))
        next_q: list[FileSpec] = []
        for node in queue:
            for dep_path in dependents[node.file_path]:
                in_degree[dep_path] -= 1
                if in_degree[dep_path] == 0:
                    next_q.append(path_to_spec[dep_path])
        queue = next_q

    # Any remaining (cycle/missing) go in a final wave
    remaining = [f for f in files if in_degree[f.file_path] > 0]
    if remaining:
        waves.append(remaining)

    return waves


def _written_context(written: list[GeneratedFile], max_chars: int = 8000) -> str:
    """Build a compact context block of already-written files for prompt injection.

    Priority order: config/database files first (most referenced), then other .py.
    Always includes requirements.txt so the LLM knows exact installed packages.
    """
    # Sort: important files first, then layer order
    _PRIORITY = ("requirements.txt", "config.py", "database.py", "security.py", "models")
    def _key(gf: GeneratedFile) -> int:
        for i, p in enumerate(_PRIORITY):
            if p in gf.file_path:
                return i
        return 99

    ordered = sorted(written, key=_key)
    lines: list[str] = []
    budget = max_chars

    for gf in ordered:
        is_py  = gf.file_path.endswith(".py")
        is_txt = gf.file_path == "requirements.txt"
        if not (is_py or is_txt):
            continue
        header = f"\n--- {gf.file_path} ({gf.lines} lines) ---\n"
        if is_txt:
            snippet = gf.content  # always show full requirements.txt
        elif gf.lines <= 60:
            snippet = gf.content
        else:
            snippet_lines = gf.content.splitlines()[:60]
            snippet = "\n".join(snippet_lines) + "\n# ... (truncated for context)"
        entry = header + snippet
        if budget - len(entry) < 0:
            break
        lines.append(entry)
        budget -= len(entry)
    return "\n".join(lines) if lines else "(no files written yet)"


def _parse_bulk_json(text: str) -> dict[str, str]:
    """Extract {file_path: content} dict from LLM output."""
    # find_json_in_text may return a parsed dict directly
    extracted = find_json_in_text(text)
    if isinstance(extracted, dict):
        return {k: v for k, v in extracted.items() if isinstance(v, str)}

    # Fall back to string-based extraction
    raw = text.strip()
    raw = re.sub(r'^```[a-z]*\n?', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\n?```$', '', raw, flags=re.MULTILINE)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str)}
    except json.JSONDecodeError:
        pass
    # Fallback: scan for quoted file paths
    result: dict[str, str] = {}
    pattern = re.compile(r'"([^"]+\.[a-z]{1,5})"\s*:\s*"((?:[^"\\]|\\.)*)"\s*[,}]', re.DOTALL)
    for m in pattern.finditer(text):
        try:
            result[m.group(1)] = m.group(2).encode().decode('unicode_escape').replace('\\n', '\n')
        except (UnicodeDecodeError, ValueError):
            result[m.group(1)] = m.group(2).replace('\\n', '\n')
    return result


# ── Core executor ──────────────────────────────────────────────────────────

class LayeredExecutor:
    def __init__(
        self,
        llm: LLMClient,
        workspace: str,
        emit: _EMIT | None = None,
        concurrency: int = 6,
    ) -> None:
        self.llm = llm
        self.workspace = workspace
        self.emit = emit or (lambda t, d: None)
        self.concurrency = concurrency
        self._written: list[GeneratedFile] = []
        self._venv_task: asyncio.Task | None = None  # background venv + pip install

    async def _emit(self, event_type: str, data: dict) -> None:
        result = self.emit(event_type, data)
        if asyncio.iscoroutine(result):
            await result

    async def execute_plan(
        self,
        files: list[FileSpec],
        manifest: ProjectManifest,
        layer_defs: list[LayerDef] = LAYER_DEFS,
    ) -> list[LayerResult]:
        """Execute all layers in order. Returns list of LayerResults."""
        results: list[LayerResult] = []
        current_manifest = manifest

        for layer_def in layer_defs:
            layer_files = [f for f in files if f.layer == layer_def.index]
            if not layer_files:
                continue

            await self._emit("layer_started", {
                "layer": layer_def.index,
                "name": layer_def.name,
                "file_count": len(layer_files),
            })

            t0 = time.monotonic()
            try:
                layer_result = await self._execute_layer(
                    layer_def, layer_files, current_manifest
                )
            except (LayerGateError, NeedsReviewError):
                raise
            except Exception as exc:
                raise LayerGateError(layer_def.index, layer_def.name, [str(exc)]) from exc

            duration = time.monotonic() - t0
            layer_result = LayerResult(
                layer=layer_result.layer,
                name=layer_result.name,
                status=layer_result.status,
                files=layer_result.files,
                duration_s=round(duration, 2),
                heal_rounds=layer_result.heal_rounds,
                errors=layer_result.errors,
            )

            results.append(layer_result)
            await self._emit("layer_passed" if layer_result.status == "passed" else "layer_failed", {
                "layer": layer_def.index,
                "name": layer_def.name,
                "status": layer_result.status,
                "files": len(layer_result.files),
                "duration_s": layer_result.duration_s,
                "errors": layer_result.errors,
            })

            # After Layer 2: update manifest with ground truth from disk
            if layer_def.index == 2:
                current_manifest = update_from_disk(current_manifest, self.workspace)
                from .manifest import save as save_manifest
                save_manifest(current_manifest, self.workspace)
                await self._emit("manifest_updated", {
                    "models": list(current_manifest.models.keys()),
                })

        return results

    async def _execute_layer(
        self,
        layer_def: LayerDef,
        files: list[FileSpec],
        manifest: ProjectManifest,
    ) -> LayerResult:
        """Generate, guard, write, and validate a single layer."""
        constraint_block = render_constraint_block(manifest)
        waves = _topo_waves(files)
        _manifest = manifest  # keep ref for frontend wave generation
        generated: list[GeneratedFile] = []
        errors: list[str] = []

        for wave_idx, wave in enumerate(waves):
            await self._emit("wave_started", {
                "layer": layer_def.index,
                "wave": wave_idx + 1,
                "files": [f.file_path for f in wave],
            })

            wave_results = await self._generate_wave_by_priority(
                wave, constraint_block, _manifest, layer_def.index
            )
            failed_specs: list[FileSpec] = []

            for spec, content in wave_results:
                if content is None:
                    failed_specs.append(spec)
                    continue

                guard_failures = run_guards(spec.file_path, content)
                if guard_failures:
                    reasons = [g.reason + ": " + g.detail for g in guard_failures]
                    print(f"  [Guard] {spec.file_path}: {reasons[0]} — queuing retry")
                    await self._emit("file_guard_failed", {
                        "layer": layer_def.index,
                        "file": spec.file_path,
                        "reason": reasons[0],
                    })
                    failed_specs.append(spec)
                    continue

                gf = self._write_file(spec, content, layer_def.index)
                generated.append(gf)
                self._written.append(gf)
                await self._emit("file_done", {
                    "layer": layer_def.index,
                    "file": spec.file_path,
                    "lines": gf.lines,
                    "status": "done",
                })

            # Retry failed files individually
            for spec in failed_specs:
                gf = await self._retry_file(spec, constraint_block, layer_def.index)
                if gf:
                    generated.append(gf)
                    self._written.append(gf)
                    await self._emit("file_done", {
                        "layer": layer_def.index,
                        "file": spec.file_path,
                        "lines": gf.lines,
                        "status": "retried",
                    })
                else:
                    errors.append(f"Failed to generate {spec.file_path}")
                    await self._emit("file_failed", {
                        "layer": layer_def.index,
                        "file": spec.file_path,
                    })

        # Layer validation
        validation_errors = await self._validate_layer(layer_def, generated, manifest)

        if validation_errors:
            errors.extend(validation_errors)
            # Healing rounds
            heal_rounds = 0
            prev_error_sig = ""
            for round_num in range(1, layer_def.max_heal_rounds + 1):
                await self._emit("healing_started", {
                    "layer": layer_def.index,
                    "round": round_num,
                    "errors": validation_errors[:3],
                })
                # If error signature unchanged from last round, skip LLM — it can't help
                curr_sig = "|".join(sorted(validation_errors))
                if curr_sig == prev_error_sig:
                    print(f"  [Healer] L{layer_def.index} round {round_num}: error unchanged, skipping LLM")
                    break
                prev_error_sig = curr_sig
                healed = await self._heal_layer(layer_def, generated, validation_errors, constraint_block)
                if healed:
                    generated = healed
                    heal_rounds = round_num
                validation_errors = await self._validate_layer(layer_def, generated, manifest)
                if not validation_errors:
                    await self._emit("healing_done", {
                        "layer": layer_def.index, "round": round_num, "status": "fixed",
                    })
                    break

            if validation_errors:
                if layer_def.on_failure == "hard_stop":
                    raise LayerGateError(layer_def.index, layer_def.name, validation_errors)
                else:
                    raise NeedsReviewError(layer_def.index, layer_def.name, validation_errors)

            return LayerResult(
                layer=layer_def.index, name=layer_def.name,
                status="passed", files=generated,
                duration_s=0, heal_rounds=heal_rounds, errors=[],
            )

        return LayerResult(
            layer=layer_def.index, name=layer_def.name,
            status="passed", files=generated,
            duration_s=0, heal_rounds=0, errors=[],
        )

    async def _generate_wave(
        self,
        wave: list[FileSpec],
        constraint_block: str,
    ) -> list[tuple[FileSpec, str | None]]:
        """Generate all files in a wave using stream-bulk (single LLM call)."""
        files_json = json.dumps([
            {
                "file_path": f.file_path,
                "purpose": f.purpose,
                "exports": f.exports,
                "depends_on": f.depends_on,
            }
            for f in wave
        ], indent=2)

        written_ctx = _written_context(self._written)
        user_prompt = BULK_USER_TEMPLATE.format(
            constraint_block=constraint_block,
            written_context=written_ctx,
            files_json=files_json,
        )
        user_prompt = prune_prompt(user_prompt, max_chars=28_000)

        await self._emit("wave_generating", {
            "files": [f.file_path for f in wave],
        })

        # Try streaming if available
        if hasattr(self.llm, "astream"):
            raw = await self._stream_collect(user_prompt)
        else:
            raw = await self.llm.generate(user_prompt, system_prompt=SYSTEM_PROMPT)

        file_map = _parse_bulk_json(raw)

        results: list[tuple[FileSpec, str | None]] = []
        for spec in wave:
            content = file_map.get(spec.file_path)
            if content is None:
                # Try basename match
                for k, v in file_map.items():
                    if k.endswith(spec.file_path) or spec.file_path.endswith(k):
                        content = v
                        break
            results.append((spec, content))
        return results

    async def _generate_wave_by_priority(
        self,
        wave: list[FileSpec],
        constraint_block: str,
        manifest: ProjectManifest,
        layer: int,
    ) -> list[tuple[FileSpec, str | None]]:
        """Route each file to the right generation strategy based on priority.

        low    → all in one bulk call (boilerplate, no real logic)
        medium → batches of 4 files per LLM call
        high   → individual LLM call per file (maximum quality)
        frontend (layer 6 HTML/CSS/JS) → dedicated frontend prompt per file
        """
        low    = [f for f in wave if f.priority == "low"]
        medium = [f for f in wave if f.priority == "medium"]
        high   = [f for f in wave if f.priority == "high"]

        results: list[tuple[FileSpec, str | None]] = []

        # LOW — one bulk call for all boilerplate
        if low:
            results.extend(await self._generate_wave(low, constraint_block))

        # MEDIUM — batches of 4
        for i in range(0, len(medium), 4):
            batch = medium[i:i+4]
            results.extend(await self._generate_wave(batch, constraint_block))

        # HIGH — individual calls; use frontend prompt for layer 6 HTML/CSS/JS
        for spec in high:
            is_frontend = layer == 6 and not spec.file_path.endswith(".py")
            if is_frontend:
                partial = await self._generate_wave_frontend([spec], constraint_block, manifest)
            else:
                partial = await self._generate_single_high(spec, constraint_block)
            results.extend(partial)

        return results

    async def _generate_single_high(
        self,
        spec: FileSpec,
        constraint_block: str,
    ) -> list[tuple[FileSpec, str | None]]:
        """Generate one high-priority file with its own dedicated LLM call."""
        written_ctx = _written_context(self._written, max_chars=8000)
        user_prompt = SINGLE_FILE_TEMPLATE.format(
            constraint_block=constraint_block,
            written_context=written_ctx,
            file_path=spec.file_path,
            purpose=spec.purpose,
            exports=spec.exports,
            depends_on=spec.depends_on,
        )
        try:
            content = await self.llm.generate(
                prune_prompt(user_prompt, 32_000), system_prompt=SYSTEM_PROMPT
            )
            content = re.sub(r'^```[a-z]*\n?', '', content, flags=re.MULTILINE)
            content = re.sub(r'\n?```$', '', content, flags=re.MULTILINE).strip()
            return [(spec, content)]
        except Exception as exc:
            print(f"  [High] {spec.file_path}: {exc}")
            return [(spec, None)]

    async def _generate_wave_frontend(
        self,
        wave: list[FileSpec],
        constraint_block: str,
        manifest: ProjectManifest,
    ) -> list[tuple[FileSpec, str | None]]:
        """Generate frontend files individually with the dedicated UI prompt.

        Each HTML/CSS/JS file gets its own LLM call with full context and
        the high-quality FRONTEND_SYSTEM_PROMPT — no token budget sharing.
        Python test files in Layer 6 still use the standard path.
        """
        # Build routes block for the frontend prompt
        routes_lines = []
        for r in manifest.routes:
            auth_tag = "[AUTH]" if r.auth_required else "[PUBLIC]"
            routes_lines.append(f"  {r.method:<6} {r.path:<50} {auth_tag}  {r.summary}")
        routes_block = "\n".join(routes_lines) if routes_lines else "  (see manifest)"

        # Generate concurrently but each file gets its own full LLM call
        semaphore = asyncio.Semaphore(3)  # max 3 parallel frontend LLM calls

        async def _gen_one(spec: FileSpec) -> tuple[FileSpec, str | None]:
            # Python files (tests etc.) use the standard single-file path
            if spec.file_path.endswith(".py"):
                written_ctx = _written_context(self._written, max_chars=5000)
                prompt = SINGLE_FILE_TEMPLATE.format(
                    constraint_block=constraint_block,
                    written_context=written_ctx,
                    file_path=spec.file_path,
                    purpose=spec.purpose,
                    exports=spec.exports,
                    depends_on=spec.depends_on,
                )
                try:
                    content = await self.llm.generate(
                        prune_prompt(prompt, 28_000), system_prompt=SYSTEM_PROMPT
                    )
                    content = re.sub(r'^```[a-z]*\n?', '', content, flags=re.MULTILINE)
                    content = re.sub(r'\n?```$', '', content, flags=re.MULTILINE).strip()
                    return spec, content
                except Exception:
                    return spec, None

            # HTML/CSS/JS: dedicated frontend prompt, no pruning on output
            async with semaphore:
                written_ctx = _written_context(self._written, max_chars=4000)
                prompt = FRONTEND_USER_TEMPLATE.format(
                    constraint_block=constraint_block,
                    routes_block=routes_block,
                    login_endpoint=manifest.auth.login_endpoint,
                    written_context=written_ctx,
                    file_path=spec.file_path,
                    purpose=spec.purpose,
                )
                try:
                    content = await self.llm.generate(
                        prune_prompt(prompt, 32_000), system_prompt=FRONTEND_SYSTEM_PROMPT
                    )
                    content = re.sub(r'^```[a-z]*\n?', '', content, flags=re.MULTILINE)
                    content = re.sub(r'\n?```$', '', content, flags=re.MULTILINE).strip()
                    return spec, content
                except Exception:
                    return spec, None

        tasks = [_gen_one(spec) for spec in wave]
        return list(await asyncio.gather(*tasks))

    async def _stream_collect(self, user_prompt: str) -> str:
        """Collect full text from an astream call."""
        chunks: list[str] = []
        async for chunk in self.llm.astream(user_prompt, system_prompt=SYSTEM_PROMPT):
            chunks.append(chunk)
        return "".join(chunks)

    async def _retry_file(
        self,
        spec: FileSpec,
        constraint_block: str,
        layer: int,
    ) -> GeneratedFile | None:
        """Retry a single file with a focused prompt."""
        best_content: str | None = None
        for attempt in range(1, MAX_FILE_RETRIES + 1):
            written_ctx = _written_context(self._written, max_chars=6000)
            user_prompt = SINGLE_FILE_TEMPLATE.format(
                constraint_block=constraint_block,
                written_context=written_ctx,
                file_path=spec.file_path,
                purpose=spec.purpose,
                exports=spec.exports,
                depends_on=spec.depends_on,
            )
            user_prompt = prune_prompt(user_prompt, max_chars=24_000)
            try:
                content = await self.llm.generate(user_prompt, system_prompt=SYSTEM_PROMPT)
                # Strip markdown fences
                content = re.sub(r'^```[a-z]*\n?', '', content, flags=re.MULTILINE)
                content = re.sub(r'\n?```$', '', content, flags=re.MULTILINE).strip()
                guard_failures = run_guards(spec.file_path, content)
                if not guard_failures:
                    return self._write_file(spec, content, layer)
                # Keep best attempt (only SizeGuard failing = acceptable fallback)
                only_size = all(g.reason == "SizeGuard" for g in guard_failures)
                if only_size:
                    best_content = content
                print(f"  [Retry {attempt}] {spec.file_path}: {guard_failures[0].reason}")
            except Exception as exc:
                print(f"  [Retry {attempt}] {spec.file_path}: {exc}")

        # If only SizeGuard failed, write with a warning rather than dropping the file
        if best_content is not None:
            print(f"  [Warn] {spec.file_path}: wrote under minimum lines (SizeGuard) — continuing")
            return self._write_file(spec, best_content, layer)
        return None

    def _write_file(self, spec: FileSpec, content: str, layer: int) -> GeneratedFile:
        """Write content to disk and return a GeneratedFile."""
        full_path = os.path.join(self.workspace, spec.file_path)
        ensure_directory(os.path.dirname(full_path))
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        lines = content.count("\n")
        print(f"  ✓ [{layer}] {spec.file_path} ({lines} lines)")

        # Kick off venv build in the background the moment requirements.txt lands
        if spec.file_path == "requirements.txt" and self._venv_task is None:
            self._venv_task = asyncio.create_task(self._prepare_venv())

        return GeneratedFile(
            file_path=spec.file_path,
            content=content,
            layer=layer,
            lines=lines,
            sha256=_sha256(content),
        )

    async def _fix_missing_schemas(
        self,
        schema_issues: list,
        constraint_block: str,
    ) -> list[str]:
        """Auto-add missing Pydantic classes to schema files using a targeted LLM call."""
        from .validators import SchemaIssue
        errors: list[str] = []
        for issue in schema_issues:
            full_path = os.path.join(self.workspace, issue.schema_path)
            existing = ""
            if os.path.exists(full_path):
                with open(full_path, encoding="utf-8") as f:
                    existing = f.read()
            missing_list = ", ".join(sorted(issue.missing_classes))
            prompt = f"""You are fixing a Pydantic schema file.

File: {issue.schema_path}
Missing classes that MUST be added: {missing_list}

Current file content:
{existing}

Add the missing classes as Pydantic BaseModel subclasses.
Each class needs appropriate fields (id: int, timestamps, relevant FK ids) and:
    model_config = ConfigDict(from_attributes=True)

Return the COMPLETE updated file — all existing classes PLUS the new ones."""
            try:
                fixed = await self.llm.generate(
                    prune_prompt(prompt, 20_000),
                    system_prompt="You are an expert Python developer. Add missing Pydantic schema classes. Return only the complete file content, no markdown.",
                )
                fixed = re.sub(r'^```[a-z]*\n?', '', fixed, flags=re.MULTILINE)
                fixed = re.sub(r'\n?```$', '', fixed, flags=re.MULTILINE).strip()
                from .guards import check_syntax
                if check_syntax(issue.schema_path, fixed).ok:
                    spec = FileSpec(issue.schema_path, issue.schema_path, 3, [], [])
                    gf = self._write_file(spec, fixed, 3)
                    self._written = [gf if w.file_path == issue.schema_path else w for w in self._written]
                    print(f"  [SchemaFix] {issue.schema_path}: added {missing_list}")
                else:
                    errors.append(f"schema fix syntax error in {issue.schema_path}")
            except Exception as exc:
                errors.append(f"schema fix failed for {issue.schema_path}: {exc}")
        return errors

    async def _fix_frontend_files(
        self,
        frontend_issues: list,
        files: list[GeneratedFile],
        constraint_block: str,
        manifest: ProjectManifest,
    ) -> list[GeneratedFile]:
        """Regenerate frontend files that failed quality checks."""
        routes_lines = []
        for r in manifest.routes:
            auth_tag = "[AUTH]" if r.auth_required else "[PUBLIC]"
            routes_lines.append(f"  {r.method:<6} {r.path:<50} {auth_tag}  {r.summary}")
        routes_block = "\n".join(routes_lines)

        updated = list(files)
        for issue in frontend_issues:
            gf = next((f for f in files if f.file_path == issue.file_path), None)
            if not gf:
                continue
            issues_text = "\n".join(f"- {i}" for i in issue.issues)
            written_ctx = _written_context(self._written, max_chars=3000)
            prompt = FRONTEND_USER_TEMPLATE.format(
                constraint_block=render_constraint_block(manifest),
                routes_block=routes_block,
                login_endpoint=manifest.auth.login_endpoint,
                written_context=written_ctx,
                file_path=issue.file_path,
                purpose=f"FIX THESE ISSUES:\n{issues_text}",
            )
            try:
                content = await self.llm.generate(
                    prune_prompt(prompt, 32_000),
                    system_prompt=FRONTEND_SYSTEM_PROMPT,
                )
                content = re.sub(r'^```[a-z]*\n?', '', content, flags=re.MULTILINE)
                content = re.sub(r'\n?```$', '', content, flags=re.MULTILINE).strip()
                spec = FileSpec(issue.file_path, issue.file_path, 6, [], [])
                new_gf = self._write_file(spec, content, 6)
                updated = [new_gf if f.file_path == issue.file_path else f for f in updated]
                self._written = [new_gf if w.file_path == issue.file_path else w for w in self._written]
                print(f"  [FrontendFix] {issue.file_path}: regenerated")
            except Exception as exc:
                print(f"  [FrontendFix] {issue.file_path}: {exc}")
        return updated

    async def _prepare_venv(self) -> None:
        """Create a project-local venv and install requirements.txt in the background."""
        venv_dir    = os.path.join(self.workspace, ".venv")
        venv_python = os.path.join(venv_dir, "bin", "python")
        req_path    = os.path.join(self.workspace, "requirements.txt")
        loop = asyncio.get_event_loop()
        try:
            print("  [venv] Creating project virtualenv…")
            r = await loop.run_in_executor(None, lambda: subprocess.run(
                [sys.executable, "-m", "venv", venv_dir],
                cwd=self.workspace, capture_output=True, timeout=60,
            ))
            if r.returncode != 0:
                print(f"  [venv] ✗ venv creation failed: {r.stderr.decode()[:200]}")
                return

            if os.path.exists(req_path) and os.path.exists(venv_python):
                print("  [venv] Installing requirements.txt…")
                r = await loop.run_in_executor(None, lambda: subprocess.run(
                    [venv_python, "-m", "pip", "install", "-r", "requirements.txt",
                     "--disable-pip-version-check"],
                    cwd=self.workspace, capture_output=True, timeout=180,
                ))
                if r.returncode != 0:
                    # Extract failed package lines from pip output
                    stderr = r.stderr.decode(errors="replace")
                    failed = [l for l in stderr.splitlines()
                              if "ERROR" in l or "No matching" in l or "Could not" in l]
                    for line in failed[:5]:
                        print(f"  [venv] ✗ {line.strip()}")
                    # Retry once with --no-deps
                    print("  [venv] Retrying with --no-deps…")
                    r2 = await loop.run_in_executor(None, lambda: subprocess.run(
                        [venv_python, "-m", "pip", "install", "-r", "requirements.txt",
                         "--disable-pip-version-check", "--no-deps"],
                        cwd=self.workspace, capture_output=True, timeout=120,
                    ))
                    if r2.returncode != 0:
                        err = r2.stderr.decode(errors="replace").strip()
                        raise RuntimeError(f"pip install failed after retry: {err[:300]}")
                    print("  [venv] ✓ Ready (no-deps fallback)")
                else:
                    print("  [venv] ✓ Ready")
        except RuntimeError:
            raise  # propagate pip failures to _validate_layer
        except Exception as exc:
            print(f"  [venv] Setup failed (non-fatal): {exc}")

    async def _validate_layer(
        self,
        layer_def: LayerDef,
        files: list[GeneratedFile],
        manifest: ProjectManifest | None = None,
    ) -> list[str]:
        """Run layer validation. Returns list of error strings (empty = passed)."""
        import py_compile, tempfile
        errors: list[str] = []

        for gf in files:
            if not gf.file_path.endswith(".py"):
                continue
            # 1. AST syntax check (instant, in-process)
            from .guards import check_syntax
            result = check_syntax(gf.file_path, gf.content)
            if not result.ok:
                errors.append(f"{gf.file_path}: {result.detail}")
                continue
            # 2. py_compile check (catches encoding + compile-time errors AST misses)
            try:
                with tempfile.NamedTemporaryFile(suffix=".py", mode="w",
                                                 encoding="utf-8", delete=False) as tmp:
                    tmp.write(gf.content)
                    tmp_path = tmp.name
                py_compile.compile(tmp_path, doraise=True)
                os.unlink(tmp_path)
            except py_compile.PyCompileError as exc:
                os.unlink(tmp_path)
                errors.append(f"{gf.file_path}: compile error — {exc}")

        # Layer 5 (API entry): wait for background venv, then smoke-test import
        if layer_def.index == 5 and not errors:
            main_path = os.path.join(self.workspace, "src", "main.py")
            if os.path.exists(main_path):
                # Wait for the background venv task (started when requirements.txt was written)
                if self._venv_task is not None:
                    try:
                        await asyncio.wait_for(self._venv_task, timeout=30.0)
                    except asyncio.TimeoutError:
                        pass  # still running — proceed anyway
                    except RuntimeError as exc:
                        # pip install failed hard — stop immediately
                        errors.append(f"dependency install failed: {exc}")
                        return errors
                    except Exception:
                        pass  # non-fatal venv errors

                venv_python = os.path.join(self.workspace, ".venv", "bin", "python")
                python_bin  = venv_python if os.path.exists(venv_python) else sys.executable
                env = dict(os.environ, PYTHONPATH=self.workspace)

                def _run_smoke() -> subprocess.CompletedProcess:
                    return subprocess.run(
                        [python_bin, "-c", "import src.main"],
                        cwd=self.workspace, env=env,
                        capture_output=True, text=True, timeout=30,
                    )

                def _extract_missing_pkg(stderr: str) -> str | None:
                    """Parse a missing package name from pip-installable error messages."""
                    # ModuleNotFoundError: No module named 'python_multipart'
                    m = re.search(r"No module named '([^']+)'", stderr)
                    if m:
                        return m.group(1).split(".")[0].replace("_", "-")
                    # RuntimeError: Form data requires "python-multipart" to be installed
                    m = re.search(r'requires "([^"]+)" to be installed', stderr)
                    if m:
                        return m.group(1)
                    return None

                try:
                    proc = _run_smoke()
                    if proc.returncode != 0:
                        stderr = proc.stderr.strip()
                        # Auto-install missing packages and retry (up to 3 packages)
                        for _ in range(3):
                            pkg = _extract_missing_pkg(stderr)
                            if not pkg:
                                break
                            print(f"  [venv] Auto-installing missing package: {pkg}")
                            subprocess.run(
                                [python_bin, "-m", "pip", "install", pkg, "-q",
                                 "--disable-pip-version-check"],
                                cwd=self.workspace, capture_output=True, timeout=60,
                            )
                            proc = _run_smoke()
                            if proc.returncode == 0:
                                break
                            stderr = proc.stderr.strip()

                        if proc.returncode != 0:
                            stderr = proc.stderr.strip()
                            first_err = next(
                                (l for l in stderr.splitlines() if "Error" in l or "error" in l),
                                stderr[:300] if stderr else ""
                            )
                            if first_err:
                                errors.append(f"startup import: {first_err}")
                except subprocess.TimeoutExpired:
                    pass  # timeout means app tried to start — good enough
                except Exception:
                    pass

        # Layer 5: deterministic schema import check — find missing classes
        if layer_def.index == 5 and not errors:
            from .validators import check_schema_imports
            schema_issues = check_schema_imports(self.workspace)
            if schema_issues:
                missing_summary = "; ".join(
                    f"{si.schema_path}: missing {', '.join(sorted(si.missing_classes))}"
                    for si in schema_issues
                )
                print(f"  [SchemaValidator] Missing schema classes: {missing_summary}")
                fix_errors = await self._fix_missing_schemas(schema_issues, "")
                errors.extend(fix_errors)

        # Layer 6: deterministic frontend quality check
        if layer_def.index == 6 and manifest is not None:
            from .validators import check_frontend_quality
            frontend_issues = check_frontend_quality(self.workspace, files)
            if frontend_issues:
                for fi in frontend_issues:
                    print(f"  [FrontendValidator] {fi.file_path}: {'; '.join(fi.issues)}")
                files = await self._fix_frontend_files(frontend_issues, files, "", manifest)
                # Re-check after fix
                remaining = check_frontend_quality(self.workspace, files)
                for fi in remaining:
                    errors.append(f"{fi.file_path}: {'; '.join(fi.issues)}")

        return errors

    async def _heal_layer(
        self,
        layer_def: LayerDef,
        files: list[GeneratedFile],
        errors: list[str],
        constraint_block: str,
    ) -> list[GeneratedFile] | None:
        """Single heal round: fix files that have errors."""
        # Find files referenced in errors — prefer exact path matches from tracebacks
        error_text = "\n".join(errors[:5])
        target_files = [
            gf for gf in files
            if gf.file_path in error_text
            or os.path.basename(gf.file_path) in error_text
        ]
        # Exclude __init__.py from healing targets — clear it instead
        init_files = [gf for gf in target_files if os.path.basename(gf.file_path) == "__init__.py"]
        target_files = [gf for gf in target_files if os.path.basename(gf.file_path) != "__init__.py"]
        # Auto-fix __init__.py by emptying it (never needs imports)
        for gf in init_files:
            self._write_file(FileSpec(gf.file_path, gf.file_path, gf.layer, [], []), "", gf.layer)
        if not target_files:
            target_files = files[:3]  # fallback: heal first 3 files

        healed = list(files)
        written_ctx = _written_context(self._written, max_chars=5000)

        for gf in target_files:
            heal_prompt = f"""{constraint_block}

FIX THIS FILE: {gf.file_path}

Errors to fix:
{error_text}

Current file content:
{gf.content}

Previously written context:
{written_ctx}

Return the COMPLETE corrected file content. Every line, no truncation."""

            try:
                fixed = await self.llm.generate(
                    prune_prompt(heal_prompt, 24_000),
                    system_prompt="You are an expert debugger. Fix only what is broken. "
                                  "Return the complete corrected file content.",
                )
                fixed = re.sub(r'^```[a-z]*\n?', '', fixed, flags=re.MULTILINE)
                fixed = re.sub(r'\n?```$', '', fixed, flags=re.MULTILINE).strip()
                guard_failures = run_guards(gf.file_path, fixed)
                if not guard_failures:
                    new_gf = self._write_file(
                        FileSpec(gf.file_path, gf.file_path, gf.layer, [], []),
                        fixed, gf.layer,
                    )
                    healed = [new_gf if h.file_path == gf.file_path else h for h in healed]
                    # Update written list too
                    self._written = [
                        new_gf if w.file_path == gf.file_path else w
                        for w in self._written
                    ]
            except Exception as exc:
                print(f"  [Healer] {gf.file_path}: {exc}")

        return healed
