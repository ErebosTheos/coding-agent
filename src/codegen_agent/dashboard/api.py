"""Dashboard REST API endpoints."""
from __future__ import annotations

import logging
import time
import uuid

log = logging.getLogger(__name__)
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .event_bus import bus

if TYPE_CHECKING:
    from .project_registry import ProjectRegistry
    from .worker_pool import WorkerPool

router = APIRouter(prefix="/api")

# Injected by server.start_server()
_registry: "ProjectRegistry | None" = None
_pool: "WorkerPool | None" = None
_output_dir: str = "./output"
_inbox_dir: str = "./inbox"


def _set_context(registry, pool, output_dir: str, inbox_dir: str) -> None:
    global _registry, _pool, _output_dir, _inbox_dir
    _registry = registry
    _pool = pool
    _output_dir = output_dir
    _inbox_dir = inbox_dir


def _reg():
    if _registry is None:
        raise HTTPException(status_code=503, detail="Registry not initialised")
    return _registry


# ── Global stats ──────────────────────────────────────────────────────────────

@router.get("/stats/global")
async def global_stats():
    projects = await _reg().all_projects()
    total_calls = sum(p.stats().get("api_calls", 0) for p in projects)
    total_cost = sum(p.stats().get("cost_usd", 0.0) for p in projects)
    active_workers = _pool.total_active() if _pool else 0
    return {
        "calls": total_calls,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": round(total_cost, 4),
        "errors": 0,
        "active_workers": active_workers,
    }


# ── Projects list ─────────────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects():
    projects = await _reg().all_projects()
    result = []
    for p in sorted(projects, key=lambda x: -x.updated_at):
        result.append({
            "id": p.id,
            "name": p.brief.get("name", p.id),
            "state": p.state.value,
            "stats": p.stats(),
            "active_workers": _pool.active_count(p.id) if _pool else 0,
        })
    return result


# ── Project detail ────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    p = await _reg().get(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="not found")

    # Also try to refresh from checkpoint
    tasks: dict = {}
    tech_stack: dict = {}
    try:
        from ..checkpoint import CheckpointManager
        workspace = str(_reg().project_dir(project_id))
        report = CheckpointManager(workspace).load()
        if report:
            # Build tasks from architecture nodes
            tasks: dict = {}
            if report.architecture:
                generated = {f.node_id for f in (report.execution_result.generated_files
                                                  if report.execution_result else [])}
                failed = set(report.execution_result.failed_nodes
                             if report.execution_result else [])
                for node in report.architecture.nodes:
                    tasks[node.node_id] = {
                        "title": node.purpose,
                        "status": "done" if node.node_id in generated
                                  else "failed" if node.node_id in failed
                                  else "pending",
                        "complexity": "medium",
                        "files": [node.file_path],
                    }

            # Tech stack from plan
            tech_stack: dict = {}
            if report.plan:
                tech_stack = {"language": report.plan.tech_stack or ""}

            # Update brief if name not set
            if report.plan and not p.brief.get("name"):
                await p.set_brief({
                    "name": report.plan.project_name,
                    "description": report.prompt[:300],
                    "features": [f.title for f in (report.plan.features or [])],
                })
    except Exception as exc:
        log.warning("Failed to enrich project %s detail: %s", project_id, exc)

    # Git log
    git_log: list[dict] = []
    try:
        from .git_manager import GitManager
        src = str(_reg().src_dir(project_id))
        gm = GitManager(project_id, src)
        git_log = await gm.log(20)
    except Exception as exc:
        log.debug("Could not load git log for %s: %s", project_id, exc)

    return {
        "id": p.id,
        "state": p.state.value,
        "brief": p.brief,
        "build_plan": {
            "tech_stack": tech_stack,
            "total_tasks": len(tasks),
            "phases": 1,
        },
        "tasks": tasks,
        "stats": p.stats(),
        "git_log": git_log,
        "activity_log": p.activity_log()[-50:],
        "active_workers": _pool.active_count(project_id) if _pool else 0,
    }


# ── Submit ────────────────────────────────────────────────────────────────────

@router.post("/submit")
async def submit_project(
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
    parallel: bool = Form(False),
    priority: str = Form("normal"),
    mode: str = Form("all"),
):
    if priority not in ("high", "normal", "low"):
        raise HTTPException(status_code=400, detail="invalid priority")
    if mode not in ("all", "build", "fix", "docs"):
        raise HTTPException(status_code=400, detail="invalid mode")

    if file and file.filename:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt", ".md"}:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {suffix}. Use PDF, DOCX, TXT, or MD.",
            )
        raw = await file.read()
        # Write to inbox so the watcher picks it up
        inbox = Path(_inbox_dir)
        inbox.mkdir(parents=True, exist_ok=True)
        stem = Path(file.filename).stem
        suffix = Path(file.filename).suffix.lower()
        safe_name = f"{stem}-{int(time.time())}-{uuid.uuid4().hex[:6]}{suffix}"
        dest = inbox / safe_name
        dest.write_bytes(raw)
        brief_name = safe_name
    elif text and text.strip():
        inbox = Path(_inbox_dir)
        inbox.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        dest = inbox / f"brief-{ts}-{uuid.uuid4().hex[:6]}.txt"
        dest.write_text(text.strip(), encoding="utf-8")
        brief_name = dest.name
    else:
        raise HTTPException(status_code=400, detail="Provide either a file or text.")

    # Write sidecar
    sidecar = dest.with_suffix(".yaml")
    sidecar.write_text(
        f"parallel: {str(parallel).lower()}\npriority: {priority}\nmode: {mode}\n",
        encoding="utf-8",
    )

    await bus.publish("inbox_file", {"file": brief_name, "source": "ui"})
    return {"status": "queued", "file": brief_name}


# ── Retry ─────────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/retry")
async def retry_project(project_id: str):
    """Re-run BugFixer + DocGenerator on a FAILED or DONE project."""
    import asyncio
    p = await _reg().get(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="not found")

    from .server import _run_bugfix_docs
    src_dir = _reg().src_dir(project_id)
    if _pool:
        asyncio.create_task(
            _pool.run(project_id, lambda: _run_bugfix_docs(p, src_dir))
        )
    else:
        asyncio.create_task(_run_bugfix_docs(p, src_dir))
    return {"status": "retrying", "id": project_id}


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config():
    return {
        "client_mode": "live",
        "output_dir": str(Path(_output_dir).resolve()),
        "inbox_dir": str(Path(_inbox_dir).resolve()),
    }
