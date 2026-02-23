import json
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.models import ImplementationPlan
from senior_agent.planner import FeaturePlanner


@dataclass
class FakeLLMClient:
    response: str
    prompts: list[str] = field(default_factory=list)

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class FeaturePlannerTests(unittest.TestCase):
    def test_plan_feature_returns_implementation_plan(self) -> None:
        llm = FakeLLMClient(
            response=(
                '{'
                '"feature_name":"FeaturePlanner",'
                '"summary":"Build planning module",'
                '"new_files":["src/senior_agent/planner.py"],'
                '"modified_files":["src/senior_agent/models.py"],'
                '"steps":["Create model","Create planner"],'
                '"validation_commands":["python -m unittest discover -s tests -v"],'
                '"design_guidance":"Use strict JSON parsing."'
                '}'
            )
        )
        planner = FeaturePlanner(llm_client=llm)

        plan = planner.plan_feature(
            requirement="Implement planner",
            codebase_summary="Python package with strategy-based healing engine.",
        )

        self.assertIsInstance(plan, ImplementationPlan)
        self.assertEqual(plan.feature_name, "FeaturePlanner")
        self.assertEqual(
            plan.validation_commands,
            ["python -m unittest discover -s tests -v"],
        )
        self.assertEqual(len(llm.prompts), 1)
        self.assertIn("Implement planner", llm.prompts[0])
        self.assertIn("Codebase Summary", llm.prompts[0])
        self.assertIn("dependency_graph", llm.prompts[0])

    def test_plan_feature_rejects_invalid_json(self) -> None:
        planner = FeaturePlanner(llm_client=FakeLLMClient(response="not json"))

        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            planner.plan_feature(
                requirement="Implement planner",
                codebase_summary="Python package",
            )

    def test_plan_feature_requires_json_object(self) -> None:
        planner = FeaturePlanner(llm_client=FakeLLMClient(response='["not","object"]'))

        with self.assertRaisesRegex(ValueError, "JSON object"):
            planner.plan_feature(
                requirement="Implement planner",
                codebase_summary="Python package",
            )

    def test_plan_feature_requires_non_empty_inputs(self) -> None:
        planner = FeaturePlanner(llm_client=FakeLLMClient(response="{}"))

        with self.assertRaisesRegex(ValueError, "requirement must not be empty"):
            planner.plan_feature(requirement="   ", codebase_summary="x")
        with self.assertRaisesRegex(ValueError, "codebase_summary must not be empty"):
            planner.plan_feature(requirement="x", codebase_summary="   ")

    def test_plan_feature_rejects_excessive_file_change_count(self) -> None:
        new_files = [f"src/file_{index}.py" for index in range(51)]
        response = json.dumps(
            {
                "feature_name": "LargePlan",
                "summary": "Too many files",
                "new_files": new_files,
                "modified_files": [],
                "steps": ["step"],
                "validation_commands": [],
                "design_guidance": "none",
            }
        )
        planner = FeaturePlanner(llm_client=FakeLLMClient(response=response))

        with self.assertRaisesRegex(ValueError, "file-change limit"):
            planner.plan_feature(
                requirement="Implement huge feature",
                codebase_summary="Python package",
            )

    def test_plan_feature_parses_dependency_graph_payload(self) -> None:
        planner = FeaturePlanner(
            llm_client=FakeLLMClient(
                response=json.dumps(
                    {
                        "feature_name": "GraphFeature",
                        "summary": "Node-based plan",
                        "design_guidance": "contract first",
                        "dependency_graph": {
                            "feature_name": "GraphFeature",
                            "summary": "Node-based plan",
                            "global_validation_commands": ["python -m pytest"],
                            "nodes": [
                                {
                                    "node_id": "contract_node",
                                    "title": "Contract",
                                    "summary": "Define API",
                                    "new_files": ["src/contracts.py"],
                                    "depends_on": [],
                                    "contract_node": True,
                                    "validation_commands": [
                                        "python -m py_compile src/contracts.py"
                                    ],
                                },
                                {
                                    "node_id": "impl_node",
                                    "title": "Implement",
                                    "summary": "Build service",
                                    "modified_files": ["src/service.py"],
                                    "depends_on": ["contract_node"],
                                    "validation_commands": ["python -m pytest tests/test_service.py"],
                                },
                            ],
                        },
                    }
                )
            )
        )

        plan = planner.plan_feature(
            requirement="Implement graph feature",
            codebase_summary="Python package",
        )

        self.assertIsNotNone(plan.dependency_graph)
        assert plan.dependency_graph is not None
        self.assertEqual(len(plan.dependency_graph.nodes), 2)
        self.assertEqual(plan.validation_commands, ["python -m pytest"])

    def test_large_request_enforces_atomic_node_window(self) -> None:
        planner = FeaturePlanner(
            llm_client=FakeLLMClient(
                response=json.dumps(
                    {
                        "feature_name": "LargeGraph",
                        "summary": "Large graph but too small",
                        "dependency_graph": {
                            "feature_name": "LargeGraph",
                            "summary": "Large graph but too small",
                            "nodes": [
                                {"node_id": "n1", "title": "N1", "summary": "N1"},
                                {"node_id": "n2", "title": "N2", "summary": "N2"},
                                {"node_id": "n3", "title": "N3", "summary": "N3"},
                            ],
                        },
                    }
                )
            )
        )

        with self.assertRaisesRegex(ValueError, "10-20 atomic nodes"):
            planner.plan_feature(
                requirement=(
                    "Large project brief: decompose a multi-service enterprise platform "
                    "with full parallel grid execution and staged rollout."
                ),
                codebase_summary="Polyglot monorepo with multiple interfaces and services.",
            )

    def test_disable_atomic_window_allows_large_request_with_small_graph(self) -> None:
        planner = FeaturePlanner(
            llm_client=FakeLLMClient(
                response=json.dumps(
                    {
                        "feature_name": "LargeGraph",
                        "summary": "Large graph but too small",
                        "dependency_graph": {
                            "feature_name": "LargeGraph",
                            "summary": "Large graph but too small",
                            "nodes": [
                                {"node_id": "n1", "title": "N1", "summary": "N1"},
                                {"node_id": "n2", "title": "N2", "summary": "N2"},
                                {"node_id": "n3", "title": "N3", "summary": "N3"},
                            ],
                        },
                    }
                )
            ),
            enforce_atomic_node_window=False,
        )

        plan = planner.plan_feature(
            requirement=(
                "Large project brief: decompose a multi-service enterprise platform "
                "with full parallel grid execution and staged rollout."
            ),
            codebase_summary="Polyglot monorepo with multiple interfaces and services.",
        )

        self.assertIsNotNone(plan.dependency_graph)
        assert plan.dependency_graph is not None
        self.assertEqual(len(plan.dependency_graph.nodes), 3)

    def test_large_request_accepts_graph_with_ten_nodes(self) -> None:
        nodes = [
            {"node_id": f"n{index}", "title": f"N{index}", "summary": f"N{index}"}
            for index in range(1, 11)
        ]
        planner = FeaturePlanner(
            llm_client=FakeLLMClient(
                response=json.dumps(
                    {
                        "feature_name": "LargeGraph",
                        "summary": "Large graph",
                        "dependency_graph": {
                            "feature_name": "LargeGraph",
                            "summary": "Large graph",
                            "nodes": nodes,
                        },
                    }
                )
            )
        )

        plan = planner.plan_feature(
            requirement="Large project brief for enterprise multi-service migration.",
            codebase_summary="Complex platform with many modules.",
        )

        self.assertIsNotNone(plan.dependency_graph)
        assert plan.dependency_graph is not None
        self.assertEqual(len(plan.dependency_graph.nodes), 10)

    def test_subtask_phase_context_does_not_enforce_atomic_window(self) -> None:
        planner = FeaturePlanner(
            llm_client=FakeLLMClient(
                response=json.dumps(
                    {
                        "feature_name": "SubtaskGraph",
                        "summary": "Small focused subtask",
                        "dependency_graph": {
                            "feature_name": "SubtaskGraph",
                            "summary": "Small focused subtask",
                            "nodes": [
                                {"node_id": "n1", "title": "N1", "summary": "N1"},
                                {"node_id": "n2", "title": "N2", "summary": "N2"},
                                {"node_id": "n3", "title": "N3", "summary": "N3"},
                                {"node_id": "n4", "title": "N4", "summary": "N4"},
                                {"node_id": "n5", "title": "N5", "summary": "N5"},
                                {"node_id": "n6", "title": "N6", "summary": "N6"},
                                {"node_id": "n7", "title": "N7", "summary": "N7"},
                            ],
                        },
                    }
                )
            )
        )

        plan = planner.plan_feature(
            requirement=(
                "Phase context: Task T1 - Core Architecture and RBAC Schema\n"
                "Subtask 1/6:\n"
                "Define initial project scaffolding and baseline accessibility support.\n"
                "Keep changes focused and small for this subtask only."
            ),
            codebase_summary=(
                "Monorepo with enterprise modules, project brief, and multi-service scope."
            ),
        )

        self.assertIsNotNone(plan.dependency_graph)
        assert plan.dependency_graph is not None
        self.assertEqual(len(plan.dependency_graph.nodes), 7)


if __name__ == "__main__":
    unittest.main()
