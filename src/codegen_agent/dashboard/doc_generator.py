"""Post-build documentation generator: README, API docs, inline comments."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .event_bus import bus
from .git_manager import GitManager
from .project_registry import Project

log = logging.getLogger(__name__)

_README_PROMPT = """\
Write a comprehensive README.md for the following project.

PROJECT INFO:
Name: {name}
Description: {description}
Tech Stack: {tech_stack}
Features:
{features}

SOURCE FILES:
{source_files}

The README must include:
1. Project title and description
2. Features list
3. Tech stack section
4. Prerequisites and Installation
5. Usage / Quick Start with code examples
6. Configuration section
7. Contributing guidelines
8. License (MIT)

Write professional, clear markdown. Use code blocks for commands.
"""

_API_DOC_PROMPT = """\
Generate concise API documentation for these source files.
List all public functions/classes/endpoints with their signatures and one-line descriptions.

FILES:
{files_content}

Output as markdown with proper headers.
"""

_COMMENT_PROMPT = """\
Add helpful inline comments to this code file. Keep all existing comments.
Only add comments where the logic is non-obvious. Return the complete file with comments added.
Do not change any functionality.

FILE: {path}
```
{content}
```
"""


async def _llm(prompt: str) -> str:
    from ..llm.router import LLMRouter
    router = LLMRouter()
    client = router.get_client_for_role("executor")
    return await client.generate(prompt)


class DocGenerator:
    def __init__(self, proj: Project, src_dir: str) -> None:
        self._proj = proj
        self._src_dir = Path(src_dir)
        self._git = GitManager(proj.id, src_dir)

    async def run(self) -> None:
        log.info("[%s] Doc generator starting", self._proj.id)
        await bus.publish("docs_start", {}, self._proj.id)
        await self._proj.log_activity("Documentation phase started")

        await self._generate_readme()
        await self._generate_api_docs()
        await self._add_inline_comments()
        await self._git.commit("docs: auto-generated documentation")

        await bus.publish("docs_done", {}, self._proj.id)
        await self._proj.log_activity("Documentation phase complete")
        log.info("[%s] Documentation complete", self._proj.id)

    async def _generate_readme(self) -> None:
        brief = self._proj.brief
        plan = self._proj.build_plan
        stack = plan.get("tech_stack", {})

        if isinstance(stack, str):
            tech_stack_str = stack
        else:
            tech_stack_str = " / ".join(filter(None, [
                stack.get("language"), stack.get("backend"), stack.get("frontend"),
            ])) or "N/A"

        source_files = self._list_source_files()
        features = brief.get("features", [])

        prompt = (
            _README_PROMPT
            .replace("{name}", brief.get("name", self._proj.id))
            .replace("{description}", brief.get("description", ""))
            .replace("{tech_stack}", tech_stack_str)
            .replace("{features}", "\n".join(f"- {f}" for f in features))
            .replace("{source_files}", "\n".join(f"- {f}" for f in source_files[:30]))
        )

        content = await _llm(prompt)
        readme = self._src_dir / "README.md"
        readme.write_text(content, encoding="utf-8")
        await self._proj.inc_stat("files_created")
        log.info("[%s] README.md written", self._proj.id)

    async def _generate_api_docs(self) -> None:
        backend_files = list(self._src_dir.rglob("*.py"))[:10]
        if not backend_files:
            backend_files = list(self._src_dir.rglob("*.js"))[:10]
        if not backend_files:
            return

        files_content = []
        for fp in backend_files[:5]:
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated)"
                rel = fp.relative_to(self._src_dir)
                files_content.append(f"### {rel}\n```\n{content}\n```")
            except Exception:
                pass

        if not files_content:
            return

        prompt = _API_DOC_PROMPT.replace("{files_content}", "\n\n".join(files_content))
        content = await _llm(prompt)
        api_doc = self._src_dir / "API.md"
        api_doc.write_text(content, encoding="utf-8")
        await self._proj.inc_stat("files_created")
        log.info("[%s] API.md written", self._proj.id)

    async def _add_inline_comments(self) -> None:
        """Add inline comments to high-complexity files from the build plan."""
        plan = self._proj.build_plan
        high_complexity: list[str] = []

        for phase in plan.get("phases", []):
            for task in phase.get("tasks", []):
                if task.get("complexity") == "high":
                    high_complexity.extend(task.get("files", []))

        for rel_path in high_complexity[:10]:
            fp = self._src_dir / rel_path
            if not fp.exists():
                continue
            content = fp.read_text(encoding="utf-8", errors="replace")
            if len(content.splitlines()) > 200 or len(content) > 6000:
                continue

            try:
                prompt = (
                    _COMMENT_PROMPT
                    .replace("{path}", rel_path)
                    .replace("{content}", content)
                )
                commented = await _llm(prompt)
                cleaned = re.sub(r"```\w*\n?", "", commented).strip().rstrip("`").strip()
                if cleaned and len(cleaned) > len(content) // 2:
                    fp.write_text(cleaned, encoding="utf-8")
                    log.debug("[%s] Comments added to %s", self._proj.id, rel_path)
            except Exception as exc:
                log.warning("[%s] Comment generation failed for %s: %s", self._proj.id, rel_path, exc)

    def _list_source_files(self) -> list[str]:
        if not self._src_dir.exists():
            return []
        return [
            str(fp.relative_to(self._src_dir))
            for fp in sorted(self._src_dir.rglob("*"))
            if fp.is_file()
        ]
