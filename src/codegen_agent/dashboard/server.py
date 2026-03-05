"""FastAPI dashboard server with WebSocket real-time updates."""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router, _set_context
from .event_bus import bus
from .project_registry import ProjectRegistry, ProjectState
from .worker_pool import WorkerPool

_STATIC_DIR = Path(__file__).parent / "static"


class _LiveTee(io.TextIOBase):
    """Wraps sys.stdout: writes to original stdout AND publishes each line
    as a 'terminal_line' event so the dashboard live feed shows all output."""

    def __init__(self, original, project_id: str, loop: asyncio.AbstractEventLoop):
        self._orig = original
        self._pid = project_id
        self._loop = loop
        self._buf = ""

    def write(self, text: str) -> int:
        self._orig.write(text)
        self._orig.flush()
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                asyncio.run_coroutine_threadsafe(
                    bus.publish("terminal_line", {"line": line}, self._pid),
                    self._loop,
                )
        return len(text)

    def flush(self):
        self._orig.flush()

app = FastAPI(title="Codegen Agent Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(str(_STATIC_DIR / "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    queue = await bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                await ws.send_text(json.dumps(event, default=str))
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "heartbeat", "t": time.time()}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await bus.unsubscribe(queue)


# ── Inbox watcher ─────────────────────────────────────────────────────────────

async def _watch_inbox(
    inbox_dir: str,
    registry: ProjectRegistry,
    worker_pool: WorkerPool,
    config_path: str | None,
) -> None:
    import re
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()

    while True:
        for brief_file in sorted(inbox.glob("brief-*.txt")):
            if brief_file.name in seen:
                continue
            seen.add(brief_file.name)

            raw_text = brief_file.read_text(encoding="utf-8").strip()
            if not raw_text:
                continue

            # Read optional sidecar
            sidecar: dict = {}
            sidecar_path = brief_file.with_suffix(".yaml")
            if sidecar_path.exists():
                try:
                    import yaml
                    sidecar = yaml.safe_load(sidecar_path.read_text()) or {}
                except Exception:
                    pass

            # Parse brief with LLM if file is rich enough; fallback to raw text
            brief_dict: dict = {}
            try:
                from .brief_reader import parse_text
                project_brief = await parse_text(raw_text, sidecar)
                brief_dict = project_brief.to_dict()
                project_name = project_brief.name
            except Exception:
                import re
                project_name = f"project-{int(time.time())}"
                brief_dict = {"name": project_name, "description": raw_text[:300]}

            pid = f"{project_name}-{int(time.time())}"[:40]
            pid = re.sub(r"[^a-z0-9-]", "-", pid.lower())

            proj = await registry.create(
                pid,
                brief_dict,
                parallel=sidecar.get("parallel", False),
                priority=sidecar.get("priority", "normal"),
                mode=sidecar.get("mode", "all"),
            )

            await bus.publish("project_enqueued", {"name": proj.brief.get("name", pid), "id": pid}, pid)

            asyncio.create_task(
                _run_pipeline(proj, registry.src_dir(pid), raw_text, config_path, worker_pool)
            )

            # Clean up
            try:
                brief_file.unlink()
                sidecar_path.unlink(missing_ok=True)
            except Exception:
                pass

        await asyncio.sleep(2)


async def _run_bugfix_docs(proj, src_dir: Path) -> None:
    """Retry: Docs → BugFixer → re-QA → DONE."""
    from .git_manager import GitManager
    from .doc_generator import DocGenerator
    from .bug_fixer import BugFixer

    pid = proj.id

    try:
        git = GitManager(pid, str(src_dir))
        await git.init_repo()

        # Docs first
        await proj.set_state(ProjectState.DOCUMENTING)
        await bus.publish("state_change", {"state": "DOCUMENTING"}, pid)
        await proj.log_activity("Retry: generating docs")
        doc_gen = DocGenerator(proj, str(src_dir))
        await doc_gen.run()
        sha = await git.commit("docs: auto-generated (retry)")
        if sha:
            await proj.record_commit(sha, "docs: auto-generated (retry)")

        # BugFixer
        await proj.set_state(ProjectState.FIXING)
        await bus.publish("state_change", {"state": "FIXING"}, pid)
        await proj.log_activity("Retry: BugFixer starting")
        bug_fixer = BugFixer(proj, str(src_dir), num_passes=2)
        await bug_fixer.run()
        await git.commit("fix: retry bug fix passes")

        await proj.set_state(ProjectState.DONE)
        await proj.log_activity("Retry complete")
        await bus.publish("build_complete", {"retry": True}, pid)
    except Exception as exc:
        await proj.set_state(ProjectState.FAILED)
        await proj.log_error(str(exc))
        await proj.log_activity(f"Retry failed: {exc}")
        await bus.publish("project_failed", {"error": str(exc)}, pid)


async def _run_pipeline(
    proj,
    src_dir: Path,
    prompt: str,
    config_path: str | None,
    worker_pool: WorkerPool,
) -> None:
    """Run the full codegen pipeline for a project."""
    from ..orchestrator import Orchestrator
    from ..checkpoint import CheckpointManager
    from .git_manager import GitManager
    from .doc_generator import DocGenerator
    from .bug_fixer import BugFixer  # noqa: F401 – used in _do_heal closure

    pid = proj.id
    workspace = str(src_dir.parent)

    await proj.set_state(ProjectState.BUILDING)
    await proj.log_activity("Pipeline started")
    await bus.publish("project_started", {}, pid)

    # Redirect stdout so every print() line appears in the live feed
    _loop = asyncio.get_event_loop()
    _orig_stdout = sys.stdout
    sys.stdout = _LiveTee(_orig_stdout, pid, _loop)

    # Polling task: sync checkpoint → registry every 3s
    async def _poll():
        prev_state = None
        while True:
            await asyncio.sleep(3)
            try:
                report = CheckpointManager(workspace).load()
                if not report:
                    continue
                # Infer state from checkpoint
                if report.healing_report is not None:
                    new_state = ProjectState.FIXING
                elif report.execution_result is not None:
                    new_state = ProjectState.BUILDING
                elif report.architecture is not None:
                    new_state = ProjectState.BUILDING
                elif report.plan is not None:
                    new_state = ProjectState.ARCHITECTING
                else:
                    new_state = ProjectState.BUILDING

                if new_state != prev_state:
                    await proj.set_state(new_state)
                    await bus.publish("state_change", {"state": new_state.value}, pid)
                    prev_state = new_state

                # Sync stats from checkpoint
                if report.execution_result:
                    files = len(report.execution_result.generated_files)
                    await proj.inc_stat("files_created",
                        max(0, files - proj.stats().get("files_created", 0)))
                if report.plan and not proj.brief.get("name"):
                    await proj.set_brief({
                        "name": report.plan.project_name,
                        "description": prompt[:300],
                        "features": [f.title for f in (report.plan.features or [])],
                    })
            except Exception:
                pass

    poll_task = asyncio.create_task(_poll())

    try:
        orchestrator = Orchestrator(workspace, config_path)
        # max_heals=0: build only — BugFixer runs after as background healing
        report = await orchestrator.run(prompt, resume=False, max_heals=0)
        poll_task.cancel()

        # Sync stats
        traces = report.stage_traces or []
        api_calls = len(traces)
        total_chars = sum(t.prompt_chars + t.response_chars for t in traces)
        cost_usd = round((total_chars / 4) / 1_000_000 * 5.0, 4)

        if report.execution_result:
            files = len(report.execution_result.generated_files)
            await proj.inc_stat("files_created",
                max(0, files - proj.stats().get("files_created", 0)))

        async with proj._lock:
            proj._data["stats"]["api_calls"] = api_calls
            proj._data["stats"]["cost_usd"] = cost_usd
            proj._data["stats"]["wall_clock"] = round(report.wall_clock_seconds, 1)
            proj._save()

        # ── If code was generated: announce BUILD APPROVED, then run Docs + Heal + QA in parallel ─
        if report.execution_result:
            git = GitManager(pid, str(src_dir))
            await git.init_repo()
            await git.commit("feat: initial build complete")

            await proj.set_state(ProjectState.BUILD_APPROVED)
            await bus.publish("build_approved", {
                "files": len(report.execution_result.generated_files),
                "api_calls": api_calls,
                "cost_usd": cost_usd,
            }, pid)
            await proj.log_activity("Build approved — Docs + Heal + QA running in parallel")

            qa_score = report.qa_report.score if report.qa_report else 0.0
            qa_approved = report.qa_report.approved if report.qa_report else False

            async def _do_docs():
                try:
                    await proj.log_activity("Docs: generating documentation")
                    doc_gen = DocGenerator(proj, str(src_dir))
                    await doc_gen.run()
                    sha = await git.commit("docs: auto-generated")
                    if sha:
                        await proj.record_commit(sha, "docs: auto-generated")
                    await bus.publish("docs_done", {}, pid)
                    await proj.log_activity("Docs: complete")
                except Exception as exc:
                    await proj.log_activity(f"Docs failed: {exc}")

            async def _do_heal():
                try:
                    await bus.publish("heal_started", {}, pid)
                    await proj.log_activity("Healer: background healing starting")
                    bug_fixer = BugFixer(proj, str(src_dir), num_passes=2)
                    await bug_fixer.run()
                    sha = await git.commit("fix: automated bug fix passes")
                    if sha:
                        await proj.record_commit(sha, "fix: automated bug fix passes")
                    await bus.publish("heal_complete", {"success": True}, pid)
                    await proj.log_activity("Healer: complete")
                except Exception as exc:
                    await proj.log_activity(f"Heal failed: {exc}")
                    await bus.publish("heal_complete", {"success": False, "error": str(exc)}, pid)

            async def _do_qa():
                nonlocal qa_score, qa_approved
                try:
                    await proj.set_state(ProjectState.QA_RUNNING)
                    await bus.publish("state_change", {"state": "QA_RUNNING"}, pid)
                    await proj.log_activity("QA: streaming audit starting")
                    from ..qa_auditor import QAAuditor

                    async def _on_file(file_path: str, result: dict) -> None:
                        await bus.publish("qa_file_reviewed", {
                            "file": file_path,
                            "issues": result.get("issues", []),
                            "clean": result.get("clean", True),
                            "lines": result.get("lines", 0),
                        }, pid)

                    auditor = QAAuditor(
                        orchestrator.router.get_client_for_role("qa_auditor"),
                        workspace,
                    )
                    new_qa = await auditor.audit_streaming(report, on_file_reviewed=_on_file)
                    qa_score = new_qa.score
                    qa_approved = new_qa.approved
                    await proj.log_activity(f"QA: score={qa_score:.0f} approved={qa_approved}")
                    await bus.publish("qa_result", {
                        "score": qa_score,
                        "approved": qa_approved,
                        "issues": new_qa.issues[:10],
                        "suggestions": new_qa.suggestions[:5],
                    }, pid)
                except Exception as exc:
                    await proj.log_activity(f"QA audit failed: {exc}")
                    await bus.publish("qa_result", {
                        "score": qa_score,
                        "approved": qa_approved,
                        "issues": [],
                        "suggestions": [],
                        "error": str(exc),
                    }, pid)

            await asyncio.gather(_do_docs(), _do_heal(), _do_qa())

            await proj.set_state(ProjectState.DONE)
            await proj.log_activity(f"Pipeline done — calls={api_calls} cost=${cost_usd} qa={qa_score:.0f}")
            await bus.publish("build_complete", {
                "qa_score": qa_score,
                "api_calls": api_calls,
                "cost_usd": cost_usd,
            }, pid)
        else:
            # No code generated at all
            await proj.set_state(ProjectState.FAILED)
            await proj.log_activity("Pipeline failed — no code generated")
            await bus.publish("project_failed", {
                "error": "No code generated",
                "api_calls": api_calls,
                "cost_usd": cost_usd,
            }, pid)

    except Exception as exc:
        poll_task.cancel()
        await proj.set_state(ProjectState.FAILED)
        await proj.log_error(str(exc))
        await proj.log_activity(f"Pipeline error: {exc}")
        await bus.publish("project_failed", {"error": str(exc)}, pid)
    finally:
        sys.stdout = _orig_stdout


# ── Entry point ───────────────────────────────────────────────────────────────

async def start_server(
    host: str = "127.0.0.1",
    port: int = 7070,
    output_dir: str = "./output",
    inbox_dir: str = "./inbox",
    config_path: str | None = None,
) -> None:
    registry = ProjectRegistry(output_dir)
    await registry.initialise()

    worker_pool = WorkerPool()

    _set_context(registry, worker_pool, output_dir, inbox_dir)

    asyncio.create_task(_watch_inbox(inbox_dir, registry, worker_pool, config_path))

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="none")
    server = uvicorn.Server(config)
    print(f"Dashboard → http://{host}:{port}")
    await server.serve()
