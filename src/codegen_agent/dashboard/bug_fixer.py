"""Post-build bug fixer — simple N-pass file-by-file triage + fix.

Replaces the complex healer loop with the Fully Autonomous approach:
  Pass 1..N:
    For each source file:
      1. Triage (cheap LLM): does it have issues?
      2. Fix  (medium LLM): generate corrected file
      3. SyntaxGuard: validate before writing
      4. PatchCache: skip LLM if identical bug seen before
  After all passes: write needs-review.md for unfixable files.
"""
from __future__ import annotations

import ast
import asyncio
import fnmatch
import hashlib
import json
import logging
import re
from pathlib import Path

from .config import cfg
from .event_bus import bus
from .git_manager import GitManager
from .project_registry import Project
from ..patch_cache import PatchCache
from ..context_builder import ProjectContextBuilder

log = logging.getLogger(__name__)

# Merged triage+fix: one LLM call instead of two for non-trivial files
_ANALYZE_AND_FIX_PROMPT = """\
Analyze this file for bugs, logic errors, missing imports, and syntax issues.
If the file has issues, return the complete corrected file.
Return ONLY JSON — no markdown fences, no explanation outside the JSON:
{{
  "has_issues": true/false,
  "issues": ["brief description of each issue"],
  "fixed_content": "<complete corrected file content, or null if no issues>"
}}

PROJECT STRUCTURE (use this to find correct import paths and exported names):
{project_context}

FILE: {path}
KNOWN ERRORS (from test run):
{errors}

CURRENT CONTENT:
```
{content}
```
"""


_EXTS = (
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx",
    "*.html", "*.css", "*.go", "*.rs", "*.java",
)

# Minimum line counts below which a frontend file is considered a stub
_FRONTEND_MIN_LINES: dict[str, int] = {
    "index.html": 150,
    "login.html": 80,
    "student.html": 120,
    "teacher.html": 120,
    "admin.html": 120,
    "dashboard.html": 120,
    "style.css": 250,
    "app.js": 150,
}

_FRONTEND_REGEN_PROMPT = """\
You are a Senior Frontend Engineer. Rewrite this file to be production-quality.

PROJECT: {project_name}
FILE: {file_path}
BRIEF: {brief}

CURRENT CONTENT (this is a stub — too thin, needs to be complete):
{content}

DESIGN SYSTEM (use exactly these CSS custom properties):
:root {{
  --bg: #09090b; --surface: #18181b; --surface-2: #27272a; --border: #3f3f46;
  --primary: #6366f1; --primary-hover: #4f46e5; --primary-glow: rgba(99,102,241,0.3);
  --accent: #8b5cf6; --success: #22c55e; --warning: #f59e0b; --danger: #ef4444;
  --text: #fafafa; --text-muted: #a1a1aa; --text-subtle: #52525b;
  --radius: 12px; --radius-sm: 8px; --shadow: 0 4px 24px rgba(0,0,0,0.5);
  --font: 'Inter', system-ui, sans-serif; --transition: all 0.2s cubic-bezier(0.4,0,0.2,1);
}}

REQUIREMENTS FOR {file_name}:
{requirements}

RULES:
- Output ONLY the raw file content. No markdown fences, no explanation.
- Write EVERYTHING — do not truncate, do not use placeholders.
- All text must be real content for this specific project, not generic lorem ipsum.
- Google Fonts import for Inter in every HTML <head>.
"""

_FRONTEND_REQUIREMENTS: dict[str, str] = {
    "index.html": """- Sticky navbar: logo, nav links, accessibility toggles (+A/-A, contrast, dyslexia), login button
- Hero: min-height 90vh, animated mesh/gradient background, large headline with gradient text, subheadline, 2 CTA buttons
- Stats bar: 4 numbers with labels (students, courses, years, volunteers)
- Programs section: 4 cards with icons, titles, descriptions, module counts
- Success stories: 3 testimonial cards with name, quote, role
- Team section: 3 staff profile cards with initials avatar, name, role, bio
- Donation CTA: progress bar, goal amount, donate button
- Contact section: address, phone, email + contact form
- Footer: logo, nav links, social links, copyright
- ARIA skip link, all images alt text, keyboard accessible""",
    "login.html": """- Centered card on full dark background
- Logo and project name at top
- Email + password inputs with labels and focus glow
- Role selector dropdown (student/teacher/staff/admin/parent)
- Login button (gradient primary)
- Error message area (hidden by default)
- Link back to home
- Accessible: all inputs labelled, focus indicators""",
    "student.html": """- Sidebar nav: Profile, My Courses, Tests, Results, Attendance, Notifications
- Stats row: 4 cards (enrolled courses, completed modules, avg score, attendance %)
- Enrolled courses list with progress bars
- Weekly tests panel with countdown timer and start button
- Test-taking UI: one question at a time, MCQ radio buttons, descriptive textarea, progress bar
- Results: score cards, percentage badge, question review
- Attendance heatmap: CSS grid calendar, color-coded days
- Notifications list with mark-as-read""",
    "teacher.html": """- Sidebar nav: Overview, Lessons, Tests, Grading, Students, Attendance
- Overview: stat cards (students, active tests, pending grades)
- Lesson builder: course/module selector, title, content area, material URL
- Test builder: add MCQ (4 options, mark correct), descriptive questions, set duration, publish
- Grading panel: ungraded answers list, marks input, feedback textarea
- Student performance table: sortable columns (name, avg score, attendance, last active)
- Attendance: date picker, student list with present/absent/late radio toggles""",
    "admin.html": """- Sidebar nav: Overview, Users, Courses, Tests, Results, Analytics, Audit Logs
- Overview: stat cards + donut chart (SVG) for users by role + recent activity feed
- User management: searchable table, role badge, activate/deactivate toggle, add user modal
- Course management: CRUD table for courses and modules
- Analytics: CSS bar chart for scores per course, top performers leaderboard
- Audit log: filterable table with user, action, resource, timestamp""",
    "dashboard.html": """- Authenticated landing: sidebar + main content area
- Welcome message with user name and role
- Quick stats cards
- Recent activity feed
- Quick-action buttons""",
    "style.css": """- Full :root design system (all custom properties listed above)
- Google Fonts @import for Inter
- CSS reset (*, box-sizing)
- Navbar: sticky, backdrop-filter blur, border-bottom, logo, nav-links, accessibility buttons
- Hero: full-height, animated gradient background (@keyframes gradientShift), gradient text
- Cards: surface bg, border, border-radius, padding, hover lift + glow transition
- Buttons: primary (gradient), outline, ghost — all with hover/active/focus states
- Forms: inputs with focus glow ring, floating or clean labels
- Sidebar layout: CSS grid, sidebar fixed width, main scrollable
- Stat cards: number large bold, label muted
- Progress bars: animated fill
- Toast container: fixed bottom-right, slideIn animation
- Modal overlay: backdrop blur
- Responsive: 640px, 768px, 1024px breakpoints, mobile sidebar hidden
- High-contrast override: [data-theme='high-contrast'] variables
- Font-size utilities: [data-fontsize='large'] root font-size bump
- Dyslexia font: [data-font='dyslexia'] font-family override
- Skeleton loading animation
- ARIA focus indicators on all interactive elements (never outline:none)
- Accessibility: .skip-link, [aria-live]
- @keyframes: fadeInUp, slideIn, gradientShift, shimmer, pulse
- Minimum 300 lines""",
    "app.js": """- Auth module: login(), logout(), getToken(), isLoggedIn(), fetchWithAuth(url, options)
- On load: read token → route to correct dashboard page
- Toast system: showToast(message, type) — success/error/info, auto-dismiss 3s, stackable
- Accessibility: announceToScreenReader(text), updateFontSize(delta), toggleHighContrast(), toggleDyslexiaFont()
- Mobile hamburger menu toggle
- Dashboard loaders that fetch from API: loadStudentDashboard(), loadTeacherDashboard(), loadAdminDashboard()
- Test engine: startTest(id), renderQuestion(index), submitAnswer(), autoSubmitOnTimeout(seconds)
- Charts: renderBarChart(containerId, labels, values), renderDonutChart(svgId, segments) — pure CSS/SVG
- Persist accessibility prefs in localStorage on page load""",
}


# Module-level shared router — created once, reused across all BugFixer calls
_SHARED_ROUTER: "LLMRouter | None" = None

def _get_router():
    global _SHARED_ROUTER
    if _SHARED_ROUTER is None:
        from ..llm.router import LLMRouter
        _SHARED_ROUTER = LLMRouter()
    return _SHARED_ROUTER

async def _llm(role: str, prompt: str) -> str:
    client = _get_router().get_client_for_role(role)
    return await client.generate(prompt)


def _fix_key(path: Path, issues_str: str, content: str) -> str:
    sig = hashlib.sha256(content.encode(), usedforsecurity=False).hexdigest()[:16]
    raw = f"{path.name}|{issues_str}|{sig}"
    return hashlib.sha256(raw.encode(), usedforsecurity=False).hexdigest()


def _syntax_ok(path: Path, content: str) -> tuple[bool, str]:
    if path.suffix != ".py":
        return True, ""
    try:
        ast.parse(content)
        return True, ""
    except SyntaxError as e:
        return False, str(e)


_FILE_TIMEOUT = 90    # seconds per file (2 LLM calls × ~40s each + headroom)
_PASS_CONCURRENCY = 10  # parallel file fixes per pass
_MAX_FILES_PER_PASS = 12  # cap files attempted per pass — prevents runaway on big projects
_TOTAL_TIMEOUT = int(__import__("os").environ.get("CODEGEN_HEALER_TIMEOUT", "480"))  # 8 min total

# ── Deterministic quick-fix table ─────────────────────────────────────────────
# Maps: undefined name → import line to inject
_IMPORT_FIX_TABLE: dict[str, str] = {
    "timezone":       "from datetime import timezone",
    "timedelta":      "from datetime import timedelta",
    "datetime":       "from datetime import datetime",
    "date":           "from datetime import date",
    "time":           "from datetime import time",
    "Optional":       "from typing import Optional",
    "List":           "from typing import List",
    "Dict":           "from typing import Dict",
    "Tuple":          "from typing import Tuple",
    "Any":            "from typing import Any",
    "Union":          "from typing import Union",
    "Callable":       "from typing import Callable",
    "TYPE_CHECKING":  "from typing import TYPE_CHECKING",
    "dataclass":      "from dataclasses import dataclass",
    "field":          "from dataclasses import field",
    "os":             "import os",
    "sys":            "import sys",
    "json":           "import json",
    "re":             "import re",
    "Path":           "from pathlib import Path",
    "uuid4":          "from uuid import uuid4",
    "uuid":           "import uuid",
    "HTTPException":  "from fastapi import HTTPException",
    "Depends":        "from fastapi import Depends",
    "status":         "from fastapi import status",
    "Request":        "from fastapi import Request",
    "logging":        "import logging",
    "asyncio":        "import asyncio",
    "ABC":            "from abc import ABC, abstractmethod",
    "abstractmethod": "from abc import abstractmethod",
    "Enum":           "from enum import Enum",
    "IntEnum":        "from enum import IntEnum",
}

# Regex to extract undefined names from NameError / ImportError lines
_UNDEFINED_RE = re.compile(
    r"NameError: name '(\w+)' is not defined"
    r"|ImportError: cannot import name '(\w+)'"
    r"|AttributeError: module '\w+' has no attribute '(\w+)'"
)


# Known antipatterns: (search_regex, replacement) applied to entire file content
_ANTIPATTERN_FIXES: list[tuple[re.Pattern, str]] = [
    # datetime.utcnow() → datetime.now(timezone.utc)
    (re.compile(r'\bdatetime\.utcnow\(\)'), 'datetime.now(timezone.utc)'),
    # sessionmaker( → async_sessionmaker( (ORM async fix)
    (re.compile(r'(?<!\w)sessionmaker\('), 'async_sessionmaker('),
    # from sqlalchemy.orm import ... sessionmaker ... → async_sessionmaker
    (re.compile(r'(from sqlalchemy\.orm import[^\n]*)\bsessionmaker\b'), r'\1async_sessionmaker'),
    # AsyncClient(app=app, ...) → AsyncClient(transport=ASGITransport(app=app), ...)
    (re.compile(r'AsyncClient\(app=(\w+),?\s*base_url=([^)]+)\)'),
     r'AsyncClient(transport=ASGITransport(app=\1), base_url=\2)'),
    # @app.on_event("startup") → keep but flag; too complex for simple regex
    # os.getenv("SECRET_KEY") with no default → add fallback to prevent ValidationError at startup
    (re.compile(r'os\.getenv\("SECRET_KEY"\)(?!\s*,)'), 'os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")'),
    (re.compile(r"os\.getenv\('SECRET_KEY'\)(?!\s*,)"), "os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')"),
    # CORS allow_origins=["*"] → use env var so production deployments can restrict origins
    (re.compile(r'allow_origins\s*=\s*\[\s*["\']?\*["\']?\s*\]'),
     'allow_origins=os.getenv("CORS_ORIGINS", "*").split(",")'),
    # Column(True, ...) / Column(False, ...) → Column(Boolean, ...)
    # LLMs often pass a Python literal instead of the SQLAlchemy type
    (re.compile(r'\bColumn\(True\s*,'), 'Column(Boolean,'),
    (re.compile(r'\bColumn\(False\s*,'), 'Column(Boolean,'),
]

# Imports required when an antipattern fix is applied
_ANTIPATTERN_EXTRA_IMPORTS: dict[str, str] = {
    'datetime.now(timezone.utc)': 'from datetime import timezone',
    'async_sessionmaker':         'from sqlalchemy.ext.asyncio import async_sessionmaker',
    'ASGITransport':              'from httpx import ASGITransport',
    'CORS_ORIGINS':               'import os',
    'Column(Boolean,':            'from sqlalchemy import Boolean',
}

# Router import pattern: app.include_router(X.router) without "import X" or "from . import X"
_INCLUDE_ROUTER_RE = re.compile(r'(?:app|router)\.include_router\(\s*(\w+)\.router')


def _edit_distance(a: str, b: str) -> int:
    """Simple Levenshtein distance for name similarity matching."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def _try_deterministic_fix(path: Path, content: str, error_lines: list[str]) -> str | None:
    """Attempt to fix common trivial issues without an LLM call.

    Handles:
    - Missing stdlib/typing imports from NameError messages
    - Known antipatterns (utcnow, sessionmaker, AsyncClient transport)
    - Missing router imports in main/app files
    Returns fixed content if any fix was applied and the result is valid Python, else None.
    """
    if path.suffix != ".py":
        return None

    changed = False
    working = content

    # ── Pass 1: antipattern substitutions ────────────────────────────────
    extra_imports: list[str] = []
    for pattern, replacement in _ANTIPATTERN_FIXES:
        if pattern.search(working):
            working = pattern.sub(replacement, working)
            changed = True
            for trigger, imp in _ANTIPATTERN_EXTRA_IMPORTS.items():
                if trigger in working and imp not in working:
                    extra_imports.append(imp)

    # ── Pass 2: missing router imports (main.py / app.py) ────────────────
    if path.name in ("main.py", "app.py") or "router" in path.name.lower():
        routers_pkg = path.parent / "routers"
        for m in _INCLUDE_ROUTER_RE.finditer(working):
            module_name = m.group(1)
            # Check if it's already imported
            if not re.search(
                rf'(?:from\s+\S+\s+import.*\b{module_name}\b|import\s+\S*{module_name})',
                working,
            ):
                # Use routers sub-package if it exists, else flat relative import
                if routers_pkg.is_dir():
                    extra_imports.append(f"from .routers import {module_name}")
                else:
                    extra_imports.append(f"from . import {module_name}")
                changed = True

    # ── Pass 3: missing imports from NameError messages ──────────────────
    missing: list[str] = []
    for line in error_lines:
        for m in _UNDEFINED_RE.finditer(line):
            name = m.group(1) or m.group(2) or m.group(3)
            if name and name in _IMPORT_FIX_TABLE:
                missing.append(name)

    existing_imports = set(re.findall(r'^\s*(?:import|from)\s+\S+', working, re.MULTILINE))
    for name in dict.fromkeys(missing):
        stmt = _IMPORT_FIX_TABLE[name]
        key = stmt.split()[1]
        if not any(key in ex for ex in existing_imports):
            extra_imports.append(stmt)
            changed = True

    if not changed:
        return None

    # ── Inject collected imports after the last import line ──────────────
    if extra_imports:
        lines = working.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("import ") or s.startswith("from "):
                insert_at = i + 1
            elif i == 0 and (s.startswith('"""') or s.startswith("'''") or s.startswith("#")):
                insert_at = 1
        deduped = list(dict.fromkeys(extra_imports))
        injection = "".join(s + "\n" for s in deduped)
        working = "".join(lines[:insert_at]) + injection + "".join(lines[insert_at:])

    fixed = working

    ok, _ = _syntax_ok(path, fixed)
    return fixed if ok else None


class BugFixer:
    def __init__(self, proj: Project, src_dir: str, num_passes: int = 2) -> None:
        self._proj = proj
        self._src_dir = Path(src_dir)
        self._git = GitManager(proj.id, src_dir)
        self._num_passes = num_passes
        self._patch_cache = PatchCache(str(self._src_dir.parent))
        self._needs_review: list[str] = []
        self._cache_hits = 0
        self._files_fixed = 0
        self._sem = asyncio.Semaphore(_PASS_CONCURRENCY)

    async def run(self) -> None:
        try:
            await asyncio.wait_for(self._run_internal(), timeout=_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("[%s] BugFixer hit total timeout (%ds) — stopping", self._proj.id, _TOTAL_TIMEOUT)
            await self._proj.log_activity(f"BugFixer: hit {_TOTAL_TIMEOUT}s total timeout — stopping early")
        finally:
            if self._needs_review:
                await self._write_needs_review()
            await self._proj.inc_stat("files_fixed", self._files_fixed)
            await self._proj.log_activity(
                f"BugFixer done — fixed={self._files_fixed} cache_hits={self._cache_hits} "
                f"needs_review={len(self._needs_review)}"
            )
            log.info(
                "[%s] BugFixer complete: fixed=%d cache_hits=%d needs_review=%d",
                self._proj.id, self._files_fixed, self._cache_hits, len(self._needs_review),
            )

    async def _run_internal(self) -> None:
        log.info("[%s] BugFixer: starting %d pass(es)", self._proj.id, self._num_passes)
        await bus.publish("fix_pass", {"pass": 0, "total": self._num_passes}, self._proj.id)
        await self._proj.log_activity(f"BugFixer: {self._num_passes} passes starting")

        # Pre-pass 1: pin known-broken unpinned packages in requirements.txt
        self._fix_requirements()
        # Pre-pass 2: rewrite passlib → bcrypt (passlib broken on Python 3.13+)
        self._fix_passlib_usage()
        # Pre-pass 2b: fix Alembic env.py for async SQLAlchemy
        self._fix_alembic_env()
        # Pre-pass 3: install project dependencies
        await self._install_deps()
        # Pre-pass 3: ensure every Python package dir has __init__.py
        self._ensure_init_files()
        # Pre-pass 4: fix missing include_router imports before anything else runs
        self._fix_missing_router_imports()
        # Pre-pass 5: fix cross-file import mismatches
        self._fix_cross_file_imports()
        # Pre-pass 6: fix __init__.py that don't re-export submodules referenced in main.py
        self._fix_init_exports()
        # Pre-pass 7: create static/ directory + stub index.html if StaticFiles is mounted
        self._ensure_static_dir()
        # Pre-pass 8: regenerate stub frontend files that are below minimum line count
        await self._fix_frontend_quality()
        # Pre-pass 9: deterministic cross-file consistency (tokenUrl, test URL prefix)
        self._fix_cross_file_consistency()
        # Pre-pass 10: fix FastAPI catch-all static file serving (login.html → 404 bug)
        self._fix_static_file_serving()
        # Pre-pass 11: gate /docs behind ENABLE_DOCS env var
        self._fix_docs_exposure()
        # Pre-pass 12: replace fake setTimeout logins with real API calls
        self._fix_fake_login_forms()
        # Pre-pass 12: inject accessibility toolbar into HTML pages that load app.js
        self._inject_accessibility_toolbar()
        # Pre-pass 13: inject forgot-password endpoint into auth routers that are missing it
        self._inject_forgot_password()

        for i in range(1, self._num_passes + 1):
            files = self._collect_files()
            if not files:
                log.info("[%s] No source files found to fix", self._proj.id)
                break

            # Run pytest first — use real failures to guide which files to fix
            pytest_failures = await self._run_pytest()
            if pytest_failures is not None and not pytest_failures:
                log.info("[%s] All tests pass on pass %d — done early", self._proj.id, i)
                await self._proj.log_activity(f"BugFixer pass {i}: all tests pass")
                break

            # Prioritise files with most failures; cap per pass to avoid runaway
            if pytest_failures:
                files = sorted(
                    [f for f in files if str(f.resolve()) in pytest_failures],
                    key=lambda f: -len(pytest_failures.get(str(f.resolve()), [])),
                ) + [f for f in files if str(f.resolve()) not in pytest_failures]
            files = files[:_MAX_FILES_PER_PASS]

            log.info("[%s] Fix pass %d/%d — %d files (capped at %d)", self._proj.id, i, self._num_passes, len(files), _MAX_FILES_PER_PASS)
            await bus.publish("fix_pass", {"pass": i, "total": self._num_passes}, self._proj.id)
            await asyncio.gather(*[self._fix_file_guarded(fp, pytest_failures) for fp in files])
            await self._git.commit(f"fix: automated pass {i}")

    def _find_test_dir(self) -> str:
        """Return the best pytest target for this workspace."""
        workspace = self._src_dir.parent
        for candidate in ("tests", "test", "__tests__"):
            if (workspace / candidate).is_dir():
                return candidate
        # No dedicated test dir — let pytest discover from the root
        return "."

    async def _run_pytest(self) -> dict[str, list[str]] | None:
        """Run pytest in the workspace. Returns {source_file: [error_lines]} or None on error.

        Returns None for:
          - pytest error / timeout
          - no tests collected (so BugFixer still runs fixes, not exits early)
        Returns {} only when tests genuinely ran and all passed.

        Keys are ABSOLUTE paths so _fix_file can match them directly via str(path).
        Falls back to a raw-output scan when traceback attribution misses source files.
        """
        workspace = str(self._src_dir.parent)
        workspace_path = Path(workspace).resolve()
        test_dir = self._find_test_dir()
        try:
            from ..pytest_parser import run_pytest_structured
            report = await asyncio.wait_for(
                run_pytest_structured(f"pytest {test_dir}", workspace), timeout=60
            )
            if report is None:
                return None

            # No tests collected at all — treat as "unknown" so we still run fixes
            if report.passed == 0 and report.failed == 0 and report.errors == 0:
                log.info("[%s] pytest: no tests collected — proceeding with fixes", self._proj.id)
                return None

            result: dict[str, list[str]] = {}

            # Convert relative paths → absolute so path_key lookup works
            def _abs(rel: str) -> str:
                p = Path(rel)
                if p.is_absolute():
                    return str(p)
                return str((workspace_path / rel).resolve())

            for tf in report.failures:
                if tf.source_files:
                    for src in tf.source_files:
                        result.setdefault(_abs(src), []).append(tf.short_repr)
                else:
                    # No traceback attribution (e.g. import errors) — mine the
                    # failure text for source file references
                    for m in re.finditer(r'([\w/\\.-]+\.py)[:\",]', tf.long_repr):
                        candidate = m.group(1).replace("\\", "/")
                        if not candidate.startswith("test_") and "/tests/" not in candidate:
                            result.setdefault(_abs(candidate), []).append(tf.short_repr)

            # If failures exist but zero source files attributed, flag the most
            # likely culprits (cap at 8) so healer doesn't spiral on large projects
            if report.failures and not result:
                all_errors = [tf.short_repr for tf in report.failures[:10]]
                # Prefer files mentioned by name in error messages
                error_text = " ".join(all_errors)
                candidates: list[Path] = []
                for src_file in self._collect_files():
                    if src_file.suffix == ".py":
                        if src_file.name in error_text or src_file.stem in error_text:
                            candidates.insert(0, src_file)  # prioritise mentioned files
                        else:
                            candidates.append(src_file)
                for src_file in candidates[:8]:
                    result[str(src_file)] = all_errors

            log.info(
                "[%s] pytest: passed=%d failed=%d attributed_files=%d",
                self._proj.id, report.passed, report.failed, len(result),
            )
            return result
        except asyncio.TimeoutError:
            log.warning("[%s] pytest timed out during BugFixer pre-check", self._proj.id)
            return None
        except Exception as exc:
            log.warning("[%s] Could not run pytest: %s", self._proj.id, exc)
            return None

    async def _fix_file_guarded(self, path: Path, pytest_failures: dict[str, list[str]] | None = None) -> None:
        """Wraps _fix_file with semaphore + per-file timeout."""
        async with self._sem:
            try:
                await asyncio.wait_for(self._fix_file(path, pytest_failures), timeout=_FILE_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("[%s] Timed out fixing %s — skipping", self._proj.id, path.name)
            except Exception as exc:
                log.error("[%s] Error fixing %s: %s", self._proj.id, path.name, exc)

    # Truncation marker: LLM sometimes emits "[...]" instead of completing a file
    _TRUNCATION_RE = re.compile(r'^\s*\[\.{2,}[^\]\n]*\]\s*$', re.MULTILINE)
    # Lone indented identifier at EOF — potential mid-word truncation
    _MIDWORD_TRUNCATION_RE = re.compile(r'\n([ \t]+)([a-z_][a-z0-9_]*)\s*$')
    # Partial assignment value at EOF, e.g. `    default=F` (False cut to F)
    _MIDASSIGN_TRUNCATION_RE = re.compile(r'\n[ \t]+\w+\s*=\s*[A-Z]\w{0,4}\s*$')
    # Python keywords valid as last token — not truncation
    _VALID_LAST_WORDS = frozenset({
        'pass', 'return', 'break', 'continue', 'else', 'finally', 'raise', 'yield',
        'true', 'false', 'none', 'and', 'or', 'not', 'in', 'is',
    })

    def _is_content_truncated(self, content: str) -> bool:
        """True if content appears LLM-truncated (mirrors executor._is_content_truncated)."""
        if bool(self._TRUNCATION_RE.search(content)):
            return True
        if bool(self._MIDASSIGN_TRUNCATION_RE.search(content)):
            return True
        m = self._MIDWORD_TRUNCATION_RE.search(content)
        if m:
            word = m.group(2).lower()
            if word not in self._VALID_LAST_WORDS:
                return True
        return False

    async def _fix_file(self, path: Path, pytest_failures: dict[str, list[str]] | None = None) -> None:
        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return
        if len(content.splitlines()) > cfg.safety.max_lines_per_fix:
            return

        # ── Determine issues: prefer real pytest failures over LLM triage ──
        # Keys in pytest_failures are absolute paths (resolved in _run_pytest)
        path_key = str(path.resolve())
        pytest_errors = pytest_failures.get(path_key) if pytest_failures else None

        # Detect truncated files — always force regeneration regardless of pytest status
        is_truncated = self._is_content_truncated(content)

        if is_truncated:
            issues = ["File content is truncated/incomplete (contains '[...]' placeholder)"]
            issues_str = issues[0]
        elif pytest_errors:
            # We have real test failures pointing at this file — skip LLM triage
            issues = [e[:200] for e in pytest_errors[:5]]
            issues_str = "\n".join(f"- {i}" for i in issues)
        else:
            # No pytest data and not truncated — file appears healthy, skip LLM entirely
            return

        if issues:
            await bus.publish("bug_found", {"file": path.name, "issues": issues}, self._proj.id)
            log.info("[%s] Bug found: %s — %s", self._proj.id, path.name, issues[:2])

        # ── Truncated files: skip to full regeneration immediately ───────
        if is_truncated:
            log.info("[%s] Truncated file detected, regenerating: %s", self._proj.id, path.name)
            regen = await self._regenerate_file(path, content, issues_str)
            if regen:
                ok, err = _syntax_ok(path, regen)
                if ok:
                    path.write_text(regen, encoding="utf-8")
                    self._files_fixed += 1
                    await bus.publish("bug_fixed", {"file": path.name, "regenerated": True}, self._proj.id)
                    log.info("[%s] Regenerated truncated file: %s", self._proj.id, path.name)
                    return
                log.warning("[%s] Regeneration of truncated file had syntax error (%s): %s", self._proj.id, err, path.name)
            # Fall through to normal stages if regen failed

        # ── Stage 0: deterministic quick-fix (no LLM) ────────────────────
        quick = _try_deterministic_fix(path, content, issues)
        if quick:
            path.write_text(quick, encoding="utf-8")
            self._files_fixed += 1
            await bus.publish("bug_fixed", {"file": path.name, "quick": True}, self._proj.id)
            log.info("[%s] Quick-fixed (no LLM): %s", self._proj.id, path.name)
            return

        # ── PatchCache check ──────────────────────────────────────────────
        cache_key = _fix_key(path, issues_str, content)
        cached = self._patch_cache.get(cache_key)
        if cached and str(path) in cached:
            ok, _ = _syntax_ok(path, cached[str(path)])
            if ok:
                path.write_text(cached[str(path)], encoding="utf-8")
                self._cache_hits += 1
                self._files_fixed += 1
                await bus.publish("bug_fixed", {"file": path.name, "cache_hit": True}, self._proj.id)
                return

        # ── Stage 1 + Stage 3 raced in parallel ──────────────────────────
        # Start analyze+fix and full-regen simultaneously; take the first
        # valid result, cancel the other. Eliminates the sequential wait.
        s1 = asyncio.create_task(self._attempt_analyze_and_fix(path, content, issues_str))
        s3 = asyncio.create_task(self._regenerate_file(path, content, issues_str))

        fixed = None
        regenerated = False
        try:
            done, pending = await asyncio.wait(
                [s1, s3], return_when=asyncio.FIRST_COMPLETED
            )
            # Check completed tasks in order: prefer s1 (lighter/cheaper)
            for task in ([s1, s3] if s1 in done else [s3, s1]):
                if task.done() and not task.cancelled() and task.exception() is None:
                    result = task.result()
                    if result:
                        ok, _ = _syntax_ok(path, result)
                        if ok:
                            fixed = result
                            regenerated = (task is s3)
                            break
            # Cancel remaining
            for t in pending:
                t.cancel()
                try: await t
                except Exception: pass
        except Exception as exc:
            log.error("[%s] Stage 1+3 race failed for %s: %s", self._proj.id, path.name, exc)
            for t in [s1, s3]:
                if not t.done(): t.cancel()

        if fixed:
            # Reject if the fix is itself truncated — mid-word cut or [...] placeholder
            fix_is_truncated = self._is_content_truncated(fixed)
            if fix_is_truncated:
                log.warning("[%s] Rejecting fix for %s — healer output is itself truncated; forcing regeneration",
                            self._proj.id, path.name)
                regen = await self._regenerate_file(path, content, issues_str or "healer output was truncated")
                if regen:
                    ok2, _ = _syntax_ok(path, regen)
                    regen_truncated = self._is_content_truncated(regen)
                    if ok2 and not regen_truncated:
                        fixed = regen
                        regenerated = True
                    else:
                        return
                else:
                    return
            # Reject fix if it's drastically shorter than original — LLM truncated again
            if len(fixed.splitlines()) < len(content.splitlines()) * 0.6 and not is_truncated:
                log.warning("[%s] Rejecting fix for %s — output (%d lines) is >40%% shorter than original (%d lines)",
                            self._proj.id, path.name, len(fixed.splitlines()), len(content.splitlines()))
                return
            path.write_text(fixed, encoding="utf-8")
            self._patch_cache.put(cache_key, {str(path): fixed})
            self._files_fixed += 1
            # Keep project_context.json current so subsequent heals have accurate exports
            try:
                rel = str(path.relative_to(self._src_dir.parent)).replace("\\", "/")
                ProjectContextBuilder(self._src_dir.parent).update_from_file(rel, fixed)
            except Exception:
                pass
            await bus.publish("bug_fixed", {"file": path.name, "regenerated": regenerated}, self._proj.id)
            log.info("[%s] %s: %s", self._proj.id, "Regenerated" if regenerated else "Fixed", path.name)
            return
        log.warning("[%s] Both fix stages failed for %s", self._proj.id, path.name)

        # ── Give up ───────────────────────────────────────────────────────
        try:
            self._needs_review.append(str(path.relative_to(self._src_dir)))
        except ValueError:
            self._needs_review.append(path.name)
        log.warning("[%s] Could not fix %s after all stages — needs review", self._proj.id, path.name)

    async def _attempt_analyze_and_fix(
        self, path: Path, content: str, known_errors: str
    ) -> str | None:
        """Single LLM call that both diagnoses and returns the fixed file."""
        ctx_file = self._src_dir.parent / "project_context.json"
        project_context = ctx_file.read_text(encoding="utf-8")[:3000] if ctx_file.exists() else "not available"
        prompt = (
            _ANALYZE_AND_FIX_PROMPT
            .replace("{project_context}", project_context)
            .replace("{path}", path.name)
            .replace("{errors}", known_errors or "none")
            .replace("{content}", content[:12000])
        )
        try:
            resp = await _llm("executor", prompt)
            text = re.sub(r"```(?:json)?\n?", "", resp).strip().rstrip("`")
            m = re.search(r'\{[\s\S]+\}', text)
            if not m:
                return None
            data = json.loads(m.group(0))
            if not data.get("has_issues"):
                return None
            fixed = data.get("fixed_content")
            if fixed and len(str(fixed).strip()) > 10:
                return str(fixed).strip()
        except Exception as exc:
            log.error("[%s] Analyze+fix failed for %s: %s", self._proj.id, path.name, exc)
        return None

    async def _regenerate_file(self, path: Path, broken_content: str, issues_str: str) -> str | None:
        """Full regeneration: collect all sibling context and ask architect to rewrite from scratch."""
        # Build sibling API surfaces for context
        siblings: list[str] = []
        for sibling in sorted(self._src_dir.rglob("*.py")):
            if sibling == path or sibling.name == "__init__.py":
                continue
            try:
                text = sibling.read_text(encoding="utf-8", errors="replace")
                sig_lines = [l for l in text.splitlines()
                             if l.strip().startswith(("def ", "async def ", "class ", "from ", "import "))]
                if sig_lines:
                    siblings.append(f"# {sibling.name}\n" + "\n".join(sig_lines[:20]))
            except OSError:
                pass

        context = "\n\n".join(siblings[:8])  # cap at 8 siblings
        prompt = (
            f"The following file is broken and previous fix attempts failed.\n"
            f"Rewrite it completely from scratch. Preserve the same purpose and public API.\n"
            f"Return ONLY the complete file content. No markdown fences.\n\n"
            f"FILE: {path.name}\n"
            f"KNOWN ISSUES:\n{issues_str}\n\n"
            f"PROJECT CONTEXT (sibling files):\n{context}\n\n"
            f"BROKEN CONTENT:\n```\n{broken_content[:10000]}\n```"
        )
        try:
            resp = await _llm("architect", prompt)
            cleaned = re.sub(r"```\w*\n?", "", resp).strip().rstrip("`").strip()
            if cleaned and len(cleaned) > 20:
                return cleaned
        except Exception as exc:
            log.error("[%s] Regeneration failed for %s: %s", self._proj.id, path.name, exc)
        return None

    # Track requirements file mtimes to avoid redundant pip installs
    _installed_req_mtimes: dict[str, float] = {}

    async def _install_deps(self) -> None:
        """Run pip install only when requirements files have changed since last install.
        Also ensures pytest-json-report is present (required for structured failure parsing).
        """
        import subprocess, sys as _sys
        workspace = self._src_dir.parent

        # Ensure pytest-json-report is installed — BugFixer is blind without it
        try:
            import pytest_jsonreport  # noqa: F401
        except ImportError:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.run(
                        [_sys.executable, "-m", "pip", "install", "pytest-json-report", "-q"],
                        capture_output=True, text=True, timeout=60,
                    ),
                )
                log.info("[%s] Installed pytest-json-report", self._proj.id)
            except Exception as exc:
                log.warning("[%s] Could not install pytest-json-report: %s", self._proj.id, exc)

        for req_file in (workspace / "requirements.txt", workspace / "requirements-dev.txt"):
            if not req_file.exists():
                continue
            mtime = req_file.stat().st_mtime
            key = str(req_file)
            if BugFixer._installed_req_mtimes.get(key) == mtime:
                log.info("[%s] pip install skipped (requirements unchanged): %s", self._proj.id, req_file.name)
                continue
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda f=str(req_file): subprocess.run(
                        [_sys.executable, "-m", "pip", "install", "-r", f, "-q", "--prefer-binary"],
                        capture_output=True, text=True, timeout=180,
                    ),
                )
                if result.returncode == 0:
                    BugFixer._installed_req_mtimes[key] = mtime
                    log.info("[%s] pip install -r %s OK", self._proj.id, req_file.name)
                else:
                    log.warning("[%s] pip install warning: %s", self._proj.id, result.stderr[:200])
            except Exception as exc:
                log.warning("[%s] pip install skipped: %s", self._proj.id, exc)

    # Packages where unpinned = broken — map bare name → minimum safe spec
    _REQ_PINS: dict[str, str] = {
        "fastapi":         "fastapi[standard]>=0.111.0",
        "uvicorn":         "uvicorn[standard]",
        "sqlalchemy":      "sqlalchemy[asyncio]>=2.0",
        "pydantic":        "pydantic>=2.0",
        "pydantic-core":   "pydantic-core>=2.14.0",
        "pydantic-settings": "pydantic-settings>=2.0",
        # passlib is broken on Python 3.13+ (bcrypt removed __about__)
        # Remove it — _fix_passlib_usage() rewrites security.py to use bcrypt directly
        "passlib":         None,
        "passlib[bcrypt]": None,
    }

    def _fix_requirements(self) -> None:
        """Pin known-broken unpinned packages in requirements.txt."""
        req = self._src_dir.parent / "requirements.txt"
        if not req.exists():
            return
        try:
            lines = req.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        changed = False
        removed_passlib = False
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            # Extract bare package name (no version specifier, no extras)
            bare = re.split(r'[>=<!;\[]', stripped)[0].strip().lower()
            if bare in self._REQ_PINS:
                pin = self._REQ_PINS[bare]
                if pin is None:
                    # Remove this package entirely
                    changed = True
                    removed_passlib = removed_passlib or "passlib" in bare
                    log.info("[%s] requirements.txt: removed %s (broken on Python 3.13+)", self._proj.id, bare)
                    continue
                if stripped == bare or stripped == bare.split("[")[0]:
                    new_lines.append(pin)
                    changed = True
                    log.info("[%s] requirements.txt: pinned %s → %s", self._proj.id, bare, pin)
                    continue
            new_lines.append(line)
        # Ensure bcrypt is present when passlib was removed
        if removed_passlib:
            has_bcrypt = any(re.split(r'[>=<!;\[]', l.strip())[0].strip().lower() == "bcrypt"
                             for l in new_lines)
            if not has_bcrypt:
                new_lines.append("bcrypt")
                log.info("[%s] requirements.txt: added bcrypt (passlib replacement)", self._proj.id)
        if changed:
            req.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    _ALEMBIC_ENV_TEMPLATE = '''\
import os
import asyncio
from logging.config import fileConfig
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool
from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from environment so the same DATABASE_URL works everywhere
_db_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./app.db")
config.set_main_option("sqlalchemy.url", _db_url)

# Import Base and ALL models so metadata is fully populated
try:
    from src.database import Base
except ImportError:
    from database import Base

# Auto-discover and import all model modules so their tables are registered
import importlib, pkgutil, pathlib
_models_paths = list(pathlib.Path(".").rglob("models"))
for _mp in _models_paths:
    for _mi in pkgutil.iter_modules([str(_mp)]):
        try:
            importlib.import_module(f"src.models.{_mi.name}")
        except Exception:
            try:
                importlib.import_module(f"models.{_mi.name}")
            except Exception:
                pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
'''

    def _fix_alembic_env(self) -> None:
        """Overwrite alembic/env.py with a correct async SQLAlchemy implementation.

        LLMs consistently generate sync env.py that breaks with async engines.
        This replaces it unconditionally with a known-good async template that:
        - Reads DATABASE_URL from the environment
        - Auto-imports all model modules so Base.metadata is fully populated
        - Handles both offline and online migration modes correctly
        """
        workspace = self._src_dir.parent
        for env_py in list(workspace.rglob("alembic/env.py")) + list(workspace.rglob("migrations/env.py")):
            try:
                current = env_py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Already async and correct?
            if "async_engine_from_config" in current and "asyncio.run" in current:
                continue
            env_py.write_text(self._ALEMBIC_ENV_TEMPLATE, encoding="utf-8")
            log.info("[%s] Alembic env.py rewritten for async SQLAlchemy: %s",
                     self._proj.id, env_py)

    def _fix_passlib_usage(self) -> None:
        """Replace passlib.context.CryptContext with direct bcrypt calls.

        passlib is broken on Python 3.13+ because the bcrypt package removed
        __about__.__version__. We rewrite security.py (or any file using CryptContext)
        to call bcrypt directly — no passlib dependency needed.
        """
        workspace = self._src_dir.parent
        _PASSLIB_IMPORT_RE = re.compile(
            r'^from passlib\.context import CryptContext[^\n]*\n'
            r'(?:from passlib[^\n]*\n)*',
            re.MULTILINE,
        )
        _CTX_DEF_RE = re.compile(
            r'^\w+\s*=\s*CryptContext\([^)]*\)\s*\n',
            re.MULTILINE,
        )
        _VERIFY_RE = re.compile(
            r'(\w+)\.verify\(([^,]+),\s*([^)]+)\)'
        )
        _HASH_RE = re.compile(
            r'(\w+)\.hash\(([^)]+)\)'
        )

        for py_file in list(workspace.rglob("*.py")):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "CryptContext" not in content and "passlib" not in content:
                continue

            modified = content

            # 1. Replace passlib imports with bcrypt import
            if _PASSLIB_IMPORT_RE.search(modified):
                modified = _PASSLIB_IMPORT_RE.sub("import bcrypt as _bcrypt\n", modified)
            elif "from passlib" in modified:
                modified = re.sub(r'^from passlib[^\n]*\n', '', modified, flags=re.MULTILINE)
                if "import bcrypt as _bcrypt" not in modified:
                    modified = "import bcrypt as _bcrypt\n" + modified

            # 2. Remove CryptContext instantiation line
            modified = _CTX_DEF_RE.sub("", modified)

            # 3. Rewrite pwd_context.verify(...) → _bcrypt.checkpw(...)
            modified = _VERIFY_RE.sub(
                lambda m: f"_bcrypt.checkpw({m.group(2).strip()}.encode(), {m.group(3).strip()}.encode())",
                modified,
            )

            # 4. Rewrite pwd_context.hash(...) → _bcrypt.hashpw(...).decode()
            modified = _HASH_RE.sub(
                lambda m: f"_bcrypt.hashpw({m.group(2).strip()}.encode(), _bcrypt.gensalt()).decode()",
                modified,
            )

            if modified != content:
                py_file.write_text(modified, encoding="utf-8")
                log.info("[%s] passlib → bcrypt rewrite: %s", self._proj.id, py_file.name)

    async def _fix_frontend_quality(self) -> None:
        """Pre-pass: detect stub frontend files (below minimum line count) and
        regenerate them with a focused, design-system-aware prompt."""
        static_dir = self._src_dir.parent / "static"
        if not static_dir.exists():
            return

        brief = self._proj.brief.get("raw_text", self._proj.brief.get("description", ""))
        project_name = self._proj.brief.get("name", self._proj.id)

        stubs = []
        for fname, min_lines in _FRONTEND_MIN_LINES.items():
            f = static_dir / fname
            if not f.exists():
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").count("\n")
            if lines < min_lines:
                stubs.append(f)
                log.info("[%s] Frontend stub detected: %s (%d lines < %d)",
                         self._proj.id, fname, lines, min_lines)

        if not stubs:
            return

        await self._proj.log_activity(f"Frontend quality pass: regenerating {len(stubs)} stub file(s)")

        sem = asyncio.Semaphore(3)

        async def _regen(f: Path) -> None:
            async with sem:
                fname = f.name
                reqs = _FRONTEND_REQUIREMENTS.get(fname, f"Write a complete, production-quality {fname} for this project.")
                prompt = _FRONTEND_REGEN_PROMPT.format(
                    project_name=project_name,
                    file_path=str(f.relative_to(self._src_dir.parent)),
                    brief=brief[:1500],
                    content=f.read_text(encoding="utf-8", errors="replace")[:3000],
                    file_name=fname,
                    requirements=reqs,
                )
                try:
                    result = await asyncio.wait_for(_llm("executor", prompt), timeout=120)
                    if result and len(result.strip()) > 200:
                        f.write_text(result.strip(), encoding="utf-8")
                        new_lines = result.count("\n")
                        log.info("[%s] Frontend regenerated: %s (%d lines)", self._proj.id, fname, new_lines)
                        await bus.publish("terminal_line", {"line": f"[FrontendQuality] Regenerated {fname} ({new_lines} lines)"}, self._proj.id)
                except Exception as exc:
                    log.warning("[%s] Frontend regen failed for %s: %s", self._proj.id, fname, exc)

        await asyncio.gather(*[_regen(f) for f in stubs])

    def _fix_missing_router_imports(self) -> None:
        """Pre-pass: scan main.py/app.py for app.include_router(X.router) calls
        that have no corresponding import for X, and add the correct import line."""
        if not self._src_dir.exists():
            return
        for py_file in self._src_dir.rglob("*.py"):
            if py_file.name not in ("main.py", "app.py", "server.py") and "include_router" not in (py_file.read_text(encoding="utf-8", errors="replace") if py_file.exists() else ""):
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "include_router" not in content:
                continue

            routers_pkg = py_file.parent / "routers"
            missing_imports: list[str] = []
            grouped: dict[str, list[str]] = {}  # subpkg → [names]

            for m in _INCLUDE_ROUTER_RE.finditer(content):
                name = m.group(1)
                if re.search(rf'(?:from\s+\S+\s+import.*\b{name}\b|import\s+\S*{name})', content):
                    continue  # already imported
                # Determine correct source
                if (py_file.parent / f"{name}.py").exists():
                    missing_imports.append(f"from . import {name}")
                elif routers_pkg.is_dir() and (routers_pkg / f"{name}.py").exists():
                    grouped.setdefault("routers", []).append(name)
                else:
                    # Search all subdirs
                    found = None
                    for sub in py_file.parent.iterdir():
                        if sub.is_dir() and (sub / f"{name}.py").exists():
                            found = sub.name
                            break
                    if found:
                        grouped.setdefault(found, []).append(name)
                    else:
                        missing_imports.append(f"from . import {name}")

            for subpkg, names in grouped.items():
                missing_imports.append(f"from .{subpkg} import {', '.join(names)}")

            if not missing_imports:
                continue

            # Insert after the last existing import line
            lines = content.splitlines()
            last_import_idx = 0
            for i, line in enumerate(lines):
                if line.startswith(("import ", "from ")):
                    last_import_idx = i
            for stmt in missing_imports:
                lines.insert(last_import_idx + 1, stmt)
                last_import_idx += 1
            py_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            log.info("[%s] Added missing router imports to %s: %s",
                     self._proj.id, py_file.name, missing_imports)

    def _fix_cross_file_imports(self) -> None:
        """Scan all Python files and fix import statements that reference names which
        don't exist in the target local module (e.g. `from .auth import get_user` but
        auth.py exports `get_current_user`)."""
        if not self._src_dir.exists():
            return

        # Build export map: module_stem → set of top-level names
        export_map: dict[str, set[str]] = {}
        for py_file in self._src_dir.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            exports: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    exports.add(node.name)
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            exports.add(t.id)
            export_map[py_file.stem] = exports

        # Check each file's relative imports
        _FROM_IMPORT_RE = re.compile(r'^from\s+\.(\w+)\s+import\s+(.+)$', re.MULTILINE)
        fixes = 0
        for py_file in self._src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            modified = content
            for m in _FROM_IMPORT_RE.finditer(content):
                module, names_str = m.group(1), m.group(2)
                if module not in export_map:
                    continue
                available = export_map[module]
                imported = [n.strip().split(" as ")[0].strip() for n in names_str.split(",")]
                bad = [n for n in imported if n and not n.startswith("*") and n not in available]
                if not bad:
                    continue
                # Try to find closest match by name similarity
                for bad_name in bad:
                    best = min(available, key=lambda x: _edit_distance(x, bad_name), default=None)
                    if best and _edit_distance(best, bad_name) <= max(2, len(bad_name) // 3):
                        old_stmt = m.group(0)
                        new_stmt = old_stmt.replace(bad_name, best)
                        modified = modified.replace(old_stmt, new_stmt, 1)
                        log.info("[%s] Cross-file fix: %s → %s in %s",
                                 self._proj.id, bad_name, best, py_file.name)
                        fixes += 1
            if modified != content:
                py_file.write_text(modified, encoding="utf-8")
        if fixes:
            log.info("[%s] Cross-file import fixes: %d", self._proj.id, fixes)

        # ── Pass 2: fix "from . import X" when X lives in a sub-package ────────
        # e.g. `from . import users` but file is at src/routers/users.py
        # → rewrite to `from .routers import users`
        _FLAT_IMPORT_RE = re.compile(r'^from \. import (.+)$', re.MULTILINE)
        for py_file in self._src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Only process files that actually use include_router or are known entry points
            if py_file.name not in ("main.py", "app.py", "server.py") and "include_router" not in content:
                continue
            modified = content
            for m in _FLAT_IMPORT_RE.finditer(content):
                names = [n.strip() for n in m.group(1).split(",")]
                # Group names by which sub-package they actually live in
                subpkg_groups: dict[str, list[str]] = {}
                flat_names: list[str] = []
                for name in names:
                    # Check direct siblings first
                    if (py_file.parent / f"{name}.py").exists():
                        flat_names.append(name)
                        continue
                    # Search one level of sub-packages
                    found_subpkg = None
                    for sub in py_file.parent.iterdir():
                        if sub.is_dir() and (sub / f"{name}.py").exists():
                            found_subpkg = sub.name
                            break
                    if found_subpkg:
                        subpkg_groups.setdefault(found_subpkg, []).append(name)
                    else:
                        flat_names.append(name)

                if not subpkg_groups:
                    continue  # nothing to fix

                # Rebuild the import block
                new_lines = []
                if flat_names:
                    new_lines.append(f"from . import {', '.join(flat_names)}")
                for subpkg, subnames in subpkg_groups.items():
                    new_lines.append(f"from .{subpkg} import {', '.join(subnames)}")
                modified = modified.replace(m.group(0), "\n".join(new_lines), 1)
                log.info("[%s] Subpkg import fix in %s: %s → %s",
                         self._proj.id, py_file.name, m.group(0), new_lines)

            if modified != content:
                py_file.write_text(modified, encoding="utf-8")

        # ── Pass 3: fix multi-level relative imports to non-existent modules ──
        # e.g. `from ..services.test import foo` but file is `test_service.py`
        # Strategy: if the module path doesn't resolve to an existing file,
        # look for the closest-named file in the same directory.
        _MULTI_IMPORT_RE = re.compile(r'^(from\s+(\.\w[\w.]*)\s+import\s+.+)$', re.MULTILINE)
        workspace = self._src_dir.parent
        for py_file in self._src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            modified = content
            for m in _MULTI_IMPORT_RE.finditer(content):
                full_stmt, dotted = m.group(1), m.group(2)
                # Count leading dots to determine parent levels
                dots = len(dotted) - len(dotted.lstrip("."))
                parts = dotted.lstrip(".").split(".")
                if len(parts) < 2:
                    continue  # single-level handled by pass 1
                # Resolve the directory this import points to
                base = py_file.parent
                for _ in range(dots - 1):
                    base = base.parent
                pkg_dir = base.joinpath(*parts[:-1])
                module_name = parts[-1]
                target = pkg_dir / f"{module_name}.py"
                if target.exists():
                    continue  # fine
                # Find best match in pkg_dir
                if not pkg_dir.is_dir():
                    continue
                candidates = [p.stem for p in pkg_dir.glob("*.py") if p.stem != "__init__"]
                if not candidates:
                    continue
                best = min(candidates, key=lambda x: _edit_distance(x, module_name))
                if _edit_distance(best, module_name) <= max(3, len(module_name) // 2):
                    new_stmt = full_stmt.replace(
                        dotted, dotted[: -len(module_name)] + best, 1
                    )
                    modified = modified.replace(full_stmt, new_stmt, 1)
                    log.info("[%s] Module name fix in %s: %s → %s",
                             self._proj.id, py_file.name, module_name, best)
            if modified != content:
                py_file.write_text(modified, encoding="utf-8")

    # ── Cross-file consistency regexes ────────────────────────────────────────
    # Matches the broken pattern: serve only "{full_path}.html" without checking direct path first
    _STATIC_SERVE_RE = re.compile(
        r'([ \t]*)(\w+)\s*=\s*(\w+)\s*/\s*f["\'][^"\']*\{(\w+)\}\.html["\']'
        r'\s*\n\1if\s+\2\.exists\(\)\s*:\s*\n\1[ \t]+return\s+FileResponse\(str\(\2\)\)',
        re.MULTILINE,
    )

    _TOKEN_URL_RE = re.compile(r'OAuth2PasswordBearer\(\s*tokenUrl\s*=\s*["\']([^"\']+)["\']')
    _INCLUDE_ROUTER_RE2 = re.compile(
        r'include_router\(\s*(\w+)\.router[^)]*prefix\s*=\s*["\']([^"\']+)["\']'
    )
    _POST_ROUTE_RE = re.compile(r'@router\.post\(\s*["\']([^"\']*(?:login|token|signin)[^"\']*)["\']', re.IGNORECASE)
    _TEST_URL_RE = re.compile(r'(?:client|ac|async_client)\s*\.\s*(?:get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']')

    def _fix_static_file_serving(self) -> None:
        """Fix FastAPI catch-all routes that serve `{path}.html` without first
        checking if the raw path (e.g. `login.html`) exists directly in static/.

        The LLM consistently generates:
            file_path = static_path / f"{full_path}.html"
            if file_path.exists():
                return FileResponse(str(file_path))

        which means GET /login.html tries static/login.html.html and returns 404.
        We prepend a direct-file check so both /login and /login.html work.
        """
        workspace = self._src_dir.parent
        for entry in ("main.py", "app.py", "src/main.py", "src/app.py"):
            ep = workspace / entry
            if not ep.exists():
                continue
            try:
                content = ep.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            m = self._STATIC_SERVE_RE.search(content)
            if not m:
                continue

            indent    = m.group(1)
            file_var  = m.group(2)   # e.g. "file_path"
            static_var = m.group(3)  # e.g. "static_path"
            path_var  = m.group(4)   # e.g. "full_path"

            # Only patch if a direct-file guard isn't already present
            if f"{static_var} / {path_var}" in content or f"{static_var}/{path_var}" in content:
                continue

            direct_block = (
                f"{indent}# Serve file directly when caller includes extension (e.g. /login.html)\n"
                f"{indent}_direct = {static_var} / {path_var}\n"
                f"{indent}if _direct.exists() and _direct.is_file():\n"
                f"{indent}    return FileResponse(str(_direct))\n"
                f"{indent}\n"
            )
            new_content = content[: m.start()] + direct_block + content[m.start():]
            ep.write_text(new_content, encoding="utf-8")
            log.info("[%s] Static-file serving fix applied to %s", self._proj.id, ep.name)

    def _fix_cross_file_consistency(self) -> None:
        """Deterministic pre-pass: fix cross-file consistency issues that LLM healing
        repeatedly misses because each file is fixed in isolation.

        1. tokenUrl alignment — OAuth2PasswordBearer tokenUrl matches actual login route.
        2. Test URL prefix alignment — test client calls use the prefix registered in main.py.
        """
        workspace = self._src_dir.parent
        fixes = 0

        # ── 1. Build router-prefix map from main.py / app.py ──────────────────
        router_prefix: dict[str, str] = {}   # module_stem → prefix  e.g. "auth" → "/api/auth"
        for entry in ("main.py", "app.py", "src/main.py", "src/app.py"):
            ep = workspace / entry
            if ep.exists():
                try:
                    src = ep.read_text(encoding="utf-8", errors="replace")
                    for m in self._INCLUDE_ROUTER_RE2.finditer(src):
                        router_prefix[m.group(1)] = m.group(2)
                except OSError:
                    pass

        # Also scan src/main.py inside _src_dir
        if self._src_dir.exists():
            for entry in self._src_dir.rglob("main.py"):
                try:
                    src = entry.read_text(encoding="utf-8", errors="replace")
                    for m in self._INCLUDE_ROUTER_RE2.finditer(src):
                        router_prefix[m.group(1)] = m.group(2)
                except OSError:
                    pass

        # ── 2. Fix OAuth2PasswordBearer tokenUrl ──────────────────────────────
        # Find actual login/token endpoint path from router files
        login_path: str | None = None
        if router_prefix and self._src_dir.exists():
            for py_file in self._src_dir.rglob("*.py"):
                try:
                    src = py_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                m = self._POST_ROUTE_RE.search(src)
                if not m:
                    continue
                endpoint = m.group(1).lstrip("/")
                stem = py_file.stem  # e.g. "auth"
                prefix = router_prefix.get(stem, "").lstrip("/")
                if prefix:
                    login_path = f"{prefix}/{endpoint}".lstrip("/")
                else:
                    login_path = endpoint
                break

        if login_path:
            # Find files with OAuth2PasswordBearer and patch tokenUrl if wrong
            search_dirs = [self._src_dir] if self._src_dir.exists() else []
            search_dirs.append(workspace)
            seen: set[Path] = set()
            for d in search_dirs:
                for py_file in (d.rglob("*.py") if d.is_dir() else []):
                    if py_file in seen:
                        continue
                    seen.add(py_file)
                    try:
                        content = py_file.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    tm = self._TOKEN_URL_RE.search(content)
                    if not tm:
                        continue
                    current_url = tm.group(1).lstrip("/")
                    if current_url != login_path:
                        new_content = content[:tm.start(1)] + login_path + content[tm.end(1):]
                        py_file.write_text(new_content, encoding="utf-8")
                        log.info("[%s] tokenUrl fix in %s: '%s' → '%s'",
                                 self._proj.id, py_file.name, current_url, login_path)
                        fixes += 1

        # ── 3. Fix test file URL prefixes ─────────────────────────────────────
        # Collect test files
        test_dirs: list[Path] = []
        for td in ("tests", "test", "__tests__"):
            p = workspace / td
            if p.is_dir():
                test_dirs.append(p)
        # Also pick up test_*.py at workspace root
        test_files = list(workspace.glob("test_*.py"))
        for td in test_dirs:
            test_files.extend(td.rglob("test_*.py"))

        if router_prefix:
            for tf in test_files:
                try:
                    content = tf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                modified = content
                # For each URL path in the test file, check if prefix is correct
                for m in self._TEST_URL_RE.finditer(content):
                    url = m.group(1)
                    if not url.startswith("/"):
                        continue
                    # Try to identify which router this test is calling
                    for stem, expected_prefix in router_prefix.items():
                        # heuristic: stem appears in url segment or file name
                        if stem not in tf.stem and stem not in url:
                            continue
                        # Check if url starts with a different prefix that should be expected_prefix
                        # e.g. url="/api/v1/auth/login" but expected="/api/auth/login"
                        parts = url.lstrip("/").split("/")
                        # Find where stem-related segment starts
                        for i, part in enumerate(parts):
                            if part == stem or (stem in part and len(stem) > 3):
                                actual_prefix = "/" + "/".join(parts[:i])
                                expected = expected_prefix.rstrip("/")
                                if actual_prefix != expected and actual_prefix:
                                    # Replace only the prefix portion
                                    rest = "/" + "/".join(parts[i:])
                                    new_url = expected + rest
                                    old_full = m.group(0)
                                    new_full = old_full.replace(url, new_url, 1)
                                    modified = modified.replace(old_full, new_full, 1)
                                    log.info("[%s] Test URL prefix fix in %s: '%s' → '%s'",
                                             self._proj.id, tf.name, url, new_url)
                                    fixes += 1
                                break
                if modified != content:
                    tf.write_text(modified, encoding="utf-8")

        if fixes:
            log.info("[%s] Cross-file consistency: %d fix(es) applied", self._proj.id, fixes)

    # ── Fake-login / accessibility / forgot-password fixes ────────────────────
    _FAKE_LOGIN_RE = re.compile(
        r'(addEventListener\(["\']submit["\'][^{]*\{[^}]*?)setTimeout\s*\([^,]+,\s*\d+\)',
        re.DOTALL,
    )
    _A11Y_TOOLBAR_HTML = '''\
    <!-- Accessibility Toolbar (injected by agent) -->
    <div id="a11y-toolbar" style="position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);
        display:flex;gap:0.5rem;background:var(--surface,#18181b);border:1px solid var(--border,#3f3f46);
        border-radius:2rem;padding:0.5rem 1rem;box-shadow:0 4px 24px rgba(0,0,0,.5);z-index:9999;"
        role="toolbar" aria-label="Accessibility options">
      <button onclick="Accessibility.updateFontSize(2)" aria-label="Increase font size" title="Increase font size"
        style="background:var(--surface-2,#27272a);border:1px solid var(--border,#3f3f46);color:var(--text,#fafafa);
        border-radius:1rem;padding:.35rem .75rem;cursor:pointer;font-weight:700;font-size:.85rem">+A</button>
      <button onclick="Accessibility.updateFontSize(-2)" aria-label="Decrease font size" title="Decrease font size"
        style="background:var(--surface-2,#27272a);border:1px solid var(--border,#3f3f46);color:var(--text,#fafafa);
        border-radius:1rem;padding:.35rem .75rem;cursor:pointer;font-weight:700;font-size:.85rem">−A</button>
      <button onclick="Accessibility.toggleHighContrast()" aria-label="Toggle high contrast" title="Toggle high contrast"
        style="background:var(--surface-2,#27272a);border:1px solid var(--border,#3f3f46);color:var(--text,#fafafa);
        border-radius:1rem;padding:.35rem .75rem;cursor:pointer;font-size:.8rem">Contrast</button>
      <button onclick="Accessibility.toggleDyslexiaFont()" aria-label="Toggle dyslexia font" title="Toggle dyslexia font"
        style="background:var(--surface-2,#27272a);border:1px solid var(--border,#3f3f46);color:var(--text,#fafafa);
        border-radius:1rem;padding:.35rem .75rem;cursor:pointer;font-size:.8rem">Dyslexia</button>
    </div>
'''
    _REAL_LOGIN_SCRIPT = '''\
        // Real API login (injected by agent — replaces fake setTimeout)
        const _loginForm = document.getElementById('login-form');
        if (_loginForm && typeof Auth !== 'undefined') {
            _loginForm.addEventListener('submit', async function(e) {
                e.preventDefault();
                const btn = e.target.querySelector('button[type="submit"]') || e.target.querySelector('button');
                const email = (document.getElementById('username') || document.getElementById('email')).value;
                const password = document.getElementById('password').value;
                const errEl = document.getElementById('error-area') || document.getElementById('error-message');
                if (btn) { btn.disabled = true; btn.textContent = 'Signing in…'; }
                if (errEl) errEl.style.display = 'none';
                try {
                    await Auth.login(email, password);
                } catch {
                    if (errEl) { errEl.textContent = 'Invalid email or password.'; errEl.style.display = 'block'; }
                    if (btn) { btn.disabled = false; btn.textContent = 'Sign In'; }
                }
            }, { once: true });
        }
'''
    _FORGOT_PASSWORD_ENDPOINTS = '''\

import secrets as _secrets
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
from pydantic import BaseModel as _BM
from sqlalchemy import select as _sel

class _ForgotReq(_BM):
    email: str

class _ResetReq(_BM):
    token: str
    new_password: str

@auth_router.post("/forgot-password")
async def forgot_password(body: _ForgotReq, db: AsyncSession = Depends(get_db)):
    from ..models.user import User as _User
    result = await db.execute(_sel(_User).where(_User.email == body.email))
    user = result.scalar_one_or_none()
    if not user:
        return {"message": "If that email exists, a reset token has been generated."}
    token = _secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_token_expiry = _dt.now(_tz.utc) + _td(hours=1)
    await db.commit()
    return {"message": "Reset token generated.", "reset_token": token}

@auth_router.post("/reset-password")
async def reset_password(body: _ResetReq, db: AsyncSession = Depends(get_db)):
    from ..models.user import User as _User
    from ..utils.security import get_password_hash as _gph
    result = await db.execute(_sel(_User).where(_User.reset_token == body.token))
    user = result.scalar_one_or_none()
    if not user or not getattr(user, "reset_token_expiry", None):
        raise HTTPException(status_code=400, detail="Invalid or expired reset token.")
    expiry = user.reset_token_expiry
    if not expiry.tzinfo:
        expiry = expiry.replace(tzinfo=_tz.utc)
    if _dt.now(_tz.utc) > expiry:
        raise HTTPException(status_code=400, detail="Reset token has expired.")
    user.hashed_password = _gph(body.new_password)
    user.reset_token = None
    user.reset_token_expiry = None
    await db.commit()
    return {"message": "Password reset successfully. You can now log in."}
'''

    def _fix_docs_exposure(self) -> None:
        """Gate FastAPI /docs and /redoc behind ENABLE_DOCS env var.

        LLMs often generate `FastAPI(title=...)` without setting docs_url=None,
        which exposes the full API surface to anyone who visits /docs.
        """
        workspace = self._src_dir.parent
        _FASTAPI_CALL_RE = re.compile(
            r'(FastAPI\([^)]*)\)',
            re.DOTALL,
        )
        for entry in ("main.py", "app.py", "src/main.py", "src/app.py", "run.py"):
            ep = workspace / entry
            if not ep.exists():
                continue
            try:
                content = ep.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "docs_url" in content:
                continue  # already configured
            if "FastAPI(" not in content:
                continue

            def _patch_fastapi_call(m: re.Match) -> str:
                inner = m.group(1)
                # Add env-gated docs_url + redoc_url
                addition = (
                    ',\n    docs_url="/docs" if __import__("os").getenv("ENABLE_DOCS") else None,'
                    '\n    redoc_url=None'
                )
                return inner + addition + ")"

            new_content = _FASTAPI_CALL_RE.sub(_patch_fastapi_call, content, count=1)
            if new_content != content:
                ep.write_text(new_content, encoding="utf-8")
                log.info("[%s] /docs gated behind ENABLE_DOCS in %s", self._proj.id, ep.name)

    def _fix_fake_login_forms(self) -> None:
        """Replace setTimeout-based fake login forms with real Auth.login() API calls.

        LLMs often generate login forms that simulate auth with a delay and redirect
        by role without ever calling the backend. This detects that pattern in HTML
        files and appends a script block that wires the form to the real API.
        """
        workspace = self._src_dir.parent
        static_dir = workspace / "static"
        if not static_dir.exists():
            return

        for html_file in static_dir.glob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Only target login pages
            if "login" not in html_file.stem.lower():
                continue
            # Already wired to real API?
            if "Auth.login(" in content or "fetchWithAuth" in content:
                continue
            # Has the fake setTimeout pattern?
            if "setTimeout" not in content or "login-form" not in content:
                continue
            # Inject real login script before </body>
            if "</body>" not in content:
                continue
            inject = f"\n    <script>\n{self._REAL_LOGIN_SCRIPT}    </script>\n</body>"
            new_content = content.replace("</body>", inject, 1)
            html_file.write_text(new_content, encoding="utf-8")
            log.info("[%s] Fake login fix applied to %s", self._proj.id, html_file.name)

    def _inject_accessibility_toolbar(self) -> None:
        """Inject the +A/−A/Contrast/Dyslexia toolbar into HTML pages that load app.js
        but don't already have an accessibility toolbar."""
        workspace = self._src_dir.parent
        static_dir = workspace / "static"
        if not static_dir.exists():
            return

        for html_file in static_dir.glob("*.html"):
            try:
                content = html_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Only pages that load app.js (meaning Accessibility object is available)
            if "app.js" not in content:
                continue
            # Already has toolbar?
            if "a11y-toolbar" in content or "Accessibility.updateFontSize" in content:
                continue
            if "</body>" not in content:
                continue
            new_content = content.replace("</body>", self._A11Y_TOOLBAR_HTML + "</body>", 1)
            html_file.write_text(new_content, encoding="utf-8")
            log.info("[%s] Accessibility toolbar injected into %s", self._proj.id, html_file.name)

    def _inject_forgot_password(self) -> None:
        """Add forgot-password and reset-password endpoints to auth routers that
        don't already define them. Also adds reset_token columns to User model."""
        workspace = self._src_dir.parent

        # ── 1. Add reset_token fields to User model if missing ──────────────
        for user_model in list(workspace.rglob("models/user.py")):
            try:
                content = user_model.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "reset_token" in content:
                continue
            # Add Text import if needed
            if "Text" not in content:
                content = re.sub(
                    r'(from sqlalchemy import[^\n]+)',
                    lambda m: m.group(0) if "Text" in m.group(0) else m.group(0).rstrip() + ", Text",
                    content, count=1,
                )
            # Inject columns before first relationship or __repr__
            inject_col = (
                "\n    # Password reset\n"
                "    reset_token = Column(Text, nullable=True)\n"
                "    reset_token_expiry = Column(DateTime(timezone=True), nullable=True)\n"
            )
            for marker in ("    # Relationships", "    def __repr__", "    enrolled_", "    teaching_"):
                if marker in content:
                    content = content.replace(marker, inject_col + marker, 1)
                    break
            user_model.write_text(content, encoding="utf-8")
            log.info("[%s] Added reset_token fields to %s", self._proj.id, user_model.name)

        # ── 2. Add endpoints to auth router ─────────────────────────────────
        for auth_router_file in list(workspace.rglob("routers/auth.py")):
            try:
                content = auth_router_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "forgot-password" in content or "forgot_password" in content:
                continue
            # Append endpoints at end of file
            content = content.rstrip() + self._FORGOT_PASSWORD_ENDPOINTS + "\n"
            auth_router_file.write_text(content, encoding="utf-8")
            log.info("[%s] Injected forgot/reset-password endpoints into %s",
                     self._proj.id, auth_router_file.name)

    def _ensure_init_files(self) -> None:
        """Create missing __init__.py in every Python package directory.
        A directory is considered a package if it contains any .py file."""
        if not self._src_dir.exists():
            return
        created = 0
        for py_file in self._src_dir.rglob("*.py"):
            pkg_dir = py_file.parent
            init = pkg_dir / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8")
                created += 1
                log.info("[%s] Created missing __init__.py: %s", self._proj.id, pkg_dir)
        if created:
            log.info("[%s] Created %d missing __init__.py file(s)", self._proj.id, created)

    # ── `from pkg import name` where name lives in a subdir ─────────────────
    # Use [ \t] not \s — \s matches newlines and would greedily eat subsequent import lines
    _PKG_IMPORT_RE = re.compile(
        r'^from\s+([\w.]+)\s+import\s+([\w,\t ]+)', re.MULTILINE
    )

    def _fix_init_exports(self) -> None:
        """Fix __init__.py files that don't re-export submodules referenced in
        entry-point files (main.py / app.py / server.py / run.py).

        Pattern caught:
            main.py:  from src.api import auth, users
            src/api/__init__.py: (empty)
            src/api/routers/auth.py: (exists)
            → adds  `from .routers import auth, users`  to src/api/__init__.py
        """
        if not self._src_dir.exists():
            return
        workspace = self._src_dir.parent
        entry_names = {"main.py", "app.py", "server.py", "run.py", "wsgi.py", "asgi.py"}
        entry_files = [f for f in self._src_dir.rglob("*.py") if f.name in entry_names]
        if not entry_files:
            return

        for entry in entry_files:
            try:
                content = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in self._PKG_IMPORT_RE.finditer(content):
                pkg_dotted = m.group(1)          # e.g. "src.api"
                names_raw  = m.group(2)          # e.g. "auth, users"
                names = [n.strip() for n in names_raw.split(",") if n.strip().isidentifier()]
                # Resolve package path relative to workspace
                pkg_path = workspace / Path(*pkg_dotted.split("."))
                if not pkg_path.is_dir():
                    continue
                init_file = pkg_path / "__init__.py"
                if not init_file.exists():
                    continue
                init_content = init_file.read_text(encoding="utf-8", errors="replace")
                additions: list[str] = []
                for name in names:
                    # Already exported?
                    if re.search(rf'\bimport\s+.*\b{re.escape(name)}\b', init_content):
                        continue
                    # Is it a direct submodule?
                    if (pkg_path / f"{name}.py").exists():
                        continue  # Python resolves this without __init__ help
                    # Search one level deeper for the submodule
                    found_subpkg: str | None = None
                    for sub in pkg_path.iterdir():
                        if sub.is_dir() and (sub / f"{name}.py").exists():
                            found_subpkg = sub.name
                            break
                    if found_subpkg:
                        additions.append(f"from .{found_subpkg} import {name}")
                if additions:
                    new_init = init_content.rstrip() + "\n" + "\n".join(additions) + "\n"
                    init_file.write_text(new_init, encoding="utf-8")
                    log.info("[%s] __init__.py patched: %s → added %s",
                             self._proj.id, init_file, additions)

    # ── Static files directory ────────────────────────────────────────────────
    _STATIC_DIR_RE = re.compile(r'StaticFiles\s*\(\s*directory\s*=\s*["\'](\w+)["\']')

    def _ensure_static_dir(self) -> None:
        """If any Python file mounts StaticFiles(directory='static'), ensure the
        directory and a minimal index.html exist so the app starts without errors."""
        if not self._src_dir.exists():
            return
        workspace = self._src_dir.parent
        for py_file in self._src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "StaticFiles" not in content:
                continue
            for m in self._STATIC_DIR_RE.finditer(content):
                dir_name = m.group(1)  # e.g. "static"
                # Resolve relative to the Python file's directory and workspace root
                for base in (py_file.parent, workspace):
                    static_dir = base / dir_name
                    index_html = static_dir / "index.html"
                    if not static_dir.exists():
                        static_dir.mkdir(parents=True, exist_ok=True)
                        log.info("[%s] Created missing static dir: %s", self._proj.id, static_dir)
                    if not index_html.exists():
                        index_html.write_text(
                            "<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"UTF-8\">"
                            "<title>App</title></head>\n<body><h1>Welcome</h1></body>\n</html>\n",
                            encoding="utf-8",
                        )
                        log.info("[%s] Created stub index.html: %s", self._proj.id, index_html)
                    break  # only create once

    def _collect_files(self) -> list[Path]:
        if not self._src_dir.exists():
            return []
        blocklist = cfg.safety.blocklist
        files: list[Path] = []
        for ext in _EXTS:
            for fp in self._src_dir.rglob(ext):
                if not any(fnmatch.fnmatch(fp.name, p) for p in blocklist):
                    files.append(fp)
        return sorted(files)

    async def _write_needs_review(self) -> None:
        review = self._src_dir.parent / "needs-review.md"
        lines = ["# Files Requiring Manual Review\n",
                 "The agent could not automatically fix these files:\n"]
        for f in self._needs_review:
            lines.append(f"- `{f}`")
        review.write_text("\n".join(lines), encoding="utf-8")
        log.info("[%s] needs-review.md written (%d files)", self._proj.id, len(self._needs_review))
