import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.models import CommandResult, ImplementationPlan
from senior_agent.orchestrator import MultiAgentOrchestrator


@dataclass
class StaticPlanner:
    plan: ImplementationPlan | None = None
    error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def plan_feature(self, requirement: str, codebase_summary: str) -> ImplementationPlan:
        self.calls.append((requirement, codebase_summary))
        if self.error is not None:
            raise self.error
        if self.plan is None:
            raise RuntimeError("plan not configured")
        return self.plan


@dataclass
class QueueLLMClient:
    responses: list[str]
    prompts: list[str] = field(default_factory=list)

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise RuntimeError("no scripted response available")
        return self.responses.pop(0)


@dataclass
class ScriptedExecutor:
    command_results: dict[str, list[CommandResult]]
    command_calls: list[str] = field(default_factory=list)

    def __call__(self, command: str, workspace: Path) -> CommandResult:
        self.command_calls.append(command)
        scripted = self.command_results.get(command)
        if not scripted:
            return CommandResult(command=command, return_code=1, stderr="unconfigured command")
        return scripted.pop(0)


@dataclass
class NoopTestWriter:
    workspace: Path | str = "."
    generated_files: dict[str, str] = field(default_factory=dict)
    validation_commands: list[str] = field(default_factory=list)

    def generate_test_suite(self, plan: ImplementationPlan, files_content: dict[str, str]) -> dict[str, str]:
        return dict(self.generated_files)

    def build_validation_commands(self, test_files: object) -> list[str]:
        return list(self.validation_commands)


@dataclass
class StubDependencyManager:
    should_fix: bool = False
    calls: list[str] = field(default_factory=list)

    def check_and_fix_dependencies(self, result: CommandResult, workspace: Path) -> bool:
        self.calls.append(result.stderr)
        return self.should_fix


@dataclass
class StubStyleMimic:
    inferred_style: str = "Style: test conventions."
    calls: list[Path] = field(default_factory=list)

    def infer_project_style(self, workspace: Path) -> str:
        self.calls.append(Path(workspace))
        return self.inferred_style


class MultiAgentOrchestratorTests(unittest.TestCase):
    def test_execute_feature_request_creates_and_modifies_files_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            existing = workspace / "src" / "app.py"
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_text("VALUE = 'old'\\n", encoding="utf-8")

            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Feature X",
                    summary="Create helper and patch app.",
                    new_files=["src/helper.py"],
                    modified_files=["src/app.py"],
                    steps=["test: python -m unittest discover -s tests -v"],
                    validation_commands=["python -m unittest discover -s tests -v"],
                    design_guidance="Keep implementation minimal.",
                )
            )
            llm = QueueLLMClient(
                responses=[
                    "def helper() -> str:\n    return 'ok'\n",
                    "```python\nVALUE = 'new'\n```",
                ]
            )
            executor = ScriptedExecutor(
                command_results={
                    "python -m unittest discover -s tests -v": [
                        CommandResult(
                            command="python -m unittest discover -s tests -v",
                            return_code=0,
                            stdout="ok",
                            stderr="",
                        )
                    ]
                }
            )
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Build Feature X",
                codebase_summary="A small python service.",
                workspace=workspace,
            )

            self.assertTrue(result)
            self.assertEqual(
                (workspace / "src" / "helper.py").read_text(encoding="utf-8"),
                "def helper() -> str:\n    return 'ok'\n",
            )
            self.assertEqual(existing.read_text(encoding="utf-8"), "VALUE = 'new'\n")
            self.assertEqual(executor.command_calls, ["python -m unittest discover -s tests -v"])
            self.assertIn("VALUE = 'old'", llm.prompts[1])
            self.assertIn("Inferred Project Style: Style: test conventions.", llm.prompts[0])
            self.assertIn("Inferred Project Style: Style: test conventions.", llm.prompts[1])
            mermaid_path = workspace / "feature_x.mermaid"
            self.assertTrue(mermaid_path.exists())
            self.assertIn(
                "Outcome: Success",
                mermaid_path.read_text(encoding="utf-8"),
            )

    def test_blocks_out_of_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Escape",
                    summary="Attempt out-of-workspace write.",
                    new_files=["../outside.py"],
                    modified_files=[],
                    steps=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["print('bad')\\n"])
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="escape",
                codebase_summary="repo",
                workspace=workspace,
            )

            self.assertFalse(result)
            self.assertEqual(len(llm.prompts), 0)

    def test_returns_false_when_planner_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            planner = StaticPlanner(error=ValueError("bad plan"))
            llm = QueueLLMClient(responses=[])
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Build",
                codebase_summary="Summary",
                workspace=Path(tmp_dir),
            )

            self.assertFalse(result)

    def test_uses_plan_validation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Feature Y",
                    summary="Create only one file.",
                    new_files=["module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=["python -m unittest discover -s tests -v"],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 1\\n"])
            executor = ScriptedExecutor(
                command_results={
                    "python -m unittest discover -s tests -v": [
                        CommandResult(
                            command="python -m unittest discover -s tests -v",
                            return_code=0,
                            stdout="ok",
                            stderr="",
                        )
                    ]
                }
            )
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Build",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertTrue(result)
            self.assertEqual(executor.command_calls, ["python -m unittest discover -s tests -v"])

    def test_rolls_back_created_and_modified_files_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            existing = workspace / "src" / "app.py"
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_text("VALUE = 'original'\n", encoding="utf-8")

            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Atomic failure",
                    summary="Should rollback both files after validation failure.",
                    new_files=["src/new_module.py"],
                    modified_files=["src/app.py"],
                    steps=[],
                    validation_commands=["python -m pytest"],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(
                responses=[
                    "def created() -> int:\n    return 1\n",
                    "VALUE = 'mutated'\n",
                ]
            )
            executor = ScriptedExecutor(
                command_results={
                    "python -m pytest": [
                        CommandResult(
                            command="python -m pytest",
                            return_code=1,
                            stdout="",
                            stderr="failing tests",
                        )
                    ]
                }
            )
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Build",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertFalse(result)
            self.assertFalse((workspace / "src" / "new_module.py").exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "VALUE = 'original'\n")
            self.assertEqual(executor.command_calls, ["python -m pytest"])
            mermaid_path = workspace / "atomic_failure.mermaid"
            self.assertTrue(mermaid_path.exists())
            self.assertIn(
                "Outcome: Fail",
                mermaid_path.read_text(encoding="utf-8"),
            )

    def test_fails_environment_check_before_llm_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Env check",
                    summary="Should fail before generation.",
                    new_files=["new_file.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=["this_binary_should_not_exist_abc123 --version"],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["print('never called')\n"])
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Build",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertFalse(result)
            self.assertEqual(len(llm.prompts), 0)
            self.assertFalse((workspace / "new_file.py").exists())

    def test_returns_false_when_modified_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Modify missing file",
                    summary="Should fail before LLM call.",
                    new_files=[],
                    modified_files=["missing.py"],
                    steps=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["x = 1\\n"])
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Modify",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertFalse(result)
            self.assertEqual(len(llm.prompts), 0)

    def test_applies_generated_tests_from_test_writer_before_feature_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Feature Z",
                    summary="Create source plus generated tests.",
                    new_files=["src/feature_z.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 7\n"])
            generated_test_path = "tests/test_feature_z.py"
            generated_test_content = (
                "import unittest\n\n\n"
                "class FeatureZTests(unittest.TestCase):\n"
                "    def test_value(self) -> None:\n"
                "        self.assertEqual(7, 7)\n"
            )
            test_writer = NoopTestWriter(
                generated_files={generated_test_path: generated_test_content},
                validation_commands=[
                    "python -m unittest discover -s tests -p test_feature_z.py",
                ],
            )
            executor = ScriptedExecutor(
                command_results={
                    "python -m unittest discover -s tests -p test_feature_z.py": [
                        CommandResult(
                            command="python -m unittest discover -s tests -p test_feature_z.py",
                            return_code=0,
                            stdout="ok",
                            stderr="",
                        )
                    ]
                }
            )
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=test_writer,
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Build Feature Z",
                codebase_summary="Python service.",
                workspace=workspace,
            )

            self.assertTrue(result)
            self.assertTrue((workspace / "src" / "feature_z.py").exists())
            self.assertEqual(
                (workspace / generated_test_path).read_text(encoding="utf-8"),
                generated_test_content,
            )
            self.assertEqual(
                executor.command_calls,
                ["python -m unittest discover -s tests -p test_feature_z.py"],
            )

    def test_retries_validation_once_when_dependency_manager_fixes_missing_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Retry validation",
                    summary="Retry command once after dependency auto-fix.",
                    new_files=["src/retry_module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=["python -m pytest"],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 1\n"])
            executor = ScriptedExecutor(
                command_results={
                    "python -m pytest": [
                        CommandResult(
                            command="python -m pytest",
                            return_code=1,
                            stdout="",
                            stderr="ModuleNotFoundError: No module named 'requests'",
                        ),
                        CommandResult(
                            command="python -m pytest",
                            return_code=0,
                            stdout="ok",
                            stderr="",
                        ),
                    ]
                }
            )
            dependency_manager = StubDependencyManager(should_fix=True)
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=dependency_manager,
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Retry",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertTrue(result)
            self.assertEqual(
                executor.command_calls,
                ["python -m pytest", "python -m pytest"],
            )
            self.assertEqual(len(dependency_manager.calls), 1)


if __name__ == "__main__":
    unittest.main()
