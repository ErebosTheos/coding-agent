import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.models import (
    CommandResult,
    ImplementationPlan,
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


if __name__ == "__main__":
    unittest.main()
