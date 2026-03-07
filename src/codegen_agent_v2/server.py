"""V2 Server — FastAPI + SSE + pipeline runner.

Port: 7071
Events pushed via SSE to /api/v2/projects/{id}/stream
"""
from __future__ import annotations

import asyncio
import contextvars
import io
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

_BASE = Path(__file__).parent.parent.parent
_STATIC_V2 = _BASE / "static_v2"
_OUTPUT_DIR = _BASE / "output"
_BRIEFS_DIR = _BASE / "briefs"

# ── Brief generator prompt ─────────────────────────────────────────────────

_BRIEF_GEN_SYSTEM = """You are a UI/UX-focused product designer and software architect. Turn the user's project description into a clear, buildable brief.

Focus on the USER EXPERIENCE — every page, every screen, every interaction. Be specific about what users see and do.

Cover these sections concisely:

1. PROJECT OVERVIEW — name, purpose, who it's for

2. USER ROLES — each role, what they can do, permission level

3. PUBLIC PAGES (no login) — for each page: name, URL, what's on it, key UI elements

4. AUTHENTICATED PAGES per role — for each role, every screen after login:
   - Page name and URL
   - Layout description (sidebar? cards? table? form?)
   - What data is displayed
   - What actions/buttons are available
   - Any modals or drawers triggered from this page

5. KEY USER FLOWS — step-by-step for the 3-5 most important journeys
   (e.g. "Student takes a test", "Teacher publishes results", "Admin adds a user")

6. UI COMPONENTS NEEDED — list reusable components (data tables, modals, cards, forms, charts)

7. DATA & FEATURES — core entities, key fields, main business rules (keep it brief)

8. FILE STRUCTURE — directory tree, every HTML/CSS/JS/Python file listed and annotated

Plain text only. Be specific about UI — layout, labels, button text, empty states, error messages."""

# ── Per-project SSE queues ─────────────────────────────────────────────────

_queues: dict[str, list[asyncio.Queue]] = {}
_projects: dict[str, dict[str, Any]] = {}  # in-memory project registry
_tasks: dict[str, asyncio.Task] = {}        # running pipeline tasks
_current_pid: contextvars.ContextVar[str] = contextvars.ContextVar("v2_pid", default="")


async def _emit(project_id: str, event_type: str, data: dict[str, Any]) -> None:
    event = {"type": event_type, "data": data, "t": time.time(), "project_id": project_id}

    # Persist log lines to disk so they survive page refresh
    if event_type == "log":
        line = data.get("line", "")
        if line:
            log_file = _OUTPUT_DIR / project_id / "logs.txt"
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    for q in list(_queues.get(project_id, [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    log.debug("SSE %s %s %s", project_id, event_type, list(data.keys()))


# ── Stdout routing (live log lines → SSE) ─────────────────────────────────

class _RoutingTee(io.TextIOBase):
    def __init__(self, original: io.TextIOBase) -> None:
        self._orig = original
        self._bufs: dict[str, str] = {}

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
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda l=line, p=pid: asyncio.ensure_future(
                        _emit(p, "log", {"line": l})
                    )
                )
        self._bufs[pid] = buf
        return len(text)

    def flush(self) -> None:
        self._orig.flush()


_real_stdout = sys.stdout
_tee = _RoutingTee(_real_stdout)
sys.stdout = _tee  # type: ignore[assignment]


# ── FastAPI app ────────────────────────────────────────────────────────────

app = FastAPI(title="Codegen Agent V2", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

if _STATIC_V2.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_V2)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    f = _STATIC_V2 / "index.html"
    if f.exists():
        return FileResponse(str(f))
    return HTMLResponse("<h1>Codegen Agent V2</h1>")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "v2"}


# ── Project API ────────────────────────────────────────────────────────────

@app.get("/api/v2/projects")
async def list_projects():
    result = []
    # Load from output dir
    if _OUTPUT_DIR.exists():
        for state_file in sorted(_OUTPUT_DIR.glob("*/v2-state.json"), key=lambda p: -p.stat().st_mtime):
            try:
                data = json.loads(state_file.read_text())
                result.append(data)
            except Exception:
                pass
    return result


@app.get("/api/v2/projects/{project_id}")
async def get_project(project_id: str):
    state_file = _OUTPUT_DIR / project_id / "v2-state.json"
    if state_file.exists():
        return json.loads(state_file.read_text())
    if project_id in _projects:
        return _projects[project_id]
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/v2/projects/{project_id}/manifest")
async def get_manifest(project_id: str):
    manifest_file = _OUTPUT_DIR / project_id / "project_manifest.json"
    if manifest_file.exists():
        return json.loads(manifest_file.read_text())
    return JSONResponse({"error": "manifest not found"}, status_code=404)


@app.get("/api/v2/projects/{project_id}/files")
async def get_files(project_id: str):
    ws = _OUTPUT_DIR / project_id
    if not ws.exists():
        return []
    files = []
    for p in sorted(ws.rglob("*")):
        if p.is_file() and p.name not in ("v2-state.json", "project_manifest.json"):
            rel = str(p.relative_to(ws))
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                files.append({"path": rel, "lines": content.count("\n"), "size": p.stat().st_size})
            except Exception:
                pass
    return files


@app.get("/api/v2/projects/{project_id}/files/{file_path:path}")
async def get_file_content(project_id: str, file_path: str):
    ws = _OUTPUT_DIR / project_id
    target = (ws / file_path).resolve()
    # Path traversal guard
    if not str(target).startswith(str(ws.resolve())):
        return JSONResponse({"error": "invalid path"}, status_code=400)
    if not target.exists() or not target.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": file_path, "content": content, "lines": content.count("\n")}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/v2/projects/{project_id}/stream")
async def stream_events(project_id: str, request: Request):
    """SSE endpoint — streams real-time pipeline events."""
    q: asyncio.Queue = asyncio.Queue(maxsize=512)
    _queues.setdefault(project_id, []).append(q)

    async def event_gen() -> AsyncIterator[str]:
        try:
            # Send current state immediately
            state_file = _OUTPUT_DIR / project_id / "v2-state.json"
            if state_file.exists():
                data = json.loads(state_file.read_text())
                yield f"data: {json.dumps({'type': 'state_sync', 'data': data, 't': time.time()})}\n\n"

            # Replay persisted log history so refreshing the page restores the log
            log_file = _OUTPUT_DIR / project_id / "logs.txt"
            if log_file.exists():
                lines = [l for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
                if lines:
                    yield f"data: {json.dumps({'type': 'log_history', 'data': {'lines': lines[-500:]}, 't': time.time()})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat to keep connection alive
                    yield f"data: {json.dumps({'type': 'heartbeat', 't': time.time()})}\n\n"
        finally:
            try:
                _queues[project_id].remove(q)
            except (KeyError, ValueError):
                pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/v2/projects")
async def create_project(request: Request):
    body = await request.json()
    brief = body.get("brief", "").strip()
    if not brief:
        return JSONResponse({"error": "brief is required"}, status_code=400)

    project_id = re.sub(r"[^a-z0-9]+", "-", brief[:40].lower()).strip("-")
    project_id += "-" + uuid.uuid4().hex[:8]

    workspace = str(_OUTPUT_DIR / project_id)
    os.makedirs(workspace, exist_ok=True)

    state = {
        "id": project_id,
        "brief": brief,
        "status": "QUEUED",
        "created_at": time.time(),
        "updated_at": time.time(),
        "layers": [],
        "qa_score": None,
        "files_created": 0,
        "errors": [],
        "activity": [],
    }
    _projects[project_id] = state
    _save_state(project_id, state)

    # Launch pipeline in background
    task = asyncio.create_task(_run_pipeline(project_id, brief, workspace))
    _tasks[project_id] = task

    return {"id": project_id, "status": "QUEUED"}


@app.delete("/api/v2/projects/{project_id}")
async def delete_project(project_id: str):
    # Cancel running task if any
    task = _tasks.pop(project_id, None)
    if task and not task.done():
        task.cancel()
    # Mark cancelled in state if still running
    state = _projects.get(project_id, {})
    if state.get("status") not in ("COMPLETE", "FAILED", "LAYER_FAILED", "NEEDS_REVIEW"):
        _update_state(project_id, status="FAILED", errors=["Cancelled by user"])
        await _emit(project_id, "project_failed", {"error": "Cancelled by user", "status": "FAILED"})
    # Remove from memory and delete output directory from disk
    _projects.pop(project_id, None)
    workspace = _OUTPUT_DIR / project_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    return {"ok": True}


# ── Brief LLM helper ──────────────────────────────────────────────────────

async def _brief_llm_call(brief_text: str) -> str:
    """Generate a structured brief using the best available LLM.

    For brief generation the instructions + brief text are merged into one prompt
    so any CLI-based provider (Gemini, Claude) can handle it without system-prompt
    argument limitations.

    Priority:
    1. Gemini CLI  — fast, already configured as default
    2. Claude CLI  — fallback (slower, spawns subprocess)
    3. Anthropic API — if ANTHROPIC_API_KEY is set
    """
    from ..codegen_agent.llm.gemini_cli import GeminiCLIClient
    from ..codegen_agent.llm.claude_cli import ClaudeCLIClient
    from ..codegen_agent.llm.anthropic_api import AnthropicAPIClient

    # Merge system instructions into the user prompt so CLI providers work reliably
    combined = f"{_BRIEF_GEN_SYSTEM}\n\n{'='*72}\nUSER INPUT — PROJECT BRIEF / DESCRIPTION:\n{'='*72}\n\n{brief_text}"

    errors = []

    # 1. Gemini CLI (fast, no subprocess overhead)
    try:
        client = GeminiCLIClient()
        # Pass as single combined prompt — no separate system_prompt arg
        result = await client.generate(combined)
        if result and result.strip() and len(result.strip()) > 200:
            return result
        errors.append(f"gemini_cli: short/empty response ({len(result.strip())} chars)")
    except Exception as e:
        errors.append(f"gemini_cli: {e}")

    # 2. Claude CLI
    try:
        client = ClaudeCLIClient()
        result = await client.generate(combined)
        if result and result.strip() and len(result.strip()) > 200:
            return result
        errors.append(f"claude_cli: short/empty response ({len(result.strip())} chars)")
    except Exception as e:
        errors.append(f"claude_cli: {e}")

    # 3. Anthropic API
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            client = AnthropicAPIClient(model="claude-haiku-4-5-20251001")
            result = await client.generate(brief_text, system_prompt=_BRIEF_GEN_SYSTEM)
            if result and result.strip():
                return result
            errors.append("anthropic_api: empty response")
        except Exception as e:
            errors.append(f"anthropic_api: {e}")

    raise RuntimeError("All LLM providers failed: " + "; ".join(errors))


# ── Briefs API ─────────────────────────────────────────────────────────────

@app.post("/api/v2/briefs/generate")
async def generate_brief(request: Request):
    body = await request.json()
    name       = body.get("name", "").strip()
    brief_text = body.get("brief_text", "").strip()

    if not brief_text:
        return JSONResponse({"error": "brief_text is required"}, status_code=400)

    # Derive a slug from name, or from first line of brief_text
    if not name:
        name = brief_text.splitlines()[0][:60].strip()

    try:
        content = await _brief_llm_call(brief_text)
    except Exception as exc:
        log.error("Brief generation failed: %s", exc)
        return JSONResponse({"error": f"LLM error: {exc}"}, status_code=500)

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "brief"
    _BRIEFS_DIR.mkdir(parents=True, exist_ok=True)

    brief_file = _BRIEFS_DIR / f"{slug}.txt"
    if brief_file.exists():
        slug = slug + "-" + uuid.uuid4().hex[:6]
        brief_file = _BRIEFS_DIR / f"{slug}.txt"

    brief_file.write_text(content, encoding="utf-8")
    return {"name": slug, "content": content}


@app.get("/api/v2/briefs")
async def list_briefs():
    _BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    briefs = []
    for f in sorted(_BRIEFS_DIR.glob("*.txt"), key=lambda p: -p.stat().st_mtime):
        try:
            preview = f.read_text(encoding="utf-8")[:200].replace("\n", " ")
            briefs.append({
                "name": f.stem,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
                "preview": preview,
            })
        except Exception:
            pass
    return briefs


@app.get("/api/v2/briefs/{name}")
async def get_brief(name: str):
    # Security: no path traversal
    if "/" in name or "\\" in name or name.startswith("."):
        return JSONResponse({"error": "invalid name"}, status_code=400)
    f = _BRIEFS_DIR / f"{name}.txt"
    if not f.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"name": name, "content": f.read_text(encoding="utf-8")}


@app.put("/api/v2/briefs/{name}")
async def update_brief(name: str, request: Request):
    if "/" in name or "\\" in name or name.startswith("."):
        return JSONResponse({"error": "invalid name"}, status_code=400)
    body = await request.json()
    content = body.get("content", "")
    _BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    f = _BRIEFS_DIR / f"{name}.txt"
    f.write_text(content, encoding="utf-8")
    return {"ok": True}


@app.delete("/api/v2/briefs/{name}")
async def delete_brief(name: str):
    if "/" in name or "\\" in name or name.startswith("."):
        return JSONResponse({"error": "invalid name"}, status_code=400)
    f = _BRIEFS_DIR / f"{name}.txt"
    if f.exists():
        f.unlink()
    return {"ok": True}


# ── State persistence ──────────────────────────────────────────────────────

def _save_state(project_id: str, state: dict) -> None:
    state["updated_at"] = time.time()
    state_file = _OUTPUT_DIR / project_id / "v2-state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _update_state(project_id: str, **kwargs) -> dict:
    state = _projects.get(project_id, {})
    state.update(kwargs)
    state["updated_at"] = time.time()
    _projects[project_id] = state
    _save_state(project_id, state)
    return state


def _log_activity(project_id: str, msg: str) -> None:
    state = _projects.get(project_id, {})
    activity = state.get("activity", [])
    activity.append({"t": time.time(), "msg": msg})
    state["activity"] = activity[-200:]  # keep last 200 entries
    _projects[project_id] = state


# ── Brief file-tree extractor ─────────────────────────────────────────────

def _extract_file_tree(brief: str) -> str:
    """Pull the directory/file structure section out of a generated brief.

    Looks for a section that starts with keywords like 'FILE STRUCTURE',
    'DIRECTORY', 'SUGGESTED FILE', or a line that looks like a tree root
    (e.g. 'project-name/'). Returns the extracted block or empty string.
    """
    lines = brief.splitlines()
    start = -1
    for i, line in enumerate(lines):
        upper = line.upper()
        if any(kw in upper for kw in ("FILE STRUCTURE", "DIRECTORY STRUCTURE", "SUGGESTED FILE", "FILE TREE")):
            start = i
            break
        # Tree root: a line that looks like "somename/" with no leading spaces
        if line.strip().endswith("/") and not line.startswith(" ") and len(line.strip()) < 60 and i > 5:
            start = i
            break

    if start == -1:
        return ""

    # Collect until we hit the next major section heading or END marker
    block = []
    for line in lines[start:]:
        if line.strip().startswith("===") and block:
            break
        if line.strip().upper().startswith("END OF BRIEF"):
            break
        # Stop at a new numbered section heading (e.g. "12. SOMETHING") after we have content
        if block and len(block) > 3 and re.match(r"^\d+\.\s+[A-Z]", line.strip()):
            break
        block.append(line)

    return "\n".join(block).strip()


# ── Pipeline runner ────────────────────────────────────────────────────────

async def _run_pipeline(project_id: str, brief: str, workspace: str) -> None:
    """Full V2 pipeline: plan → execute layers → QA."""
    token = _current_pid.set(project_id)
    t0 = time.monotonic()

    async def emit(event_type: str, data: dict) -> None:
        await _emit(project_id, event_type, data)
        _log_activity(project_id, f"{event_type}: {list(data.keys())}")
        # Incrementally persist file→layer mapping as files are written
        if event_type == "file_done" and "file" in data and "layer" in data:
            st = _projects.get(project_id, {})
            flm = st.get("file_layer_map", {})
            flm[data["file"]] = data["layer"]
            st["file_layer_map"] = flm
            _projects[project_id] = st
            # Persist every 5 new files to avoid thrashing disk
            if len(flm) % 5 == 0:
                _save_state(project_id, st)

    try:
        _update_state(project_id, status="PLANNING")
        await emit("status_change", {"status": "PLANNING"})

        # Build LLM router
        from ..codegen_agent.llm.router import LLMRouter
        router = LLMRouter()
        planner_llm = router.get_client_for_role("planner")
        executor_llm = router.get_client_for_role("executor")
        qa_llm = router.get_client_for_role("qa_auditor")

        # Stage 1: Plan
        print(f"[V2] Planning project: {brief[:60]}")
        from .planner import PlannerV2
        planner = PlannerV2(planner_llm)

        # If the brief contains a file structure section, extract and inject it
        # so the planner uses the exact directory layout from the brief
        file_tree = _extract_file_tree(brief)
        plan = await planner.plan(brief, file_tree=file_tree)

        files_planned = len(plan.files)
        _update_state(project_id,
            status="ARCHITECTING",
            files_planned=files_planned,
            manifest={
                "project_name": plan.manifest.project_name,
                "stack": plan.manifest.stack,
                "models": list(plan.manifest.models.keys()),
                "routes": len(plan.manifest.routes),
                "auth_sub": plan.manifest.auth.sub_field,
            }
        )
        await emit("plan_ready", {
            "project_name": plan.manifest.project_name,
            "stack": plan.manifest.stack,
            "files_planned": files_planned,
            "layers": sorted(set(f.layer for f in plan.files)),
            "models": list(plan.manifest.models.keys()),
            "routes": len(plan.manifest.routes),
            "file_specs": [{"file_path": f.file_path, "layer": f.layer} for f in plan.files],
        })

        # Save initial manifest
        from .manifest import save as save_manifest
        save_manifest(plan.manifest, workspace)

        # Stage 2: Execute layers
        _update_state(project_id, status="BUILDING")
        await emit("status_change", {"status": "BUILDING"})

        from .executor import LayeredExecutor
        executor = LayeredExecutor(llm=executor_llm, workspace=workspace, emit=emit)
        layer_results = await executor.execute_plan(plan.files, plan.manifest)

        files_created = sum(len(lr.files) for lr in layer_results)
        all_files = [f for lr in layer_results for f in lr.files]

        # Store per-file layer map so UI can restore file cards after reload
        file_layer_map = {gf.file_path: gf.layer for gf in all_files}

        _update_state(project_id,
            status="QA_RUNNING",
            files_created=files_created,
            file_layer_map=file_layer_map,
            layers=[{
                "index": lr.layer,
                "name": lr.name,
                "status": lr.status,
                "files": len(lr.files),
                "heal_rounds": lr.heal_rounds,
                "errors": lr.errors,
                "duration_s": lr.duration_s,
            } for lr in layer_results],
        )
        await emit("status_change", {"status": "QA_RUNNING"})

        # Stage 3: QA
        print("[V2] Running QA audit...")
        from .qa import QAAuditorV2
        qa = QAAuditorV2(qa_llm, workspace)
        qa_score, qa_issues = await qa.audit(all_files, plan.manifest, plan.validation_commands)

        duration = round(time.monotonic() - t0, 1)
        _update_state(project_id,
            status="COMPLETE",
            qa_score=qa_score,
            duration_s=duration,
            qa_issues=qa_issues[:20],
        )
        await emit("project_done", {
            "qa_score": qa_score,
            "files_created": files_created,
            "duration_s": duration,
            "issues": qa_issues[:10],
        })
        print(f"[V2] Done — QA={qa_score} files={files_created} t={duration}s")

    except asyncio.CancelledError:
        # Server shutting down — save state so it's not stuck at PLANNING
        _update_state(project_id, status="FAILED", errors=["Server shutdown during pipeline"])
        raise  # re-raise so asyncio cleanup works properly
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        log.error("Pipeline error for %s: %s", project_id, tb)
        name = type(exc).__name__
        if "NeedsReview" in name:
            status = "NEEDS_REVIEW"
        elif "LayerGate" in name:
            status = "LAYER_FAILED"
        else:
            status = "FAILED"
        _update_state(project_id, status=status, errors=[str(exc)])
        await emit("project_failed", {"error": str(exc), "status": status})
    finally:
        _current_pid.reset(token)
        _tasks.pop(project_id, None)


# ── Entry point ────────────────────────────────────────────────────────────

def start(port: int = 7071, host: str = "127.0.0.1") -> None:
    print(f"Codegen Agent V2 → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
