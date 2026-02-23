from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from senior_agent.llm_client import LLMClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisualAuditResult:
    passed: bool
    visual_bugs: tuple[str, ...] = ()
    suggested_css_fixes: str = ""
    status: str = "completed"
    rationale: str = ""
    screenshot_path: str | None = None
    target_url: str | None = None
    entrypoint: str | None = None
    reviewer_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass": bool(self.passed),
            "visual_bugs": list(self.visual_bugs),
            "suggested_css_fixes": self.suggested_css_fixes,
            "status": self.status,
            "rationale": self.rationale,
            "screenshot_path": self.screenshot_path,
            "target_url": self.target_url,
            "entrypoint": self.entrypoint,
            "reviewer_output": self.reviewer_output,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


class VisualLinter:
    """Playwright + LLM visual verification for UI-focused workspaces."""

    def __init__(
        self,
        *,
        reviewer_llm_client: LLMClient,
        handoff_dir: str = ".senior_agent",
        page_timeout_ms: int = 20_000,
        max_image_base64_chars: int = 220_000,
        screenshot_filename: str = "visual_snapshot.png",
        local_server_start_timeout_seconds: float = 15.0,
        local_server_poll_interval_seconds: float = 0.25,
    ) -> None:
        self.reviewer_llm_client = reviewer_llm_client
        self.handoff_dir = handoff_dir
        self.page_timeout_ms = max(5_000, int(page_timeout_ms))
        self.max_image_base64_chars = max(10_000, int(max_image_base64_chars))
        self.screenshot_filename = screenshot_filename.strip() or "visual_snapshot.png"
        self.local_server_start_timeout_seconds = max(1.0, float(local_server_start_timeout_seconds))
        self.local_server_poll_interval_seconds = max(
            0.05,
            float(local_server_poll_interval_seconds),
        )

    def should_run(self, workspace_root: Path) -> bool:
        return self.detect_entrypoint(workspace_root) is not None

    def detect_entrypoint(self, workspace_root: Path) -> Path | None:
        root = workspace_root.resolve()
        direct_candidates = [
            root / "index.html",
            root / "web_app.py",
            root / "src" / "index.html",
            root / "public" / "index.html",
        ]
        for candidate in direct_candidates:
            if candidate.exists() and candidate.is_file():
                return candidate

        for candidate in root.rglob("index.html"):
            if self._is_ignored_path(candidate):
                continue
            if candidate.is_file():
                return candidate
        for candidate in root.rglob("web_app.py"):
            if self._is_ignored_path(candidate):
                continue
            if candidate.is_file():
                return candidate
        return None

    def prepare_environment(self, *, python_executable: str | None = None) -> tuple[bool, str]:
        exe = python_executable or sys.executable
        commands = [
            [exe, "-m", "pip", "install", "playwright", "pytest-playwright"],
            [exe, "-m", "playwright", "install", "chromium"],
        ]
        for command in commands:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                combined = "\n".join(
                    part.strip()
                    for part in (completed.stdout, completed.stderr)
                    if part.strip()
                )
                return False, (
                    f"Visual linter environment setup failed: {' '.join(command)} "
                    f"(code={completed.returncode}) {combined}"
                )
        return True, "Playwright + pytest-playwright installed; Chromium browser available."

    async def run(
        self,
        *,
        workspace_root: Path,
        ui_design_guidance: str,
        target_url: str | None = None,
    ) -> VisualAuditResult:
        root = workspace_root.resolve()
        entrypoint = self.detect_entrypoint(root)
        if entrypoint is None:
            result = VisualAuditResult(
                passed=True,
                status="skipped",
                rationale="No UI entrypoint detected (index.html/web_app.py).",
            )
            self._persist_result(root, result)
            return result

        handoff_root = (root / self.handoff_dir).resolve()
        screenshot_path = handoff_root / self.screenshot_filename
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)

        resolved_url = (
            target_url.strip()
            if isinstance(target_url, str) and target_url.strip()
            else self._default_url_for_entrypoint(entrypoint)
        )

        try:
            resolved_url = await self._capture_screenshot_with_server_fallback(
                workspace_root=root,
                entrypoint=entrypoint,
                target_url=resolved_url,
                destination=screenshot_path,
            )
        except Exception as exc:
            result = VisualAuditResult(
                passed=False,
                status="error",
                rationale=f"Screenshot capture failed: {exc}",
                screenshot_path=str(screenshot_path),
                target_url=resolved_url,
                entrypoint=str(entrypoint.relative_to(root)),
            )
            self._persist_result(root, result)
            return result

        prompt = self._build_visual_audit_prompt(
            ui_design_guidance=ui_design_guidance,
            screenshot_path=screenshot_path,
            target_url=resolved_url,
            entrypoint=entrypoint.relative_to(root).as_posix(),
        )
        reviewer_output = ""
        try:
            reviewer_output = await asyncio.to_thread(
                self.reviewer_llm_client.generate_fix,
                prompt,
            )
        except Exception as exc:
            result = VisualAuditResult(
                passed=False,
                status="error",
                rationale=f"Visual reviewer request failed: {exc}",
                screenshot_path=str(screenshot_path),
                target_url=resolved_url,
                entrypoint=str(entrypoint.relative_to(root)),
            )
            self._persist_result(root, result)
            return result

        passed, visual_bugs, suggested_css_fixes, rationale = self._parse_reviewer_output(
            reviewer_output
        )
        result = VisualAuditResult(
            passed=passed,
            visual_bugs=tuple(visual_bugs),
            suggested_css_fixes=suggested_css_fixes,
            status="completed",
            rationale=rationale,
            screenshot_path=str(screenshot_path),
            target_url=resolved_url,
            entrypoint=str(entrypoint.relative_to(root)),
            reviewer_output=reviewer_output.strip(),
        )
        self._persist_result(root, result)
        return result

    async def _capture_screenshot_with_server_fallback(
        self,
        *,
        workspace_root: Path,
        entrypoint: Path,
        target_url: str,
        destination: Path,
    ) -> str:
        try:
            await self._capture_screenshot(url=target_url, destination=destination)
            return target_url
        except Exception as exc:
            if not self._should_auto_boot_local_server(target_url=target_url, error=exc):
                raise
            logger.warning(
                "VisualLinter: capture failed for %s (%s). Attempting temporary local server boot.",
                target_url,
                exc,
            )
            process: asyncio.subprocess.Process | None = None
            booted_url = target_url
            try:
                process, booted_url = await self._launch_server_for_visual_capture(
                    workspace_root=workspace_root,
                    entrypoint=entrypoint,
                    target_url=target_url,
                )
                await self._capture_screenshot(url=booted_url, destination=destination)
                return booted_url
            finally:
                if process is not None:
                    await self._stop_server_process(process)

    def _should_auto_boot_local_server(self, *, target_url: str, error: Exception) -> bool:
        parsed = urlparse(target_url)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").strip().lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return False
        text = str(error).lower()
        return any(
            snippet in text
            for snippet in (
                "err_connection_refused",
                "econnrefused",
                "connection refused",
                "failed to establish a new connection",
            )
        )

    async def _launch_server_for_visual_capture(
        self,
        *,
        workspace_root: Path,
        entrypoint: Path,
        target_url: str,
    ) -> tuple[asyncio.subprocess.Process, str]:
        parsed = urlparse(target_url)
        host = (parsed.hostname or "127.0.0.1").strip() or "127.0.0.1"
        if host == "::1":
            host = "127.0.0.1"
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        normalized_url = f"http://{host}:{port}{parsed.path or '/'}"
        if parsed.query:
            normalized_url = f"{normalized_url}?{parsed.query}"

        candidates = self._build_server_command_candidates(
            workspace_root=workspace_root,
            entrypoint=entrypoint,
            host=host,
            port=port,
        )
        errors: list[str] = []
        for command, command_cwd in candidates:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(command_cwd),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ready = await self._wait_for_server_ready(
                host=host,
                port=port,
                process=process,
            )
            if ready:
                logger.info(
                    "VisualLinter: started temporary local server (%s) for screenshot capture.",
                    " ".join(command),
                )
                return process, normalized_url
            await self._stop_server_process(process)
            errors.append(f"{' '.join(command)} (failed to become ready)")

        raise RuntimeError(
            "Unable to start temporary local server for visual capture. Tried: "
            + "; ".join(errors)
        )

    def _build_server_command_candidates(
        self,
        *,
        workspace_root: Path,
        entrypoint: Path,
        host: str,
        port: int,
    ) -> list[tuple[list[str], Path]]:
        candidates: list[tuple[list[str], Path]] = []
        seen: set[tuple[tuple[str, ...], str]] = set()

        def _add(command: list[str], cwd: Path) -> None:
            key = (tuple(command), str(cwd.resolve()))
            if key in seen:
                return
            seen.add(key)
            candidates.append((command, cwd))

        root = workspace_root.resolve()
        entrypoint_resolved = entrypoint.resolve()
        preferred_scripts = [
            root / "app.py",
            root / "main.py",
            root / "web_app.py",
            entrypoint_resolved,
        ]
        for script in preferred_scripts:
            if not script.exists() or not script.is_file():
                continue
            if script.suffix != ".py":
                continue
            _add(
                [sys.executable, str(script), "--host", host, "--port", str(port)],
                root,
            )
            _add(
                [sys.executable, str(script), "--port", str(port)],
                root,
            )

        static_cwd = entrypoint_resolved.parent if entrypoint_resolved.name == "index.html" else root
        _add(
            [sys.executable, "-m", "http.server", str(port), "--bind", host],
            static_cwd,
        )
        return candidates

    async def _wait_for_server_ready(
        self,
        *,
        host: str,
        port: int,
        process: asyncio.subprocess.Process,
    ) -> bool:
        deadline = time.monotonic() + self.local_server_start_timeout_seconds
        while time.monotonic() < deadline:
            if process.returncode is not None:
                return False
            try:
                reader, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                await asyncio.sleep(self.local_server_poll_interval_seconds)
        return False

    async def _stop_server_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    async def _capture_screenshot(self, *, url: str, destination: Path) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "Playwright is not installed. Run: python -m pip install playwright pytest-playwright "
                "&& python -m playwright install chromium"
            ) from exc

        async with async_playwright() as manager:
            browser = await manager.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 2200})
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.page_timeout_ms,
                )
                await page.wait_for_timeout(500)
                await page.screenshot(path=str(destination), full_page=True)
            finally:
                await browser.close()

    def _default_url_for_entrypoint(self, entrypoint: Path) -> str:
        if entrypoint.name == "index.html":
            return entrypoint.resolve().as_uri()
        return "http://127.0.0.1:8080"

    def _build_visual_audit_prompt(
        self,
        *,
        ui_design_guidance: str,
        screenshot_path: Path,
        target_url: str,
        entrypoint: str,
    ) -> str:
        encoded_image = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")
        truncated = False
        if len(encoded_image) > self.max_image_base64_chars:
            encoded_image = encoded_image[: self.max_image_base64_chars]
            truncated = True
        truncation_note = "true" if truncated else "false"
        return (
            "You are a Visual QA reviewer. Evaluate screenshot compliance with the UI guidance.\n"
            "Respond with JSON only using this schema:\n"
            "{\"pass\": <true|false>, \"visual_bugs\": [\"...\"], "
            "\"suggested_css_fixes\": \"...\", \"rationale\": \"...\"}\n\n"
            f"UI Guidance:\n{ui_design_guidance.strip()}\n\n"
            f"Entrypoint: {entrypoint}\n"
            f"Target URL: {target_url}\n"
            f"Image Base64 Truncated: {truncation_note}\n"
            "Screenshot Base64 PNG:\n"
            f"{encoded_image}\n"
        )

    def _parse_reviewer_output(
        self,
        reviewer_output: str,
    ) -> tuple[bool, list[str], str, str]:
        cleaned = reviewer_output.strip()
        if cleaned.startswith("```"):
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                cleaned = cleaned[start : end + 1]

        payload: dict[str, Any] | None = None
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    parsed = json.loads(cleaned[start : end + 1])
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = None

        if payload is None:
            return (
                False,
                ["Reviewer returned invalid JSON payload."],
                "",
                "Visual reviewer output was not parseable JSON.",
            )

        raw_bugs = payload.get("visual_bugs")
        visual_bugs: list[str] = []
        if isinstance(raw_bugs, list):
            for item in raw_bugs:
                text = str(item).strip()
                if text:
                    visual_bugs.append(text)

        suggested_css_fixes = str(payload.get("suggested_css_fixes", "")).strip()
        rationale = str(payload.get("rationale", "")).strip()
        if not rationale:
            rationale = "No rationale provided."

        passed = bool(payload.get("pass", False))
        if passed and visual_bugs:
            # Conflict guard: explicit pass with non-empty bugs is considered a fail.
            passed = False
            rationale = (
                "Reviewer payload inconsistent: pass=true while visual_bugs is non-empty."
            )
        return passed, visual_bugs, suggested_css_fixes, rationale

    def _persist_result(self, workspace_root: Path, result: VisualAuditResult) -> None:
        output_path = workspace_root / self.handoff_dir / "visual_audit.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _is_ignored_path(self, path: Path) -> bool:
        ignored_parts = {".senior_agent", ".git", "node_modules", ".venv", "venv"}
        return any(part in ignored_parts for part in path.parts)
