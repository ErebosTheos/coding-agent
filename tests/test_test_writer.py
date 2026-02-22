import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.models import ImplementationPlan
from senior_agent.test_writer import TestWriter


@dataclass
class QueueLLMClient:
    responses: list[str]
    prompts: list[str] = field(default_factory=list)

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise RuntimeError("no scripted test response available")
        return self.responses.pop(0)


class TestWriterTests(unittest.TestCase):
    def test_generates_python_test_files_with_unittest_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "tests").mkdir(parents=True, exist_ok=True)

            plan = ImplementationPlan(
                feature_name="Widget feature",
                summary="Create a widget helper.",
                new_files=["src/widget.py"],
                modified_files=[],
                steps=["Implement helper function"],
                validation_commands=[],
                design_guidance="Keep implementation small.",
            )
            llm = QueueLLMClient(
                responses=[
                    "```python\nimport unittest\n\nclass WidgetTests(unittest.TestCase):\n    def test_happy_path(self):\n        self.assertTrue(True)\n\n    def test_edge_case_one(self):\n        self.assertEqual(1, 1)\n\n    def test_edge_case_two(self):\n        self.assertIsNone(None)\n```"
                ]
            )
            writer = TestWriter(llm_client=llm, workspace=workspace)

            generated = writer.generate_test_suite(
                plan=plan,
                files_content={"src/widget.py": "def make_widget() -> str:\n    return 'ok'\n"},
            )
            commands = writer.build_validation_commands(generated.keys())

            self.assertIn("tests/test_widget.py", generated)
            self.assertIn("WidgetTests", generated["tests/test_widget.py"])
            self.assertIn("happy-path test", llm.prompts[0])
            self.assertEqual(
                commands,
                ["python -m unittest discover -s tests -p test_widget.py"],
            )

    def test_detects_pytest_and_builds_pytest_validation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
                encoding="utf-8",
            )
            writer = TestWriter(llm_client=QueueLLMClient(responses=[]), workspace=workspace)

            framework = writer.detect_framework()
            commands = writer.build_validation_commands(["tests/test_widget.py"])

            self.assertEqual(framework, "pytest")
            self.assertEqual(commands, ["pytest tests/test_widget.py"])

    def test_detects_jest_from_package_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "package.json").write_text(
                json.dumps({"scripts": {"test": "jest --runInBand"}}),
                encoding="utf-8",
            )
            writer = TestWriter(llm_client=QueueLLMClient(responses=[]), workspace=workspace)

            framework = writer.detect_framework()
            commands = writer.build_validation_commands(["tests/feature.test.js"])

            self.assertEqual(framework, "jest")
            self.assertEqual(commands, ["npx jest tests/feature.test.js"])


if __name__ == "__main__":
    unittest.main()
