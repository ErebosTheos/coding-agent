"""Persistent multi-project registry backed by agent-state.json files.

Each project lives in <output_dir>/<project_id>/agent-state.json.
Survives daemon restarts — in-progress projects are resumed automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class ProjectState(str, Enum):
    NEW            = "NEW"
    PARSING        = "PARSING"
    ARCHITECTING   = "ARCHITECTING"
    BUILDING       = "BUILDING"
    BUILD_APPROVED = "BUILD_APPROVED"
    FIXING         = "FIXING"
    QA_RUNNING     = "QA_RUNNING"
    DOCUMENTING    = "DOCUMENTING"
    WATCHING       = "WATCHING"
    DONE           = "DONE"
    FAILED         = "FAILED"
    PAUSED         = "PAUSED"


class Project:
    def __init__(self, project_id: str, state_file: Path) -> None:
        self.id = project_id
        self.state_file = state_file
        self._data: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _blank(self) -> dict:
        return {
            "id": self.id,
            "state": ProjectState.NEW.value,
            "created_at": time.time(),
            "updated_at": time.time(),
            "brief": {},
            "build_plan": {},
            "tasks": {},
            "git_commits": [],
            "errors": [],
            "stats": {
                "files_created": 0,
                "files_fixed": 0,
                "bugs_found": 0,
                "api_calls": 0,
                "git_commits": 0,
                "cost_usd": 0.0,
                "wall_clock": 0.0,
            },
            "parallel": False,
            "priority": "normal",
            "mode": "all",
            "activity_log": [],
        }

    def _load(self) -> None:
        if self.state_file.exists():
            try:
                with open(self.state_file) as fh:
                    self._data = json.load(fh)
            except Exception:
                self._data = self._blank()
        else:
            self._data = self._blank()

    def _save(self) -> None:
        self._data["updated_at"] = time.time()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(self._data, fh, indent=2, default=str)
        tmp.replace(self.state_file)

    async def load(self) -> "Project":
        async with self._lock:
            self._load()
        return self

    async def save(self) -> None:
        async with self._lock:
            self._save()

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def state(self) -> ProjectState:
        return ProjectState(self._data.get("state", ProjectState.NEW.value))

    async def set_state(self, state: ProjectState) -> None:
        async with self._lock:
            self._data["state"] = state.value
            self._save()
        log.info("[%s] State → %s", self.id, state.value)

    # ── Brief / plan ──────────────────────────────────────────────────────────

    @property
    def brief(self) -> dict:
        return self._data.get("brief", {})

    async def set_brief(self, brief_dict: dict) -> None:
        async with self._lock:
            self._data["brief"] = brief_dict
            self._save()

    @property
    def build_plan(self) -> dict:
        return self._data.get("build_plan", {})

    async def set_build_plan(self, plan: dict) -> None:
        async with self._lock:
            self._data["build_plan"] = plan
            self._save()

    # ── Task tracking ─────────────────────────────────────────────────────────

    async def set_task_status(self, task_id: str, status: str, detail: str = "") -> None:
        async with self._lock:
            self._data.setdefault("tasks", {})[task_id] = {
                "status": status,
                "detail": detail,
                "updated_at": time.time(),
            }
            self._save()

    def get_task_status(self, task_id: str) -> str:
        return self._data.get("tasks", {}).get(task_id, {}).get("status", "pending")

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def inc_stat(self, key: str, by: int = 1) -> None:
        async with self._lock:
            self._data.setdefault("stats", {})[key] = (
                self._data["stats"].get(key, 0) + by
            )
            self._save()

    def stats(self) -> dict:
        return self._data.get("stats", {})

    async def update_stats(self, **kwargs) -> None:
        """Merge keyword-argument key/value pairs into the stats dict and persist."""
        async with self._lock:
            s = self._data.setdefault("stats", {})
            s.update(kwargs)
            self._save()

    # ── Activity log ──────────────────────────────────────────────────────────

    async def log_activity(self, msg: str) -> None:
        async with self._lock:
            self._data.setdefault("activity_log", []).append({"t": time.time(), "msg": msg})
            self._data["activity_log"] = self._data["activity_log"][-500:]
            self._save()

    def activity_log(self) -> list[dict]:
        return self._data.get("activity_log", [])

    # ── Git ───────────────────────────────────────────────────────────────────

    async def record_commit(self, sha: str, message: str) -> None:
        async with self._lock:
            self._data.setdefault("git_commits", []).append(
                {"sha": sha, "message": message, "t": time.time()}
            )
            self._data.setdefault("stats", {})["git_commits"] = (
                self._data["stats"].get("git_commits", 0) + 1
            )
            self._save()

    # ── Error log ─────────────────────────────────────────────────────────────

    async def log_error(self, error: str) -> None:
        async with self._lock:
            self._data.setdefault("errors", []).append({"t": time.time(), "error": error})
            self._save()

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.brief.get("name") or self.id

    @property
    def updated_at(self) -> float:
        return self._data.get("updated_at", 0.0)

    @property
    def parallel(self) -> bool:
        return bool(self._data.get("parallel", False))

    @property
    def priority(self) -> str:
        return self._data.get("priority", "normal")

    @property
    def mode(self) -> str:
        return self._data.get("mode", "all")

    def workspace(self) -> str:
        return str(self.state_file.parent)

    def to_dict(self) -> dict:
        return dict(self._data)


class ProjectRegistry:
    """Manages all projects on disk. Thread-safe, async."""

    def __init__(self, projects_dir: str) -> None:
        self._root = Path(projects_dir)
        self._projects: dict[str, Project] = {}
        self._lock = asyncio.Lock()

    async def initialise(self) -> None:
        """Load all existing projects from disk on startup."""
        self._root.mkdir(parents=True, exist_ok=True)
        for state_file in self._root.glob("*/agent-state.json"):
            pid = state_file.parent.name
            proj = Project(pid, state_file)
            await proj.load()
            self._projects[pid] = proj
            log.info("Resumed project: %s (%s)", pid, proj.state.value)

    async def create(
        self,
        project_id: str,
        brief_dict: dict,
        parallel: bool = False,
        priority: str = "normal",
        mode: str = "all",
    ) -> Project:
        project_dir = self._root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        state_file = project_dir / "agent-state.json"

        proj = Project(project_id, state_file)
        await proj.load()
        proj._data.update({
            "id": project_id,
            "brief": brief_dict,
            "parallel": parallel,
            "priority": priority,
            "mode": mode,
        })
        await proj.save()

        async with self._lock:
            self._projects[project_id] = proj
        log.info("Created project: %s", project_id)
        return proj

    async def get(self, project_id: str) -> Optional[Project]:
        async with self._lock:
            return self._projects.get(project_id)

    async def all_projects(self) -> list[Project]:
        async with self._lock:
            return list(self._projects.values())

    async def active_projects(self) -> list[Project]:
        all_p = await self.all_projects()
        return [
            p for p in all_p
            if p.state not in (ProjectState.DONE, ProjectState.FAILED, ProjectState.PAUSED)
        ]

    def project_dir(self, project_id: str) -> Path:
        return self._root / project_id

    def src_dir(self, project_id: str) -> Path:
        return self._root / project_id / "src"
