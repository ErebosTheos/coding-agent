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


if __name__ == "__main__":
    unittest.main()
