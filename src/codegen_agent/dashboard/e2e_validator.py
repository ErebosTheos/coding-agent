"""Container-based E2E smoke tester for generated projects.

Lifecycle:
  1. Auto-generate a Dockerfile if the project doesn't have one.
  2. docker build  →  docker run -d -p <random_host_port>:<app_port>
  3. Wait for port to accept connections (max 30s).
  4. Probe common endpoints (/, /health, /docs, /api, ...).
  5. Tear down container + image regardless of outcome.
  6. Return E2EResult with hit/failed endpoints and any error.

Skips gracefully when Docker is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

# ── Dockerfile templates ──────────────────────────────────────────────────────

_DF_FASTAPI = """\
FROM python:3.11-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir $(ls requirements*.txt | xargs -I{{}} echo -r {{}}) 2>/dev/null || true
COPY . .
EXPOSE {port}
CMD ["python", "-m", "uvicorn", "{module}:app", "--host", "0.0.0.0", "--port", "{port}"]
"""

_DF_FLASK = """\
FROM python:3.11-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir $(ls requirements*.txt | xargs -I{{}} echo -r {{}}) 2>/dev/null || true
COPY . .
EXPOSE {port}
ENV FLASK_APP={entry}
CMD ["python", "-m", "flask", "run", "--host", "0.0.0.0", "--port", "{port}"]
"""

_DF_PYTHON_GENERIC = """\
FROM python:3.11-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir $(ls requirements*.txt | xargs -I{{}} echo -r {{}}) 2>/dev/null || true
COPY . .
EXPOSE {port}
CMD ["python", "{entry}"]
"""

_DF_NODE = """\
FROM node:18-slim
WORKDIR /app
COPY package*.json ./
RUN npm install --silent 2>/dev/null || true
COPY . .
EXPOSE {port}
CMD ["node", "{entry}"]
"""

# ── Probe paths in order of priority ─────────────────────────────────────────
_PROBE_PATHS = ["/health", "/", "/docs", "/api", "/api/v1", "/ping", "/status"]


@dataclass
class E2EResult:
    success: bool = False
    endpoints_hit: list[str] = field(default_factory=list)
    endpoints_failed: list[str] = field(default_factory=list)
    error: str = ""
    docker_available: bool = True
    skipped: bool = False
    build_seconds: float = 0.0
    startup_seconds: float = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _docker_ok() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def _detect_port(src_dir: Path) -> int:
    for py_file in list(src_dir.rglob("*.py"))[:30]:
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
            for pat in (r'port\s*=\s*(\d{4,5})', r'PORT\s*=\s*(\d{4,5})'):
                m = re.search(pat, content)
                if m:
                    p = int(m.group(1))
                    if 1024 <= p <= 65535:
                        return p
        except OSError:
            pass
    return 8000


def _detect_stack(src_dir: Path) -> tuple[str, str, str]:
    """Returns (stack, entry_file, module_name).
    stack: 'fastapi' | 'flask' | 'node' | 'python'
    """
    if (src_dir / "package.json").exists():
        entry = next(
            (e for e in ("index.js", "app.js", "server.js") if (src_dir / e).exists()),
            "index.js",
        )
        return "node", entry, entry

    req_txt = src_dir / "requirements.txt"
    req_lower = ""
    if req_txt.exists():
        try:
            req_lower = req_txt.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass

    # Find entry point with app object
    candidates = ["main.py", "app.py", "run.py", "server.py", "application.py"]
    entry_file = "main.py"
    module = "main"
    for name in candidates:
        fp = src_dir / name
        if not fp.exists():
            # check src/ sub-layout
            fp = src_dir / "src" / name
        if fp.exists():
            entry_file = name
            module = name[:-3]
            break

    # Also scan for file with app = FastAPI() / app = Flask()
    for py_file in sorted(src_dir.rglob("*.py")):
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
            if re.search(r'\bapp\s*=\s*(FastAPI|Flask)\s*\(', content):
                entry_file = py_file.name
                rel = py_file.relative_to(src_dir)
                module = str(rel).replace("/", ".")[:-3]
                break
        except OSError:
            pass

    if "fastapi" in req_lower or "uvicorn" in req_lower:
        return "fastapi", entry_file, module
    if "flask" in req_lower:
        return "flask", entry_file, module
    return "python", entry_file, module


def _make_dockerfile(src_dir: Path, port: int) -> str:
    stack, entry, module = _detect_stack(src_dir)
    if stack == "node":
        return _DF_NODE.format(port=port, entry=entry)
    if stack == "fastapi":
        return _DF_FASTAPI.format(port=port, module=module)
    if stack == "flask":
        return _DF_FLASK.format(port=port, entry=entry)
    return _DF_PYTHON_GENERIC.format(port=port, entry=entry)


async def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> float:
    """Poll until port accepts connections. Returns elapsed seconds, or -1 on timeout."""
    import time
    start = time.monotonic()
    deadline = start + timeout
    while time.monotonic() < deadline:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return time.monotonic() - start
        except Exception:
            await asyncio.sleep(0.8)
    return -1.0


async def _run_proc(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a subprocess in the executor thread pool."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(args, capture_output=True, text=True, timeout=timeout),
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_e2e_smoke(
    src_dir: Path,
    project_name: str,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> E2EResult:
    """Build, start, probe, and teardown a container for the project."""
    import time

    if not _docker_ok():
        log.info("Docker unavailable — skipping E2E")
        return E2EResult(skipped=True, docker_available=False)

    port = _detect_port(src_dir)
    host_port = random.randint(20000, 29999)
    safe_name = re.sub(r"[^a-z0-9-]", "-", project_name.lower())[:30].strip("-")
    tag = f"codegen-e2e-{safe_name}"
    container_id: str | None = None

    # Auto-generate Dockerfile if missing
    dockerfile = src_dir / "Dockerfile"
    df_created = False
    if not dockerfile.exists():
        dockerfile.write_text(_make_dockerfile(src_dir, port), encoding="utf-8")
        df_created = True
        log.info("Generated Dockerfile for %s (stack detected)", project_name)

    try:
        # ── Build ────────────────────────────────────────────────────────────
        if on_event:
            await on_event("e2e_building", {"tag": tag, "port": port})
        t0 = time.monotonic()
        rc, _, err = await _run_proc(
            ["docker", "build", "-t", tag, "--quiet", str(src_dir)], timeout=180
        )
        build_secs = time.monotonic() - t0
        if rc != 0:
            return E2EResult(error=f"Build failed: {err[:300]}", build_seconds=build_secs)

        # ── Start container ──────────────────────────────────────────────────
        rc, cid, err = await _run_proc(
            ["docker", "run", "-d", "--rm", "-p", f"{host_port}:{port}", tag],
            timeout=20,
        )
        if rc != 0:
            return E2EResult(error=f"Run failed: {err[:300]}", build_seconds=build_secs)
        container_id = cid
        if on_event:
            await on_event("e2e_started", {"host_port": host_port, "container_port": port})

        # ── Wait for readiness ───────────────────────────────────────────────
        startup_secs = await _wait_for_port("127.0.0.1", host_port, timeout=35.0)
        if startup_secs < 0:
            return E2EResult(
                error="App did not accept connections within 35s",
                build_seconds=build_secs,
            )
        log.info("Container ready in %.1fs on port %d", startup_secs, host_port)

        # ── Probe endpoints ──────────────────────────────────────────────────
        result = E2EResult(build_seconds=build_secs, startup_seconds=startup_secs)
        base = f"http://127.0.0.1:{host_port}"
        try:
            import httpx
            async with httpx.AsyncClient(base_url=base, timeout=6.0, follow_redirects=True) as client:
                for path in _PROBE_PATHS:
                    try:
                        resp = await client.get(path)
                        entry = f"{path} → {resp.status_code}"
                        if resp.status_code < 500:
                            result.endpoints_hit.append(entry)
                        else:
                            result.endpoints_failed.append(entry)
                    except Exception as exc:
                        result.endpoints_failed.append(f"{path} → {type(exc).__name__}")
        except ImportError:
            import urllib.request as _ur
            for path in _PROBE_PATHS[:4]:
                try:
                    _ur.urlopen(f"{base}{path}", timeout=6)
                    result.endpoints_hit.append(f"{path} → 200")
                except Exception as exc:
                    result.endpoints_failed.append(f"{path} → {exc}")

        result.success = len(result.endpoints_hit) > 0
        return result

    except Exception as exc:
        log.error("E2E error for %s: %s", project_name, exc)
        return E2EResult(error=str(exc)[:300])

    finally:
        if container_id:
            await _run_proc(["docker", "stop", container_id], timeout=15)
        await _run_proc(["docker", "rmi", "-f", tag], timeout=20)
        if df_created and dockerfile.exists():
            try:
                dockerfile.unlink()
            except OSError:
                pass
