from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent_v2.visual_linter import VisualLinter


@dataclass
class _StaticLLM:
    responses: list[str] = field(default_factory=list)

    def generate_fix(self, prompt: str) -> str:
        if self.responses:
            return self.responses.pop(0)
        return (
            '{"pass": true, "visual_bugs": [], "suggested_css_fixes": "", '
            '"rationale": "default pass"}'
        )


class VisualLinterTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_skips_when_no_ui_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            linter = VisualLinter(reviewer_llm_client=_StaticLLM())

            result = await linter.run(
                workspace_root=workspace,
                ui_design_guidance="Keep a clean layout.",
            )

            self.assertTrue(result.passed)
            self.assertEqual(result.status, "skipped")
            audit_path = workspace / ".senior_agent" / "visual_audit.json"
            self.assertTrue(audit_path.exists())

    async def test_run_parses_reviewer_json_and_persists_scorecard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><main>hello</main></body></html>\n",
                encoding="utf-8",
            )
            linter = VisualLinter(
                reviewer_llm_client=_StaticLLM(
                    responses=[
                        '{"pass": false, "visual_bugs": ["Button overlaps title"], '
                        '"suggested_css_fixes": ".btn{margin-top:8px;}", '
                        '"rationale": "Spacing mismatch."}'
                    ]
                )
            )

            async def _fake_capture(*, url: str, destination: Path) -> None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"PNG")

            with patch.object(linter, "_capture_screenshot", side_effect=_fake_capture):
                result = await linter.run(
                    workspace_root=workspace,
                    ui_design_guidance="Buttons should not overlap headings.",
                )

            self.assertFalse(result.passed)
            self.assertEqual(result.status, "completed")
            self.assertIn("Button overlaps title", result.visual_bugs)

            audit_payload = json.loads(
                (workspace / ".senior_agent" / "visual_audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit_payload["pass"])
            self.assertIn("Button overlaps title", audit_payload["visual_bugs"])

    async def test_invalid_reviewer_payload_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><main>hello</main></body></html>\n",
                encoding="utf-8",
            )
            linter = VisualLinter(reviewer_llm_client=_StaticLLM(responses=["not-json"]))

            async def _fake_capture(*, url: str, destination: Path) -> None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"PNG")

            with patch.object(linter, "_capture_screenshot", side_effect=_fake_capture):
                result = await linter.run(
                    workspace_root=workspace,
                    ui_design_guidance="Headings must remain visible.",
            )

            self.assertFalse(result.passed)
            self.assertEqual(result.status, "completed")
            self.assertIn("parseable", result.rationale.lower())

    async def test_capture_boots_temporary_server_on_connection_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><main>hello</main></body></html>\n",
                encoding="utf-8",
            )
            linter = VisualLinter(reviewer_llm_client=_StaticLLM())

            fake_process = object()
            capture_mock = AsyncMock(
                side_effect=[
                    RuntimeError(
                        "Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8080/"
                    ),
                    None,
                ]
            )

            with patch.object(linter, "_capture_screenshot", capture_mock):
                with patch.object(
                    linter,
                    "_launch_server_for_visual_capture",
                    AsyncMock(return_value=(fake_process, "http://127.0.0.1:8080/")),
                ) as launch_mock:
                    with patch.object(
                        linter,
                        "_stop_server_process",
                        AsyncMock(),
                    ) as stop_mock:
                        resolved = await linter._capture_screenshot_with_server_fallback(
                            workspace_root=workspace,
                            entrypoint=workspace / "index.html",
                            target_url="http://127.0.0.1:8080/",
                            destination=workspace / ".senior_agent" / "visual_snapshot.png",
                        )

            self.assertEqual(resolved, "http://127.0.0.1:8080/")
            self.assertEqual(capture_mock.await_count, 2)
            launch_mock.assert_awaited_once()
            stop_mock.assert_awaited_once_with(fake_process)

    async def test_capture_does_not_boot_server_for_non_refused_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><main>hello</main></body></html>\n",
                encoding="utf-8",
            )
            linter = VisualLinter(reviewer_llm_client=_StaticLLM())
            capture_mock = AsyncMock(side_effect=RuntimeError("Playwright timeout"))

            with patch.object(linter, "_capture_screenshot", capture_mock):
                with patch.object(
                    linter,
                    "_launch_server_for_visual_capture",
                    AsyncMock(),
                ) as launch_mock:
                    with self.assertRaises(RuntimeError):
                        await linter._capture_screenshot_with_server_fallback(
                            workspace_root=workspace,
                            entrypoint=workspace / "index.html",
                            target_url="http://127.0.0.1:8080/",
                            destination=workspace / ".senior_agent" / "visual_snapshot.png",
                        )

            launch_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
