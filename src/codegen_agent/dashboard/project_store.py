"""In-memory project registry backed by checkpoint files on disk."""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ProjectInfo:
    id: str
    workspace: str
    state: str  # NEW | ARCHITECTING | BUILDING | FIXING | DONE | FAILED | PAUSED
    prompt: str
    name: str
    created_at: float
    updated_at: float
    stats: dict[str, Any] = field(default_factory=dict)
    activity_log: list[dict] = field(default_factory=list)
    # Populated from checkpoint once available
    tech_stack: dict[str, Any] = field(default_factory=dict)
    tasks: dict[str, Any] = field(default_factory=dict)
    features: list[str] = field(default_factory=list)
    git_log: list[dict] = field(default_factory=list)
    active_workers: int = 0


class ProjectStore:
    def __init__(self) -> None:
        self._projects: dict[str, ProjectInfo] = {}

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, workspace: str, prompt: str, name: str = "") -> ProjectInfo:
        pid = str(uuid.uuid4())[:8]
        now = time.time()
        info = ProjectInfo(
            id=pid,
            workspace=workspace,
            state="NEW",
            prompt=prompt,
            name=name or f"Project {pid}",
            created_at=now,
            updated_at=now,
        )
        self._projects[pid] = info
        return info

    def get(self, pid: str) -> Optional[ProjectInfo]:
        return self._projects.get(pid)

    def all(self) -> list[ProjectInfo]:
        return list(self._projects.values())

    def update_state(self, pid: str, state: str) -> None:
        p = self._projects.get(pid)
        if p:
            p.state = state
            p.updated_at = time.time()

    def set_active_workers(self, pid: str, count: int) -> None:
        p = self._projects.get(pid)
        if p:
            p.active_workers = count

    def push_activity(self, pid: str, msg: str) -> None:
        p = self._projects.get(pid)
        if p:
            p.activity_log.append({"t": time.time(), "msg": msg})
            p.updated_at = time.time()

    def update_from_checkpoint(self, pid: str, report: Any) -> None:
        """Sync ProjectInfo fields from a loaded PipelineReport."""
        p = self._projects.get(pid)
        if not p or not report:
            return

        # Infer state
        if report.qa_report is not None:
            p.state = "DONE" if report.qa_report.approved else "FAILED"
        elif report.healing_report is not None:
            p.state = "FIXING"
        elif report.execution_result is not None:
            p.state = "BUILDING"
        elif report.architecture is not None:
            p.state = "BUILDING"
        elif report.plan is not None:
            p.state = "ARCHITECTING"

        # Stats
        files_created = len(report.execution_result.generated_files) if report.execution_result else 0
        files_fixed = len(report.healing_report.attempts) if report.healing_report else 0
        qa_score = report.qa_report.score if report.qa_report else 0.0
        p.stats = {
            "files_created": files_created,
            "files_fixed": files_fixed,
            "git_commits": 0,
            "api_calls": len(report.stage_traces) if report.stage_traces else 0,
            "cost_usd": 0.0,
            "qa_score": qa_score,
            "wall_clock": round(report.wall_clock_seconds, 1),
            "updated_at": p.updated_at,
        }

        # Tech stack
        if report.plan:
            p.name = report.plan.project_name or p.name
            p.features = [f.title for f in (report.plan.features or [])]
            p.tech_stack = {"language": report.plan.tech_stack or ""}

        # Tasks from architecture nodes
        if report.architecture:
            p.tasks = {
                n.node_id: {
                    "title": n.purpose,
                    "status": _node_status(n.node_id, report),
                    "complexity": "medium",
                    "files": [n.file_path],
                }
                for n in (report.architecture.nodes or [])
            }

        p.updated_at = time.time()

    def scan_existing(self, output_dir: str) -> None:
        """Load any pre-existing workspaces from disk on startup."""
        base = Path(output_dir)
        if not base.exists():
            return
        for ckpt in base.rglob(".codegen_agent/checkpoint.json"):
            workspace = str(ckpt.parent.parent)
            pid = ckpt.parent.parent.name
            if pid in self._projects:
                continue
            try:
                from ..checkpoint import CheckpointManager
                report = CheckpointManager(workspace).load()
                prompt = report.prompt if report else ""
                name = report.plan.project_name if (report and report.plan) else pid
                now = os.path.getmtime(str(ckpt))
                info = ProjectInfo(
                    id=pid,
                    workspace=workspace,
                    state="DONE",
                    prompt=prompt,
                    name=name,
                    created_at=now,
                    updated_at=now,
                )
                self._projects[pid] = info
                if report:
                    self.update_from_checkpoint(pid, report)
            except Exception:
                pass


def _node_status(node_id: str, report: Any) -> str:
    if report.execution_result:
        if node_id in (report.execution_result.failed_nodes or []):
            return "failed"
        if node_id in (report.execution_result.skipped_nodes or []):
            return "pending"
        generated = {f.node_id for f in report.execution_result.generated_files}
        if node_id in generated:
            return "done"
    return "pending"


store = ProjectStore()
