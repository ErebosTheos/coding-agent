import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.models import (
    CommandResult,
    ImplementationPlan,
    NodeExecutionRecord,
    NodeStatus,
    OrchestrationTelemetry,
    SessionReport,
)
from senior_agent.visual_reporter import VisualReporter


class VisualReporterTests(unittest.TestCase):
    def test_generate_mermaid_summary_success(self) -> None:
        plan = ImplementationPlan(
            feature_name="Feature Alpha",
            summary="Wire new module and patch core behavior.",
            new_files=["src/new_module.py"],
            modified_files=["src/core.py"],
            steps=["Create new module", "Update core integration"],
            validation_commands=["python -m pytest", "python -m ruff check ."],
            design_guidance="Keep surface area narrow.",
        )
        report = SessionReport(
            command="Implement Feature Alpha",
            initial_result=CommandResult(command="plan", return_code=0),
            final_result=CommandResult(
                command="python -m ruff check .",
                return_code=0,
                stdout="all checks passed",
                stderr="",
            ),
            attempts=[],
            success=True,
            blocked_reason=None,
        )

        mermaid = VisualReporter().generate_mermaid_summary(plan, report)

        self.assertIn("flowchart TD", mermaid)
        self.assertIn('Feature: Feature Alpha', mermaid)
        self.assertIn("NEW: src/new_module.py", mermaid)
        self.assertIn("MODIFIED: src/core.py", mermaid)
        self.assertIn("Plan Steps", mermaid)
        self.assertIn("Status: Success", mermaid)
        self.assertIn("Outcome: Success", mermaid)

    def test_generate_mermaid_summary_marks_failed_validation(self) -> None:
        plan = ImplementationPlan(
            feature_name="Feature Beta",
            summary="Demonstrate failed second validation command.",
            new_files=["src/new_module.py"],
            modified_files=[],
            steps=[],
            validation_commands=["python -m pytest", "python -m ruff check ."],
            design_guidance="",
        )
        report = SessionReport(
            command="Implement Feature Beta",
            initial_result=CommandResult(command="plan", return_code=0),
            final_result=CommandResult(
                command="python -m ruff check .",
                return_code=1,
                stdout="",
                stderr="ruff failures",
            ),
            attempts=[],
            success=False,
            blocked_reason="Validation failed",
        )

        mermaid = VisualReporter().generate_mermaid_summary(plan, report)

        self.assertIn("Validate: python -m pytest", mermaid)
        self.assertIn("Validate: python -m ruff check .", mermaid)
        self.assertIn("Outcome: Fail", mermaid)
        self.assertIn("Reason: Validation failed", mermaid)
        self.assertIn("Status: Success", mermaid)
        self.assertIn("Status: Fail", mermaid)

    def test_generate_mermaid_summary_renders_dependency_graph_nodes(self) -> None:
        plan = ImplementationPlan.from_dict(
            {
                "feature_name": "Graph Visual",
                "summary": "Render graph nodes",
                "dependency_graph": {
                    "feature_name": "Graph Visual",
                    "summary": "Render graph nodes",
                    "nodes": [
                        {
                            "node_id": "contract_api",
                            "title": "Contract",
                            "summary": "Define contract",
                            "contract_node": True,
                        },
                        {
                            "node_id": "impl_service",
                            "title": "Impl",
                            "summary": "Implement service",
                            "depends_on": ["contract_api"],
                        },
                    ],
                },
            }
        )
        report = SessionReport(
            command="graph visual",
            initial_result=CommandResult(command="graph", return_code=0),
            final_result=CommandResult(command="graph", return_code=0),
            attempts=[],
            node_records=[
                NodeExecutionRecord(
                    node_id="contract_api",
                    trace_id="trace1",
                    status=NodeStatus.SUCCESS,
                    level1_passed=True,
                    duration_seconds=0.1,
                    note="ok",
                ),
                NodeExecutionRecord(
                    node_id="impl_service",
                    trace_id="trace2",
                    status=NodeStatus.FAILED,
                    level1_passed=False,
                    duration_seconds=0.2,
                    note="fail",
                ),
            ],
            telemetry=OrchestrationTelemetry(
                total_node_seconds=0.3,
                wall_clock_seconds=0.25,
                parallel_gain=1.2,
                initial_concurrency=2,
                final_concurrency=1,
                adaptive_throttle_events=1,
                level1_pass_nodes=1,
                level1_failed_nodes=1,
                level2_failures=0,
            ),
            success=False,
            blocked_reason="contract failed",
        )

        mermaid = VisualReporter().generate_mermaid_summary(plan, report)

        self.assertIn("Execution Nodes", mermaid)
        self.assertIn("Node contract_api: Contract", mermaid)
        self.assertIn("Node impl_service: Impl", mermaid)
        self.assertIn("Parallel Gain", mermaid)

    def test_generate_dashboard_payload_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            recorder_dir = workspace / ".senior_agent"
            recorder_dir.mkdir(parents=True, exist_ok=True)
            (recorder_dir / "node_contract_api.log").write_text(
                "trace=trace1 node=contract_api heartbeat\n",
                encoding="utf-8",
            )

            plan = ImplementationPlan.from_dict(
                {
                    "feature_name": "Dashboard Graph",
                    "summary": "Render dashboard nodes",
                    "dependency_graph": {
                        "feature_name": "Dashboard Graph",
                        "summary": "Render dashboard nodes",
                        "nodes": [
                            {
                                "node_id": "contract_api",
                                "title": "Contract",
                                "summary": "Define contract",
                            },
                            {
                                "node_id": "impl_service",
                                "title": "Impl",
                                "summary": "Implement service",
                                "depends_on": ["contract_api"],
                            },
                        ],
                    },
                }
            )
            report = SessionReport(
                command="dashboard run",
                initial_result=CommandResult(command="dashboard", return_code=0),
                final_result=CommandResult(command="dashboard", return_code=0, stdout="ok"),
                attempts=[],
                node_records=[
                    NodeExecutionRecord(
                        node_id="contract_api",
                        trace_id="trace1",
                        status=NodeStatus.SUCCESS,
                        level1_passed=True,
                        duration_seconds=1.1,
                        note="done",
                    )
                ],
                telemetry=OrchestrationTelemetry(
                    total_node_seconds=1.1,
                    wall_clock_seconds=1.0,
                    parallel_gain=1.1,
                    initial_concurrency=2,
                    final_concurrency=1,
                    adaptive_throttle_events=1,
                    level1_pass_nodes=1,
                    level1_failed_nodes=0,
                    level2_failures=0,
                ),
                success=False,
                blocked_reason="still running",
            )

            reporter = VisualReporter()
            payload = reporter.generate_dashboard_payload(
                plan,
                report,
                workspace_root=workspace,
                stage="running",
            )
            html = reporter.generate_dashboard_html(
                initial_payload=payload,
                dashboard_json_relative_path="dashboard_graph.dashboard.json",
            )

            self.assertEqual(payload["feature_name"], "Dashboard Graph")
            self.assertEqual(payload["stage"], "running")
            self.assertEqual(len(payload["nodes"]), 2)
            first = payload["nodes"][0]
            self.assertEqual(first["node_id"], "contract_api")
            self.assertEqual(first["status"], "success")
            self.assertEqual(first["trace_log_path"], ".senior_agent/node_contract_api.log")
            self.assertIn("setInterval(refresh, 2000)", html)
            self.assertIn("dashboard_graph.dashboard.json", html)
            self.assertIn("Senior Agent Dashboard", html)


if __name__ == "__main__":
    unittest.main()
