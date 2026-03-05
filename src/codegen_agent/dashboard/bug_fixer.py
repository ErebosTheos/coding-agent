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

log = logging.getLogger(__name__)

_TRIAGE_PROMPT = """\
Review this code file for bugs, syntax errors, logic issues, or missing imports.
Return ONLY JSON — no markdown:
{"has_issues": true/false, "severity": "none|minor|major", "issues": ["brief description"]}

FILE: {path}
```
{content}
```
"""

_FIX_PROMPT = """\
Fix the following issues in this file. Return ONLY the complete corrected file content.
No explanations. No markdown fences. Just the fixed code.

FILE: {path}
ISSUES:
{issues}

CURRENT CONTENT:
```
{content}
```
"""

_HARD_FIX_PROMPT = """\
A previous fix attempt failed syntax validation. Carefully rewrite this file to fix all issues.
Preserve all intended functionality. Return ONLY the complete corrected file content.

FILE: {path}
ISSUES: {issues}
CURRENT CONTENT:
```
{content}
```
"""

_EXTS = (
    "*.py", "*.js", "*.ts", "*.jsx", "*.tsx",
    "*.html", "*.css", "*.go", "*.rs", "*.java",
)


async def _llm(role: str, prompt: str) -> str:
    from ..llm.router import LLMRouter
    router = LLMRouter()
    client = router.get_client_for_role(role)
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

    async def run(self) -> None:
        log.info("[%s] BugFixer: starting %d pass(es)", self._proj.id, self._num_passes)
        await bus.publish("fix_pass", {"pass": 0, "total": self._num_passes}, self._proj.id)
        await self._proj.log_activity(f"BugFixer: {self._num_passes} passes starting")

        for i in range(1, self._num_passes + 1):
            files = self._collect_files()
            if not files:
                log.info("[%s] No source files found to fix", self._proj.id)
                break
            log.info("[%s] Fix pass %d/%d — %d files", self._proj.id, i, self._num_passes, len(files))
            await bus.publish("fix_pass", {"pass": i, "total": self._num_passes}, self._proj.id)
            for fp in files:
                await self._fix_file(fp)
            await self._git.commit(f"fix: automated pass {i}")

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

    async def _fix_file(self, path: Path) -> None:
        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return
        if len(content.splitlines()) > cfg.safety.max_lines_per_fix:
            return

        # ── Triage ────────────────────────────────────────────────────────
        try:
            triage_raw = await _llm(
                "healer",
                _TRIAGE_PROMPT.replace("{path}", path.name).replace("{content}", content[:4000]),
            )
            triage_text = re.sub(r"```(?:json)?\n?", "", triage_raw).strip().rstrip("`")
            m = re.search(r'\{[\s\S]+\}', triage_text)
            if not m:
                return
            triage = json.loads(m.group(0))
        except Exception:
            return

        if not triage.get("has_issues") or triage.get("severity") == "none":
            return

        issues = triage.get("issues", [])
        issues_str = "\n".join(f"- {i}" for i in issues)
        await bus.publish("bug_found", {"file": path.name, "issues": issues}, self._proj.id)
        log.info("[%s] Bug found: %s — %s", self._proj.id, path.name, issues[:2])

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

        # ── Stage 1: fix (executor role = mid-tier) ────────────────────────
        fixed = await self._attempt_fix(path, content, issues_str, "executor")

        if fixed:
            ok, err = _syntax_ok(path, fixed)
            if ok:
                path.write_text(fixed, encoding="utf-8")
                self._patch_cache.put(cache_key, {str(path): fixed})
                self._files_fixed += 1
                await bus.publish("bug_fixed", {"file": path.name}, self._proj.id)
                log.info("[%s] Fixed: %s", self._proj.id, path.name)
                return
            log.info("[%s] Stage-1 fix invalid syntax (%s), escalating: %s", self._proj.id, err, path.name)

        # ── Stage 2: hard fix (architect role = high-tier) ─────────────────
        fixed = await self._attempt_fix(path, content, issues_str, "architect", hard=True)

        if fixed:
            ok, err = _syntax_ok(path, fixed)
            if ok:
                path.write_text(fixed, encoding="utf-8")
                self._patch_cache.put(cache_key, {str(path): fixed})
                self._files_fixed += 1
                await bus.publish("bug_fixed", {"file": path.name, "escalated": True}, self._proj.id)
                log.info("[%s] Hard-fixed: %s", self._proj.id, path.name)
                return
            log.warning("[%s] Hard fix also has syntax error (%s): %s", self._proj.id, err, path.name)

        # ── Give up ───────────────────────────────────────────────────────
        try:
            self._needs_review.append(str(path.relative_to(self._src_dir)))
        except ValueError:
            self._needs_review.append(path.name)
        log.warning("[%s] Could not fix %s — needs review", self._proj.id, path.name)

    async def _attempt_fix(
        self, path: Path, content: str, issues_str: str, role: str, hard: bool = False
    ) -> str | None:
        template = _HARD_FIX_PROMPT if hard else _FIX_PROMPT
        prompt = (
            template
            .replace("{path}", path.name)
            .replace("{issues}", issues_str)
            .replace("{content}", content[:6000])
        )
        try:
            resp = await _llm(role, prompt)
            cleaned = re.sub(r"```\w*\n?", "", resp).strip().rstrip("`").strip()
            if cleaned and len(cleaned) > 10:
                return cleaned
        except Exception as exc:
            log.error("[%s] Fix attempt failed for %s: %s", self._proj.id, path.name, exc)
        return None

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
