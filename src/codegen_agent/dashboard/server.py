"""FastAPI dashboard server with WebSocket real-time updates."""
from __future__ import annotations

import asyncio
import contextvars
import io
import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router, _set_context
from .event_bus import bus
from .project_registry import ProjectRegistry, ProjectState
from .worker_pool import WorkerPool

_STATIC_DIR = Path(__file__).parent / "static"


# ── Per-coroutine stdout routing via ContextVar ───────────────────────────────
# Each pipeline run sets _current_pid in its own asyncio task context.
# The singleton _RoutingTee installed once at import time routes output
# to the correct project's event bus — no global sys.stdout mutation needed.
_current_pid: contextvars.ContextVar[str] = contextvars.ContextVar("current_pid", default="")


class _RoutingTee(io.TextIOBase):
    """Installed once as sys.stdout at server startup.

    Writes to the original stdout AND publishes each line to the event bus
    of whichever project is active in the current asyncio task context.
    Concurrent project runs each have their own ContextVar value, so output
    never bleeds between projects.
    """

    def __init__(self, original: io.TextIOBase) -> None:
        self._orig = original
        self._bufs: dict[str, str] = {}  # pid → partial line buffer

    def write(self, text: str) -> int:
        self._orig.write(text)
        self._orig.flush()
        pid = _current_pid.get("")
        if not pid:
            return len(text)
        buf = self._bufs.get(pid, "") + text
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip()
            if line:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bus.publish("terminal_line", {"line": line}, pid), loop
                        )
                except Exception:
                    pass
        self._bufs[pid] = buf
        return len(text)

    def flush(self) -> None:
        self._orig.flush()

    def clear_pid(self, pid: str) -> None:
        """Remove buffer for a finished project to free memory."""
        self._bufs.pop(pid, None)


# Install once — never replaced again
_real_stdout = sys.stdout
_tee = _RoutingTee(_real_stdout)
sys.stdout = _tee  # type: ignore[assignment]

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

            import uuid as _uuid
            pid = f"{project_name}-{_uuid.uuid4().hex[:8]}"[:40]
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
                worker_pool.run(
                    pid,
                    lambda: _run_pipeline(proj, registry.src_dir(pid), raw_text, config_path, worker_pool),
                )
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
        bug_fixer = BugFixer(proj, str(src_dir), num_passes=4)
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


async def _qa_driven_fix(
    issues: list[str],
    src_dir: Path,
    llm_client,
    proj,
    pid: str,
) -> int:
    """For each QA issue that references a specific file, run a targeted LLM fix.
    Returns number of files successfully fixed."""
    import re as _re
    fixed = 0
    # Extract file paths from issue strings — matches any of:
    #   "src/main.py: ..."  "src/main.py imports ..."  "src/main.py has ..."
    _FILE_RE = _re.compile(r'\b((?:[\w./\\-]+/)?[\w-]+\.(?:py|js|ts|tsx|go))\b')
    file_issue_map: dict[str, list[str]] = {}
    for issue in issues:
        for m in _FILE_RE.finditer(issue):
            fpath = m.group(1).replace("\\", "/")
            file_issue_map.setdefault(fpath, []).append(issue)

    workspace = str(src_dir.parent)
    from ..utils import resolve_workspace_path as _rwp
    from pathlib import Path as _Path

    def _locate(rel: str):
        """Resolve rel to an existing Path inside workspace, trying multiple strategies."""
        # 1. As-is (e.g. "app/routers/cms.py")
        p = _rwp(workspace, rel)
        if p and p.exists():
            return p
        # 2. Under src/ (e.g. "src/app/routers/cms.py")
        p = _rwp(workspace, f"src/{rel}")
        if p and p.exists():
            return p
        # 3. Bare filename only — glob search the workspace tree
        #    Handles QA issues that mention just "cms.py" or "users.py"
        name = _Path(rel).name
        if name == rel or "/" not in rel:
            ws_root = _Path(workspace).resolve()
            matches = [
                m for m in ws_root.rglob(name)
                if m.is_file()
                # Ignore test files and __pycache__
                and "__pycache__" not in m.parts
                and not m.name.startswith("test_")
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                # Prefer deepest match that isn't a test dir
                non_test = [m for m in matches if "test" not in str(m).lower()]
                return (non_test or matches)[0]
        return None

    for rel_path, file_issues in file_issue_map.items():
        full_path = _locate(rel_path)
        if full_path is None:
            log.warning("_qa_driven_fix: skipping path outside workspace or not found: %s", rel_path)
            continue

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            issues_block = "\n".join(f"- {i}" for i in file_issues)
            prompt = (
                f"Fix the following QA issues in this file. "
                f"Return ONLY the complete corrected file. No markdown fences.\n\n"
                f"FILE: {full_path.name}\n"
                f"QA ISSUES:\n{issues_block}\n\n"
                f"CURRENT CONTENT:\n```\n{content[:5000]}\n```"
            )
            resp = await llm_client.generate(prompt)
            import re as _re2
            cleaned = _re2.sub(r"```\w*\n?", "", resp).strip().rstrip("`").strip()
            if cleaned and len(cleaned) > 20:
                # Syntax check for Python
                if full_path.suffix == ".py":
                    import ast as _ast
                    try:
                        _ast.parse(cleaned)
                    except SyntaxError:
                        continue
                full_path.write_text(cleaned, encoding="utf-8")
                fixed += 1
                await proj.log_activity(f"QA fix: {rel_path}")
                await bus.publish("bug_fixed", {"file": full_path.name, "qa_driven": True}, pid)
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning("QA-driven fix failed for %s: %s", rel_path, exc)

    return fixed


async def _maybe_rearchitect(src_dir, orchestrator, report, proj, pid, git) -> None:
    """If >50% of tests still fail after healing, regenerate the broken source files
    from scratch using the architect-tier LLM with full project context."""
    from ..pytest_parser import run_pytest_structured
    try:
        pytest_report = await asyncio.wait_for(
            run_pytest_structured("pytest tests/", str(src_dir.parent)), timeout=60
        )
    except Exception:
        return
    if pytest_report is None:
        return

    total = pytest_report.passed + pytest_report.failed + pytest_report.errors
    if total == 0 or pytest_report.failed / total <= 0.5:
        return  # less than 50% failing — normal healing is enough

    broken = list(pytest_report.broken_source_files.keys())[:5]  # cap at 5 files
    if not broken:
        return

    await proj.log_activity(
        f"Re-architect: {pytest_report.failed}/{total} tests failing — regenerating {len(broken)} file(s)"
    )
    await bus.publish("heal_started", {"rearchitect": True, "files": broken}, pid)

    llm = orchestrator.router.get_client_for_role("architect")
    from ..utils import resolve_workspace_path as _rwp
    workspace = str(src_dir.parent)
    fixed_any = False
    for rel_path in broken:
        full_path = Path(src_dir.parent) / rel_path
        if not full_path.exists():
            full_path = src_dir / rel_path
        if not full_path.exists():
            continue
        # Enforce workspace boundary via shared helper
        if _rwp(workspace, rel_path) is None:
            log.warning("_maybe_rearchitect: skipping path outside workspace: %s", rel_path)
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            failures = "\n".join(
                f"- {tf.short_repr}" for tf in pytest_report.failures
                if rel_path in tf.source_files
            )
            # Collect sibling context
            siblings = []
            for sf in sorted(Path(src_dir).rglob("*.py")):
                if sf == full_path:
                    continue
                try:
                    lines = [l for l in sf.read_text(encoding="utf-8").splitlines()
                             if l.strip().startswith(("def ", "async def ", "class ", "from ", "import "))]
                    if lines:
                        siblings.append(f"# {sf.name}\n" + "\n".join(lines[:15]))
                except OSError:
                    pass
            context = "\n\n".join(siblings[:6])

            prompt = (
                f"This file is causing test failures. Rewrite it completely from scratch.\n"
                f"Preserve its purpose. Return ONLY the file content, no markdown.\n\n"
                f"FILE: {full_path.name}\n"
                f"TEST FAILURES:\n{failures}\n\n"
                f"PROJECT CONTEXT:\n{context}\n\n"
                f"CURRENT BROKEN CONTENT:\n```\n{content[:4000]}\n```"
            )
            resp = await llm.generate(prompt)
            import re as _re
            cleaned = _re.sub(r"```\w*\n?", "", resp).strip().rstrip("`").strip()
            if cleaned and len(cleaned) > 20:
                import ast as _ast
                if full_path.suffix == ".py":
                    try:
                        _ast.parse(cleaned)
                    except SyntaxError:
                        continue
                full_path.write_text(cleaned, encoding="utf-8")
                await proj.log_activity(f"Re-architected: {rel_path}")
                fixed_any = True
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning("Re-architect failed for %s: %s", rel_path, exc)

    if fixed_any:
        sha = await git.commit("fix: re-architected high-failure files")
        if sha:
            await proj.record_commit(sha, "fix: re-architected high-failure files")


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

    # Route this project's print() output to its live feed via ContextVar
    _pid_token = _current_pid.set(pid)

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
        # max_heals=0: build only — BugFixer + QA run after as background tasks
        # Skip orchestrator's internal QA — dashboard runs its own richer QA
        # Use a context-local flag via the orchestrator constructor rather than a global env var
        orchestrator.skip_qa = True
        report = await orchestrator.run(prompt, resume=False, max_heals=0)
        poll_task.cancel()

        # Sync stats — use real counters from the router (counts every live LLM call)
        api_calls = orchestrator.router.total_llm_calls
        total_chars = orchestrator.router.total_prompt_chars + orchestrator.router.total_response_chars
        cost_usd = round((total_chars / 4) / 1_000_000 * 5.0, 4)

        if report.execution_result:
            files = len(report.execution_result.generated_files)
            await proj.inc_stat("files_created",
                max(0, files - proj.stats().get("files_created", 0)))

        await proj.update_stats(
            api_calls=api_calls,
            cost_usd=cost_usd,
            wall_clock=round(report.wall_clock_seconds, 1),
        )

        # ── If code was generated: announce BUILD APPROVED, then run Docs + Heal + QA in parallel ─
        if report.execution_result:
            git = GitManager(pid, str(src_dir))
            try:
                await git.init_repo()
                sha = await git.commit("feat: initial build complete")
                if sha:
                    await proj.record_commit(sha, "feat: initial build complete")
                else:
                    log.warning("[%s] Initial git commit returned no SHA — git_commits will stay 0", pid)
            except Exception as exc:
                log.warning("[%s] Git init/commit failed: %s", pid, exc)

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

                    # Re-architect on high failure rate: if >50% tests still fail,
                    # regenerate the worst files with full architect context
                    await _maybe_rearchitect(
                        src_dir, orchestrator, report, proj, pid, git
                    )

                    await bus.publish("heal_complete", {"success": True}, pid)
                    await proj.log_activity("Healer: complete")
                except Exception as exc:
                    await proj.log_activity(f"Heal failed: {exc}")
                    await bus.publish("heal_complete", {"success": False, "error": str(exc)}, pid)

            async def _do_qa(reaudit: bool = False):
                nonlocal qa_score, qa_approved
                label = "QA re-audit" if reaudit else "QA"
                try:
                    await proj.set_state(ProjectState.QA_RUNNING)
                    await bus.publish("state_change", {"state": "QA_RUNNING"}, pid)
                    await proj.log_activity(f"{label}: streaming audit starting")
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
                    await proj.log_activity(f"{label}: score={qa_score:.0f} approved={qa_approved}")
                    await bus.publish("qa_result", {
                        "score": qa_score,
                        "approved": qa_approved,
                        "issues": new_qa.issues[:10],
                        "suggestions": new_qa.suggestions[:5],
                    }, pid)

                    # QA-driven healing: any time QA finds real issues (initial or reaudit)
                    if not qa_approved and new_qa.issues:
                        await proj.log_activity(f"QA: score {qa_score:.0f}, not approved — running targeted fix pass")
                        await bus.publish("heal_started", {"qa_driven": True}, pid)
                        fixed_count = await _qa_driven_fix(
                            new_qa.issues, src_dir,
                            orchestrator.router.get_client_for_role("executor"),
                            proj, pid,
                        )
                        if fixed_count:
                            sha = await git.commit("fix: QA-driven targeted fixes")
                            if sha:
                                await proj.record_commit(sha, "fix: QA-driven targeted fixes")
                        await bus.publish("heal_complete", {"success": True, "qa_driven": True}, pid)
                except Exception as exc:
                    await proj.log_activity(f"{label} failed: {exc}")
                    await bus.publish("qa_result", {
                        "score": qa_score,
                        "approved": qa_approved,
                        "issues": [],
                        "suggestions": [],
                        "error": str(exc),
                    }, pid)

            async def _do_e2e():
                try:
                    from .e2e_validator import run_e2e_smoke
                    await proj.log_activity("E2E: starting container smoke test")

                    async def _on_e2e(ev_type: str, data: dict) -> None:
                        await bus.publish(ev_type, data, pid)
                        if ev_type == "e2e_building":
                            await proj.log_activity("E2E: building container...")
                        elif ev_type == "e2e_started":
                            await proj.log_activity(
                                f"E2E: container up on port {data.get('host_port')} — probing endpoints"
                            )

                    e2e = await asyncio.wait_for(
                        run_e2e_smoke(src_dir, proj.name or pid, on_event=_on_e2e),
                        timeout=240,
                    )

                    if e2e.skipped:
                        await proj.log_activity("E2E: Docker unavailable — skipped")
                        await bus.publish("e2e_result", {"skipped": True}, pid)
                    elif e2e.success:
                        hit = len(e2e.endpoints_hit)
                        await proj.log_activity(
                            f"E2E: ✓ passed — {hit} endpoint(s) responding "
                            f"(build={e2e.build_seconds:.0f}s startup={e2e.startup_seconds:.1f}s)"
                        )
                        await bus.publish("e2e_result", {
                            "success": True,
                            "endpoints_hit": e2e.endpoints_hit,
                            "endpoints_failed": e2e.endpoints_failed,
                            "build_seconds": e2e.build_seconds,
                            "startup_seconds": e2e.startup_seconds,
                        }, pid)
                    else:
                        await proj.log_activity(f"E2E: ✗ failed — {e2e.error or 'no endpoints responded'}")
                        await bus.publish("e2e_result", {
                            "success": False,
                            "error": e2e.error,
                            "endpoints_hit": e2e.endpoints_hit,
                            "endpoints_failed": e2e.endpoints_failed,
                        }, pid)
                except asyncio.TimeoutError:
                    await proj.log_activity("E2E: timed out after 240s")
                    await bus.publish("e2e_result", {"success": False, "error": "timeout"}, pid)
                except Exception as exc:
                    await proj.log_activity(f"E2E: error — {exc}")
                    await bus.publish("e2e_result", {"success": False, "error": str(exc)[:200]}, pid)

            # Phase 1: Docs (background) + BugFixer — heal before QA ever sees the code
            await asyncio.gather(_do_docs(), _do_heal())
            # Phase 2: QA fix loop — audit → if issues found → targeted fix → re-audit
            # Repeats up to 3 rounds or until QA approves or score stops improving
            _MAX_QA_ROUNDS = 3
            _prev_qa_score = -1.0
            for _qa_round in range(_MAX_QA_ROUNDS):
                await _do_qa(reaudit=(_qa_round > 0))
                if qa_approved:
                    break
                # Stop early if score didn't improve from the previous round
                if _qa_round > 0 and qa_score <= _prev_qa_score:
                    await proj.log_activity(
                        f"QA round {_qa_round + 1}: score unchanged ({qa_score:.0f}) — stopping loop"
                    )
                    break
                _prev_qa_score = qa_score
                if _qa_round < _MAX_QA_ROUNDS - 1:
                    # One focused BugFixer pass targeting whatever QA just flagged
                    await proj.log_activity(f"QA round {_qa_round + 1}: issues remain — running extra fix pass")
                    await bus.publish("heal_started", {"qa_round": _qa_round + 1}, pid)
                    extra_fixer = BugFixer(proj, str(src_dir), num_passes=1)
                    await extra_fixer.run()
                    sha = await git.commit(f"fix: QA round {_qa_round + 1} fix pass")
                    if sha:
                        await proj.record_commit(sha, f"fix: QA round {_qa_round + 1} fix pass")
                    await bus.publish("heal_complete", {"success": True, "qa_round": _qa_round + 1}, pid)
            # Phase 3: E2E on the fully healed code
            await _do_e2e()

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
        _current_pid.reset(_pid_token)
        _tee.clear_pid(pid)


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
