from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent_v2.models import Contract, DependencyGraph, ExecutionNode
from senior_agent_v2.orchestrator import MultiAgentOrchestratorV2
from senior_agent_v2.visual_linter import VisualAuditResult


@dataclass
class _Plan:
    dependency_graph: DependencyGraph | None


@dataclass
class _StaticPlanner:
    graph: DependencyGraph
    calls: list[tuple[str, str]] = field(default_factory=list)

    def plan_feature(self, requirement: str, codebase_summary: str) -> _Plan:
        self.calls.append((requirement, codebase_summary))
        return _Plan(dependency_graph=self.graph)


@dataclass
class _StaticLLM:
    responses: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    file_responses: dict[str, str] = field(default_factory=dict)
    default_response: str = '{"pass": true, "rationale": "default pass"}'

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        match = re.search(r"^Target File:\s*(.+)$", prompt, flags=re.MULTILINE)
        if match:
            target = match.group(1).strip()
            if target in self.file_responses:
                return self.file_responses[target]
        if self.responses:
            return self.responses.pop(0)
        return self.default_response


@dataclass
class _StubVisualLinter:
    should_run_flag: bool = True
    run_result: VisualAuditResult = field(
        default_factory=lambda: VisualAuditResult(passed=True, status="completed")
    )
    run_results: list[VisualAuditResult] = field(default_factory=list)
    run_calls: int = 0
    guidance_samples: list[str] = field(default_factory=list)

    def should_run(self, workspace_root: Path) -> bool:
        return self.should_run_flag

    async def run(
        self,
        *,
        workspace_root: Path,
        ui_design_guidance: str,
        target_url: str | None = None,
    ) -> VisualAuditResult:
        self.run_calls += 1
        self.guidance_samples.append(ui_design_guidance)
        if self.run_results:
            return self.run_results.pop(0)
        return self.run_result


def _contract() -> Contract:
    return Contract(
        node_id="n1",
        purpose="Ensure service returns stable payload.",
        inputs=[{"name": "id", "type": "str"}],
        outputs=[{"name": "payload", "type": "dict"}],
        public_api=["get_payload(id: str) -> dict"],
        invariants=["Returned dict includes id"],
        error_taxonomy={"NotFound": "Raised when missing"},
        examples=[{"input": {"id": "x"}, "output": {"id": "x"}}],
    )


def _build_graph(
    *,
    red_command: str = 'python -c "import sys; sys.exit(1)"',
    green_command: str = 'python -c "print(\\"node-ok\\")"',
    global_command: str = 'python -c "print(\\"global-ok\\")"',
    modified_files: list[str] | None = None,
) -> DependencyGraph:
    return DependencyGraph(
        feature_name="V2 Task 3",
        summary="Lifecycle hardening and atomic merge",
        nodes=[
            ExecutionNode(
                node_id="n1",
                title="Node 1",
                summary="Audit target node",
                new_files=[],
                modified_files=modified_files or ["src/service.py"],
                steps=[f"red_test: {red_command}", "Implement service"],
                validation_commands=[green_command],
                depends_on=[],
                contract=_contract(),
            )
        ],
        global_validation_commands=[global_command],
    )


class MultiAgentOrchestratorV2Task3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_full_flow_runs_red_green_audit_merge_global_and_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            graph = _build_graph()
            planner = _StaticPlanner(graph=graph)
            reviewer = _StaticLLM(
                responses=['{"pass": true, "rationale": "Implementation matches contract."}']
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=reviewer,
                planner=planner,
                enable_persistent_daemons=False,
            )

            result = await orchestrator.execute_feature_request(
                requirement="Build service",
                workspace=workspace,
            )
            self.assertTrue(result)

            scorecard_path = workspace / ".senior_agent" / "nodes" / "n1" / "audit_scorecard.json"
            self.assertTrue(scorecard_path.exists())
            scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
            self.assertTrue(scorecard["pass"])

            session_report_path = workspace / ".senior_agent" / "v2_session_report.json"
            self.assertTrue(session_report_path.exists())
            session_payload = json.loads(session_report_path.read_text(encoding="utf-8"))
            self.assertTrue(session_payload["success"])
            self.assertGreaterEqual(
                float(session_payload["telemetry"]["parallel_gain"]),
                1.0,
            )
            self.assertIn("grid_efficiency", session_payload["telemetry"])
            self.assertGreaterEqual(
                float(session_payload["telemetry"]["grid_efficiency"]),
                0.0,
            )

            execution_log_path = workspace / ".senior_agent" / "nodes" / "n1" / "execution.log"
            self.assertTrue(execution_log_path.exists())
            execution_log = execution_log_path.read_text(encoding="utf-8")
            self.assertIn("[TraceID:", execution_log)
            self.assertIn("Starting node execution", execution_log)

            slug = "v2_task_3"
            self.assertTrue((workspace / f"{slug}.mermaid").exists())
            self.assertTrue((workspace / f"{slug}.dashboard.json").exists())
            self.assertTrue((workspace / f"{slug}.dashboard.html").exists())

    async def test_red_gate_fails_when_red_command_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            graph = _build_graph(red_command='python -c "print(\\"unexpected-green\\")"')
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(),
                planner=_StaticPlanner(graph=graph),
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build service",
                workspace=workspace,
            )
            self.assertFalse(result)

            session_report_path = workspace / ".senior_agent" / "v2_session_report.json"
            payload = json.loads(session_report_path.read_text(encoding="utf-8"))
            self.assertIn("RED gate failed", payload["blocked_reason"])

    async def test_audit_rejection_creates_change_request_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            reviewer = _StaticLLM(
                responses=['{"pass": false, "rationale": "Needs MAJOR contract update."}']
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=reviewer,
                planner=_StaticPlanner(graph=_build_graph()),
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build service",
                workspace=workspace,
            )
            self.assertFalse(result)

            change_request_path = workspace / ".senior_agent" / "nodes" / "n1" / "change_request.json"
            self.assertTrue(change_request_path.exists())
            payload = json.loads(change_request_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "required")
            self.assertEqual(payload["requested_version_bump"], "MAJOR")

    async def test_global_validation_gate_blocks_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )
            graph = _build_graph(global_command='python -c "import sys; sys.exit(1)"')
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(
                    responses=['{"pass": true, "rationale": "ok"}']
                ),
                planner=_StaticPlanner(graph=graph),
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build service",
                workspace=workspace,
            )
            self.assertFalse(result)

            session_report_path = workspace / ".senior_agent" / "v2_session_report.json"
            payload = json.loads(session_report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["telemetry"]["level2_failures"], 1)

    async def test_phase2_verifies_handoff_before_wave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            graph = _build_graph()
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(
                    responses=['{"pass": true, "rationale": "ok"}']
                ),
                planner=_StaticPlanner(graph=graph),
                enable_persistent_daemons=False,
            )
            orchestrator.workspace_root = workspace.resolve()
            handoff = orchestrator._phase1_export_handoff(graph)

            contract_path = workspace / ".senior_agent" / "nodes" / "n1" / "contract.json"
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            payload["purpose"] = "tampered"
            contract_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

            phase2 = await orchestrator._phase2_implementation_grid(handoff)
            self.assertFalse(phase2.success)

    async def test_phase5_atomic_merge_rolls_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            original_a = "A_OLD\n"
            original_b = "B_OLD\n"
            (workspace / "src" / "a.txt").write_text(original_a, encoding="utf-8")
            (workspace / "src" / "b.txt").write_text(original_b, encoding="utf-8")

            graph = _build_graph(
                modified_files=["src/a.txt", "src/b.txt"],
                green_command='python -c "print(\\"node-ok\\")"',
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(),
                planner=_StaticPlanner(graph=graph),
                enable_persistent_daemons=False,
            )
            orchestrator.workspace_root = workspace.resolve()
            handoff = orchestrator._phase1_export_handoff(graph)

            artifacts_dir = workspace / ".senior_agent" / "nodes" / "n1" / "artifacts" / "src"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "a.txt").write_text("A_NEW\n", encoding="utf-8")
            (artifacts_dir / "b.txt").write_text("B_NEW\n", encoding="utf-8")

            real_copyfile = __import__("shutil").copyfile
            copy_count = {"value": 0}

            def flaky_copy(source: str | Path, target: str | Path) -> None:
                copy_count["value"] += 1
                if copy_count["value"] == 2:
                    raise RuntimeError("simulated merge failure")
                real_copyfile(source, target)

            with patch("senior_agent_v2.orchestrator.shutil.copyfile", side_effect=flaky_copy):
                ok, note = await orchestrator._phase5_atomic_merge(
                    handoff=handoff,
                    audited_node_ids={"n1"},
                )

            self.assertFalse(ok)
            self.assertIn("rolled back", note)
            self.assertEqual((workspace / "src" / "a.txt").read_text(encoding="utf-8"), original_a)
            self.assertEqual((workspace / "src" / "b.txt").read_text(encoding="utf-8"), original_b)

    async def test_validation_daemon_path_executes_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(),
                planner=_StaticPlanner(graph=_build_graph()),
                enable_persistent_daemons=True,
            )
            orchestrator.workspace_root = workspace

            ok, result, _ = await orchestrator._run_validation_commands(
                commands=('python -c "print(\\"daemon-ok\\")"',),
                stage_label="daemon-test",
            )
            self.assertTrue(ok)
            self.assertIsNotNone(result)
            self.assertIsNotNone(orchestrator._validation_daemon_state)
            await orchestrator._shutdown_validation_daemon()
            self.assertIsNone(orchestrator._validation_daemon_state)

    async def test_watchdog_evicts_stale_node_and_rolls_back_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            graph = DependencyGraph(
                feature_name="Watchdog Eviction",
                summary="Evict stale node and rollback generated file",
                nodes=[
                    ExecutionNode(
                        node_id="n_watch",
                        title="Watchdog Node",
                        summary="Generate a file then stall in validation command",
                        new_files=["generated.py"],
                        modified_files=[],
                        steps=['red_test: python -c "import sys; sys.exit(1)"'],
                        validation_commands=['python -c "import time; time.sleep(2)"'],
                        depends_on=[],
                        contract=_contract(),
                    )
                ],
                global_validation_commands=[],
            )

            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(file_responses={"generated.py": "value = 1\n"}),
                reviewer_llm_client=_StaticLLM(),
                planner=_StaticPlanner(graph=graph),
                enable_persistent_daemons=False,
                validation_command_timeout_seconds=5.0,
                watchdog_timeout_seconds=0.2,
                watchdog_poll_interval_seconds=0.05,
            )

            result = await orchestrator.execute_feature_request(
                requirement="Eviction test",
                workspace=workspace,
            )
            self.assertFalse(result)

            generated_path = workspace / "generated.py"
            self.assertFalse(generated_path.exists())

            report_payload = json.loads(
                (workspace / ".senior_agent" / "v2_session_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report_payload["node_records"][0]["status"], "evicted")
            self.assertIn("heartbeat", report_payload["blocked_reason"].lower())

            execution_log = (
                workspace / ".senior_agent" / "nodes" / "n_watch" / "execution.log"
            ).read_text(encoding="utf-8")
            self.assertIn("Watchdog eviction triggered", execution_log)

    async def test_phase6b_visual_validation_runs_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><h1>Demo</h1></body></html>\n",
                encoding="utf-8",
            )
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            visual_linter = _StubVisualLinter(
                should_run_flag=True,
                run_result=VisualAuditResult(
                    passed=True,
                    status="completed",
                    visual_bugs=(),
                    suggested_css_fixes="",
                    rationale="UI matches guidance.",
                ),
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(
                    responses=['{"pass": true, "rationale": "ok"}']
                ),
                planner=_StaticPlanner(graph=_build_graph()),
                visual_linter=visual_linter,
                enable_visual_auto_heal=False,
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build UI and service",
                workspace=workspace,
            )
            self.assertTrue(result)
            self.assertEqual(visual_linter.run_calls, 1)
            self.assertTrue(visual_linter.guidance_samples)

    async def test_phase6b_visual_validation_failure_blocks_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><h1>Broken UI</h1></body></html>\n",
                encoding="utf-8",
            )
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            visual_linter = _StubVisualLinter(
                should_run_flag=True,
                run_result=VisualAuditResult(
                    passed=False,
                    status="completed",
                    visual_bugs=("Header is clipped on mobile.",),
                    suggested_css_fixes=".hero { padding-top: 24px; }",
                    rationale="Layout mismatch.",
                ),
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(
                    responses=['{"pass": true, "rationale": "ok"}']
                ),
                planner=_StaticPlanner(graph=_build_graph()),
                visual_linter=visual_linter,
                enable_visual_auto_heal=False,
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build UI and service",
                workspace=workspace,
            )
            self.assertFalse(result)
            report_payload = json.loads(
                (workspace / ".senior_agent" / "v2_session_report.json").read_text(encoding="utf-8")
            )
            self.assertIn("visual audit failed", str(report_payload["blocked_reason"]).lower())
            self.assertEqual(visual_linter.run_calls, 1)

    async def test_phase6b_visual_validation_skips_without_ui_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            visual_linter = _StubVisualLinter(should_run_flag=False)
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(),
                reviewer_llm_client=_StaticLLM(
                    responses=['{"pass": true, "rationale": "ok"}']
                ),
                planner=_StaticPlanner(graph=_build_graph()),
                visual_linter=visual_linter,
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build service only",
                workspace=workspace,
            )
            self.assertTrue(result)
            self.assertEqual(visual_linter.run_calls, 0)

    async def test_phase6b_auto_heal_generates_visual_fix_node_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><h1>Broken UI</h1></body></html>\n",
                encoding="utf-8",
            )
            (workspace / "styles.css").write_text(
                "h1 { margin-top: 0; }\n",
                encoding="utf-8",
            )
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            visual_linter = _StubVisualLinter(
                should_run_flag=True,
                run_results=[
                    VisualAuditResult(
                        passed=False,
                        status="completed",
                        visual_bugs=("Header is clipped on mobile.",),
                        suggested_css_fixes=".hero { padding-top: 24px; }",
                        rationale="Layout mismatch.",
                    ),
                    VisualAuditResult(
                        passed=True,
                        status="completed",
                        visual_bugs=(),
                        suggested_css_fixes="",
                        rationale="Visual fixes are now correct.",
                    ),
                ],
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(
                    file_responses={
                        "index.html": "<html><body><h1>Fixed UI</h1></body></html>\n",
                        "styles.css": ".hero { padding-top: 24px; }\n",
                    }
                ),
                reviewer_llm_client=_StaticLLM(
                    responses=[
                        '{"pass": true, "rationale": "primary node ok"}',
                        '{"pass": true, "rationale": "visual fix node ok"}',
                    ]
                ),
                planner=_StaticPlanner(graph=_build_graph()),
                visual_linter=visual_linter,
                max_visual_auto_heal_attempts=1,
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build UI and service",
                workspace=workspace,
            )
            self.assertTrue(result)
            self.assertEqual(visual_linter.run_calls, 2)

            auto_heal_path = workspace / ".senior_agent" / "visual_auto_heal_nodes.json"
            self.assertTrue(auto_heal_path.exists())
            payload = json.loads(auto_heal_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["attempt"], 1)
            node_id = str(payload[0]["node"]["node_id"])
            self.assertTrue(node_id.startswith("visual_fix_"))

            node_log_path = workspace / ".senior_agent" / "nodes" / node_id / "execution.log"
            self.assertTrue(node_log_path.exists())

    async def test_phase6b_auto_heal_exhausted_attempts_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "index.html").write_text(
                "<html><body><h1>Broken UI</h1></body></html>\n",
                encoding="utf-8",
            )
            (workspace / "src").mkdir(parents=True, exist_ok=True)
            (workspace / "src" / "service.py").write_text(
                "def get_payload(id: str) -> dict:\n    return {'id': id}\n",
                encoding="utf-8",
            )

            visual_linter = _StubVisualLinter(
                should_run_flag=True,
                run_results=[
                    VisualAuditResult(
                        passed=False,
                        status="completed",
                        visual_bugs=("Header overlaps CTA.",),
                        suggested_css_fixes=".hero { margin-top: 12px; }",
                        rationale="First failure.",
                    ),
                    VisualAuditResult(
                        passed=False,
                        status="completed",
                        visual_bugs=("Header overlaps CTA.",),
                        suggested_css_fixes=".hero { margin-top: 24px; }",
                        rationale="Still failing.",
                    ),
                ],
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=_StaticLLM(
                    file_responses={
                        "index.html": "<html><body><h1>Attempted Fix</h1></body></html>\n",
                    }
                ),
                reviewer_llm_client=_StaticLLM(
                    responses=[
                        '{"pass": true, "rationale": "primary node ok"}',
                        '{"pass": true, "rationale": "visual fix node ok"}',
                    ]
                ),
                planner=_StaticPlanner(graph=_build_graph()),
                visual_linter=visual_linter,
                max_visual_auto_heal_attempts=1,
                enable_persistent_daemons=False,
            )
            result = await orchestrator.execute_feature_request(
                requirement="Build UI and service",
                workspace=workspace,
            )
            self.assertFalse(result)
            self.assertEqual(visual_linter.run_calls, 2)
            report_payload = json.loads(
                (workspace / ".senior_agent" / "v2_session_report.json").read_text(encoding="utf-8")
            )
            self.assertIn("after 1 auto-heal attempt", str(report_payload["blocked_reason"]).lower())

    async def test_phase2b_generates_missing_new_file_from_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            graph = DependencyGraph(
                feature_name="Generate File",
                summary="Create missing file in phase2b",
                nodes=[
                    ExecutionNode(
                        node_id="n_gen",
                        title="Generate module",
                        summary="Generate a new python module from node contract",
                        new_files=["generated_module.py"],
                        modified_files=[],
                        steps=['red_test: python -c "import sys; sys.exit(1)"'],
                        validation_commands=["python -m py_compile generated_module.py"],
                        depends_on=[],
                        contract=_contract(),
                    )
                ],
                global_validation_commands=["python -m py_compile generated_module.py"],
            )

            coder = _StaticLLM(
                file_responses={
                    "generated_module.py": (
                        "def ping() -> str:\n"
                        "    return 'ok'\n"
                    )
                }
            )
            reviewer = _StaticLLM(
                responses=['{"pass": true, "rationale": "Generated module matches contract."}']
            )
            orchestrator = MultiAgentOrchestratorV2(
                llm_client=coder,
                reviewer_llm_client=reviewer,
                planner=_StaticPlanner(graph=graph),
                enable_persistent_daemons=False,
            )

            result = await orchestrator.execute_feature_request(
                requirement="Generate module",
                workspace=workspace,
            )
            self.assertTrue(result)

            generated_path = workspace / "generated_module.py"
            self.assertTrue(generated_path.exists())
            self.assertIn("def ping()", generated_path.read_text(encoding="utf-8"))

            artifact_path = (
                workspace
                / ".senior_agent"
                / "nodes"
                / "n_gen"
                / "artifacts"
                / "generated_module.py"
            )
            self.assertTrue(artifact_path.exists())


if __name__ == "__main__":
    unittest.main()
