"""Per-project git manager: init repo, commit phases, return log."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from .event_bus import bus

log = logging.getLogger(__name__)


class GitManager:
    """Manages all git operations for a single project workspace."""

    def __init__(self, project_id: str, src_dir: str) -> None:
        self._pid = project_id
        self._src_dir = Path(src_dir)

    async def init_repo(self) -> None:
        """Initialise a git repo in the project src directory."""
        self._src_dir.mkdir(parents=True, exist_ok=True)
        git_dir = self._src_dir / ".git"

        if not git_dir.exists():
            await self._run("init")
            await self._run("config", "user.email", "agent@codegen.ai")
            await self._run("config", "user.name", "Codegen Agent")

            gitignore = self._src_dir / ".gitignore"
            gitignore.write_text(
                "\n".join([
                    "*.pyc", "__pycache__/", "*.egg-info/", ".env",
                    "*.key", "*.pem", ".DS_Store", "node_modules/",
                    "dist/", "build/", ".venv/", "venv/", ".pytest_cache/",
                    "*.sqlite", "*.db",
                ]),
                encoding="utf-8",
            )
            await self._run("add", ".gitignore")
            await self._commit_raw("chore: initial commit — project scaffolded by agent")
            log.info("[%s] Git repo initialised", self._pid)

    async def commit(self, message: str) -> str | None:
        """Stage all changes and commit. Returns SHA or None if nothing to commit."""
        try:
            status = await self._run("status", "--porcelain", capture=True)
            if not status.strip():
                return None
            await self._run("add", "-A")
            sha = await self._commit_raw(message)
            if sha:
                await bus.publish("git_commit", {"sha": sha[:8], "message": message}, self._pid)
                log.info("[%s] Committed: %s — %s", self._pid, sha[:8], message[:60])
            return sha
        except Exception as exc:
            log.error("[%s] Git commit failed: %s", self._pid, exc)
            return None

    async def log(self, n: int = 10) -> list[dict]:
        """Return last N commits as dicts with sha, message, date."""
        try:
            out = await self._run(
                "log", f"-{n}", "--pretty=format:%H|%s|%ci", capture=True
            )
            commits = []
            for line in out.strip().splitlines():
                parts = line.split("|", 2)
                if len(parts) == 3:
                    commits.append({"sha": parts[0][:8], "message": parts[1], "date": parts[2]})
            return commits
        except Exception:
            return []

    async def _commit_raw(self, message: str) -> str | None:
        try:
            await self._run("commit", "-m", message, "--allow-empty")
            sha = await self._run("rev-parse", "HEAD", capture=True)
            return sha.strip() or None
        except Exception as exc:
            log.error("[%s] Raw commit failed: %s", self._pid, exc)
            return None

    async def _run(self, *args: str, capture: bool = False) -> str:
        if not self._src_dir.exists():
            return ""
        cmd = ["git"] + list(args)
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    cwd=str(self._src_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                ),
            )
            if result.returncode != 0 and not capture:
                stderr = result.stderr.strip()
                if "nothing to commit" not in stderr and stderr:
                    log.debug("[%s] git %s: %s", self._pid, args[0], stderr[:200])
            return result.stdout
        except subprocess.TimeoutExpired:
            log.warning("[%s] git %s timed out", self._pid, args[0])
            return ""
        except Exception as exc:
            log.error("[%s] git %s error: %s", self._pid, args[0], exc)
            return ""
