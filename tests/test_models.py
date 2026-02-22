import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.models import (
    AttemptRecord,
    CommandResult,
    FailureType,
    ImplementationPlan,
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


if __name__ == "__main__":
    unittest.main()
