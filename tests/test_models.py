import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.models import (
    AttemptRecord,
    CommandResult,
    DependencyGraph,
    ExecutionNode,
    FailureType,
    ImplementationPlan,
    NodeExecutionRecord,
    NodeStatus,
    OrchestrationTelemetry,
    SessionReport,
)


class SessionReportPersistenceTests(unittest.TestCase):
    def test_serializes_and_restores_session_report(self) -> None:
        report = SessionReport(
            command="python -m pytest",
            initial_result=CommandResult(
                command="python -m pytest",
                return_code=1,
                stdout="",
                stderr="initial failure",
            ),
            final_result=CommandResult(
                command="python -m ruff check .",
                return_code=0,
                stdout="lint ok",
                stderr="",
            ),
            attempts=[
                AttemptRecord(
                    attempt_number=1,
                    strategy_name="llm_strategy",
                    failure_type=FailureType.TEST_FAILURE,
                    applied=True,
                    note="patched",
                    changed_files=(Path("src/module.py"),),
                    diff_summary=("Modified src/module.py: +2/-1 lines.",),
                ),
            ],
            success=True,
            blocked_reason=None,
        )

        raw_json = report.to_json()
        restored = SessionReport.from_json(raw_json)

        self.assertEqual(restored.command, report.command)
        self.assertEqual(restored.initial_result, report.initial_result)
        self.assertEqual(restored.final_result, report.final_result)
        self.assertEqual(len(restored.attempts), 1)
        self.assertEqual(restored.attempts[0], report.attempts[0])
        self.assertTrue(restored.success)
        self.assertIsNone(restored.blocked_reason)

        parsed = json.loads(raw_json)
        self.assertEqual(parsed["attempts"][0]["failure_type"], "test_failure")

    def test_unknown_failure_type_defaults_to_unknown(self) -> None:
        payload = {
            "command": "python -m pytest",
            "initial_result": {"command": "python -m pytest", "return_code": 1},
            "final_result": {"command": "python -m pytest", "return_code": 1},
            "attempts": [
                {
                    "attempt_number": 1,
                    "strategy_name": "x",
                    "failure_type": "not_a_real_failure_type",
                    "applied": False,
                }
            ],
            "success": False,
        }

        restored = SessionReport.from_dict(payload)

        self.assertEqual(restored.attempts[0].failure_type, FailureType.UNKNOWN)

    def test_serializes_node_records_and_telemetry(self) -> None:
        report = SessionReport(
            command="run graph",
            initial_result=CommandResult(command="graph", return_code=0),
            final_result=CommandResult(command="graph", return_code=0),
            attempts=[],
            node_records=[
                NodeExecutionRecord(
                    node_id="node_1",
                    trace_id="trace123",
                    status=NodeStatus.SUCCESS,
                    level1_passed=True,
                    duration_seconds=1.25,
                    note="ok",
                    commands_run=("python -m py_compile src/a.py",),
                )
            ],
            telemetry=OrchestrationTelemetry(
                total_node_seconds=2.5,
                wall_clock_seconds=1.5,
                parallel_gain=1.66,
                initial_concurrency=4,
                final_concurrency=2,
                adaptive_throttle_events=1,
                level1_pass_nodes=1,
                level1_failed_nodes=0,
                level2_failures=0,
            ),
            success=True,
            blocked_reason=None,
        )

        restored = SessionReport.from_json(report.to_json())

        self.assertEqual(len(restored.node_records), 1)
        self.assertEqual(restored.node_records[0].node_id, "node_1")
        self.assertEqual(restored.node_records[0].status, NodeStatus.SUCCESS)
        self.assertIsNotNone(restored.telemetry)
        assert restored.telemetry is not None
        self.assertEqual(restored.telemetry.initial_concurrency, 4)
        self.assertEqual(restored.telemetry.final_concurrency, 2)

    def test_from_json_requires_object_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an object"):
            SessionReport.from_json("[]")


class ImplementationPlanTests(unittest.TestCase):
    def test_serializes_and_restores_implementation_plan(self) -> None:
        plan = ImplementationPlan(
            feature_name="Feature Planner",
            summary="Create planner from requirement.",
            new_files=["src/senior_agent/planner.py"],
            modified_files=["src/senior_agent/models.py"],
            steps=["Define plan model", "Parse LLM JSON"],
            validation_commands=["python -m unittest discover -s tests -v"],
            design_guidance="Keep diffs small and test-first.",
        )

        raw = plan.to_json()
        restored = ImplementationPlan.from_dict(json.loads(raw))

        self.assertEqual(restored, plan)

    def test_from_dict_requires_feature_name_and_summary(self) -> None:
        with self.assertRaisesRegex(ValueError, "feature_name"):
            ImplementationPlan.from_dict({"summary": "x"})
        with self.assertRaisesRegex(ValueError, "summary"):
            ImplementationPlan.from_dict({"feature_name": "x"})

    def test_from_dict_coerces_list_fields(self) -> None:
        restored = ImplementationPlan.from_dict(
            {
                "feature_name": "Planner",
                "summary": "Summary",
                "new_files": ["a.py", "", 1],
                "modified_files": "not-a-list",
                "steps": ["step 1", None, "  "],
                "validation_commands": ["python -m pytest", None, "  "],
                "design_guidance": " guidance ",
            }
        )

        self.assertEqual(restored.new_files, ["a.py"])
        self.assertEqual(restored.modified_files, [])
        self.assertEqual(restored.steps, ["step 1"])
        self.assertEqual(restored.validation_commands, ["python -m pytest"])
        self.assertEqual(restored.design_guidance, "guidance")

    def test_from_dict_parses_dependency_graph_and_aggregates_lists(self) -> None:
        restored = ImplementationPlan.from_dict(
            {
                "feature_name": "Graph plan",
                "summary": "Use graph nodes",
                "design_guidance": "contract first",
                "dependency_graph": {
                    "feature_name": "Graph plan",
                    "summary": "Use graph nodes",
                    "global_validation_commands": ["python -m pytest"],
                    "nodes": [
                        {
                            "node_id": "contract_api",
                            "title": "Contract",
                            "summary": "Define API contract",
                            "new_files": ["src/contracts.py"],
                            "modified_files": [],
                            "depends_on": [],
                            "validation_commands": ["python -m py_compile src/contracts.py"],
                            "contract_node": True,
                            "shared_resources": ["singleton_config"],
                        },
                        {
                            "node_id": "impl_service",
                            "title": "Implement",
                            "summary": "Implement service",
                            "new_files": [],
                            "modified_files": ["src/service.py"],
                            "depends_on": ["contract_api"],
                            "validation_commands": ["python -m pytest tests/test_service.py"],
                        },
                    ],
                },
            }
        )

        self.assertIsNotNone(restored.dependency_graph)
        assert restored.dependency_graph is not None
        self.assertEqual(
            restored.validation_commands,
            ["python -m pytest"],
        )
        self.assertIn("src/contracts.py", restored.new_files)
        self.assertIn("src/service.py", restored.modified_files)
        self.assertEqual(len(restored.dependency_graph.nodes), 2)


class DependencyGraphTests(unittest.TestCase):
    def test_validate_rejects_cycles(self) -> None:
        with self.assertRaisesRegex(ValueError, "cycle"):
            DependencyGraph.from_dict(
                {
                    "feature_name": "Cycle",
                    "summary": "bad",
                    "nodes": [
                        {"node_id": "a", "title": "A", "summary": "A", "depends_on": ["b"]},
                        {"node_id": "b", "title": "B", "summary": "B", "depends_on": ["a"]},
                    ],
                }
            )

    def test_all_collectors_deduplicate_values(self) -> None:
        graph = DependencyGraph(
            feature_name="G",
            summary="S",
            nodes=[
                ExecutionNode(
                    node_id="n1",
                    title="N1",
                    summary="N1",
                    new_files=["a.py", "a.py"],
                    modified_files=["b.py"],
                    steps=["step"],
                ),
                ExecutionNode(
                    node_id="n2",
                    title="N2",
                    summary="N2",
                    new_files=["c.py"],
                    modified_files=["b.py"],
                    steps=["step", "step2"],
                ),
            ],
        )

        self.assertEqual(graph.all_new_files(), ["a.py", "c.py"])
        self.assertEqual(graph.all_modified_files(), ["b.py"])
        self.assertEqual(graph.all_steps(), ["step", "step2"])


if __name__ == "__main__":
    unittest.main()
