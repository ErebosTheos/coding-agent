import sys
import tempfile
import time
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
class FlakyPlanner:
    plan: ImplementationPlan
    failures_remaining: int = 1
    calls: list[tuple[str, str]] = field(default_factory=list)

    def plan_feature(self, requirement: str, codebase_summary: str) -> ImplementationPlan:
        self.calls.append((requirement, codebase_summary))
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise ValueError("transient planner failure")
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
class FlexibleExecutor:
    command_calls: list[str] = field(default_factory=list)

    def __call__(self, command: str, workspace: Path) -> CommandResult:
        self.command_calls.append(command)
        if "py_compile" in command:
            if "contract_fail.py" in command:
                return CommandResult(command=command, return_code=1, stdout="", stderr="compile failed")
            return CommandResult(command=command, return_code=0, stdout="ok", stderr="")
        if "python -m pytest" in command:
            return CommandResult(command=command, return_code=0, stdout="ok", stderr="")
        return CommandResult(command=command, return_code=0, stdout="ok", stderr="")


@dataclass
class PromptAwareLLMClient:
    prompts: list[str] = field(default_factory=list)

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "Target File: src/api.py" in prompt:
            return "API_VERSION = 'v2'\n"
        if "Target File: src/service_a.py" in prompt:
            return "def service_a() -> str:\n    return 'a'\n"
        if "Target File: src/service_b.py" in prompt:
            return "def service_b() -> str:\n    return 'b'\n"
        if "Target File: src/contract_fail.py" in prompt:
            return "VALUE = (\n"
        if "Target File: src/implementation.py" in prompt:
            return "VALUE = 1\n"
        if "Target File: src/shared.py" in prompt:
            return "VALUE = 2\n"
        return "VALUE = 0\n"


@dataclass
class FormattingViolationLLMClient:
    prompts: list[str] = field(default_factory=list)

    def generate_fix(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "Target File: src/shared.py" in prompt:
            return "VALUE = 2   \nFLAG = True\n"
        return "VALUE = 0\n"


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

    def test_autodetects_validation_commands_when_plan_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "tests").mkdir(parents=True, exist_ok=True)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Autodetect validation",
                    summary="Create one file with no explicit validation commands.",
                    new_files=["module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 3\n"])
            autodetected_command = "python -m unittest discover -s tests -v"
            executor = ScriptedExecutor(
                command_results={
                    autodetected_command: [
                        CommandResult(
                            command=autodetected_command,
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
            self.assertEqual(executor.command_calls, [autodetected_command])

    def test_fails_when_no_validation_commands_can_be_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="No validations",
                    summary="No validation commands and no detectable project tooling.",
                    new_files=["module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 4\n"])
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

    def test_fast_mode_allows_execution_without_validation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Fast mode build",
                    summary="Should skip strict validation gate in fast mode.",
                    new_files=["module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 5\n"])
            executor = ScriptedExecutor(command_results={})
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Build quickly",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=True,
            )

            self.assertTrue(result)
            self.assertEqual((workspace / "module.py").read_text(encoding="utf-8"), "VALUE = 5\n")
            self.assertEqual(executor.command_calls, [])

    def test_adds_symbol_graph_impacted_validation_for_modified_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            source_dir = workspace / "src"
            test_dir = workspace / "tests"
            source_dir.mkdir(parents=True, exist_ok=True)
            test_dir.mkdir(parents=True, exist_ok=True)

            core_file = source_dir / "core.py"
            core_file.write_text(
                "def process() -> int:\n"
                "    return 1\n",
                encoding="utf-8",
            )
            (source_dir / "service.py").write_text(
                "from core import process\n"
                "VALUE = process()\n",
                encoding="utf-8",
            )
            (test_dir / "test_service.py").write_text(
                "import unittest\n\n"
                "class ServiceTests(unittest.TestCase):\n"
                "    def test_value(self) -> None:\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Core update",
                    summary="Modify core implementation only.",
                    new_files=[],
                    modified_files=["src/core.py"],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["def process() -> int:\n    return 2\n"])
            validation_command = "python -m unittest discover -s tests -p test_service.py"
            executor = ScriptedExecutor(
                command_results={
                    validation_command: [
                        CommandResult(
                            command=validation_command,
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
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Update process",
                codebase_summary="Python app with unit tests.",
                workspace=workspace,
            )

            self.assertTrue(result)
            self.assertEqual(executor.command_calls, [validation_command])
            self.assertIn("return 2", core_file.read_text(encoding="utf-8"))

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

    def test_persists_fix_cache_and_reuses_it_for_same_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target = workspace / "module.py"
            target.write_text("VALUE = 1\n", encoding="utf-8")

            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Cached update",
                    summary="Use cache on repeated requirement.",
                    new_files=[],
                    modified_files=["module.py"],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 2\n"])
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                generation_concurrency=1,
                enable_fix_cache=True,
            )

            first = orchestrator.execute_feature_request(
                requirement="Update cached module",
                codebase_summary="Small module",
                workspace=workspace,
                fast_mode=True,
            )
            self.assertTrue(first)
            self.assertEqual(target.read_text(encoding="utf-8"), "VALUE = 2\n")

            target.write_text("VALUE = 1\n", encoding="utf-8")
            second = orchestrator.execute_feature_request(
                requirement="Update cached module",
                codebase_summary="Small module",
                workspace=workspace,
                fast_mode=True,
            )
            self.assertTrue(second)
            self.assertEqual(target.read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertEqual(len(llm.prompts), 1)

    def test_parallel_generation_runs_self_critique_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Critique",
                    summary="Critique generated file before write.",
                    new_files=["module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(
                responses=[
                    "VALUE = 1\n",
                    "VALUE = 2\n",
                ]
            )
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                generation_concurrency=4,
                enable_self_critique=True,
                enable_fix_cache=False,
            )

            result = orchestrator.execute_feature_request(
                requirement="Build module",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=True,
            )

            self.assertTrue(result)
            self.assertEqual((workspace / "module.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertEqual(len(llm.prompts), 2)
            self.assertIn("Role: Senior Code Critic.", llm.prompts[1])

    def test_executes_dependency_graph_nodes_and_runs_global_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            api_path = workspace / "src" / "api.py"
            api_path.parent.mkdir(parents=True, exist_ok=True)
            api_path.write_text("API_VERSION = 'v1'\n", encoding="utf-8")

            planner = StaticPlanner(
                plan=ImplementationPlan.from_dict(
                    {
                        "feature_name": "Graph Build",
                        "summary": "Run dependency graph",
                        "design_guidance": "contract-first",
                        "dependency_graph": {
                            "feature_name": "Graph Build",
                            "summary": "Run dependency graph",
                            "global_validation_commands": [
                                "python -m py_compile src/api.py src/service_a.py src/service_b.py"
                            ],
                            "nodes": [
                                {
                                    "node_id": "contract_api",
                                    "title": "Contract",
                                    "summary": "Update API contract",
                                    "modified_files": ["src/api.py"],
                                    "validation_commands": [
                                        "python -m py_compile src/api.py"
                                    ],
                                    "contract_node": True,
                                },
                                {
                                    "node_id": "impl_a",
                                    "title": "A",
                                    "summary": "Implement A",
                                    "new_files": ["src/service_a.py"],
                                    "depends_on": ["contract_api"],
                                },
                                {
                                    "node_id": "impl_b",
                                    "title": "B",
                                    "summary": "Implement B",
                                    "new_files": ["src/service_b.py"],
                                    "depends_on": ["contract_api"],
                                },
                            ],
                        },
                    }
                )
            )
            llm = PromptAwareLLMClient()
            executor = FlexibleExecutor()
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                node_concurrency=2,
                generation_concurrency=1,
            )

            result = orchestrator.execute_feature_request(
                requirement="Graph Build",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertTrue(result)
            self.assertEqual(api_path.read_text(encoding="utf-8"), "API_VERSION = 'v2'\n")
            self.assertTrue((workspace / "src" / "service_a.py").exists())
            self.assertTrue((workspace / "src" / "service_b.py").exists())
            self.assertTrue((workspace / ".senior_agent" / "node_contract_api.log").exists())
            mermaid = (workspace / "graph_build.mermaid").read_text(encoding="utf-8")
            self.assertTrue((workspace / "graph_build.dashboard.json").exists())
            self.assertTrue((workspace / "graph_build.dashboard.html").exists())
            self.assertIn("Execution Nodes", mermaid)
            self.assertIn("Parallel Gain", mermaid)

    def test_disable_runtime_checks_skips_graph_validation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan.from_dict(
                    {
                        "feature_name": "Graph Build",
                        "summary": "Run dependency graph without checks",
                        "design_guidance": "code first",
                        "dependency_graph": {
                            "feature_name": "Graph Build",
                            "summary": "Run dependency graph without checks",
                            "global_validation_commands": ["ls -la"],
                            "nodes": [
                                {
                                    "node_id": "impl",
                                    "title": "Implement",
                                    "summary": "Write module",
                                    "new_files": ["src/module.py"],
                                    "validation_commands": ["ls -la"],
                                },
                            ],
                        },
                    }
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 1\n"])
            executor = ScriptedExecutor(command_results={})
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                disable_runtime_checks=True,
            )

            result = orchestrator.execute_feature_request(
                requirement="Graph build no checks",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=False,
            )

            self.assertTrue(result)
            self.assertTrue((workspace / "src" / "module.py").exists())
            self.assertEqual(executor.command_calls, [])

    def test_contract_node_failure_evicts_dependents_and_rolls_back_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan.from_dict(
                    {
                        "feature_name": "Contract Failure",
                        "summary": "Contract node should fail",
                        "dependency_graph": {
                            "feature_name": "Contract Failure",
                            "summary": "Contract node should fail",
                            "nodes": [
                                {
                                    "node_id": "contract",
                                    "title": "Contract",
                                    "summary": "Write invalid contract",
                                    "new_files": ["src/contract_fail.py"],
                                    "validation_commands": [
                                        "python -m py_compile src/contract_fail.py"
                                    ],
                                    "contract_node": True,
                                },
                                {
                                    "node_id": "impl",
                                    "title": "Impl",
                                    "summary": "Should be evicted",
                                    "new_files": ["src/implementation.py"],
                                    "depends_on": ["contract"],
                                },
                            ],
                        },
                    }
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 2\n"])
            executor = FlexibleExecutor()
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                node_concurrency=2,
                generation_concurrency=1,
            )

            result = orchestrator.execute_feature_request(
                requirement="Contract failure graph",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertFalse(result)
            self.assertFalse((workspace / "src" / "contract_fail.py").exists())
            self.assertFalse((workspace / "src" / "implementation.py").exists())
            self.assertEqual(len(llm.prompts), 1)

    def test_merges_conflicting_file_owners_in_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            shared = workspace / "src" / "shared.py"
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_text("VALUE = 1\n", encoding="utf-8")

            planner = StaticPlanner(
                plan=ImplementationPlan.from_dict(
                    {
                        "feature_name": "Merge Conflicts",
                        "summary": "Conflicting writes should merge",
                        "dependency_graph": {
                            "feature_name": "Merge Conflicts",
                            "summary": "Conflicting writes should merge",
                            "nodes": [
                                {
                                    "node_id": "node_a",
                                    "title": "A",
                                    "summary": "A modifies shared",
                                    "modified_files": ["src/shared.py"],
                                },
                                {
                                    "node_id": "node_b",
                                    "title": "B",
                                    "summary": "B also modifies shared",
                                    "modified_files": ["src/shared.py"],
                                },
                            ],
                        },
                    }
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 2\n"])
            executor = FlexibleExecutor()
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                generation_concurrency=1,
            )

            result = orchestrator.execute_feature_request(
                requirement="Merge conflicting graph",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=True,
            )

            self.assertTrue(result)
            self.assertEqual(shared.read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertEqual(len(llm.prompts), 1)

    def test_semantic_merge_gate_blocks_unformatted_auto_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            shared = workspace / "src" / "shared.py"
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_text("VALUE = 1\n", encoding="utf-8")

            planner = StaticPlanner(
                plan=ImplementationPlan.from_dict(
                    {
                        "feature_name": "Merge Conflicts",
                        "summary": "Conflicting writes should merge",
                        "dependency_graph": {
                            "feature_name": "Merge Conflicts",
                            "summary": "Conflicting writes should merge",
                            "global_validation_commands": [
                                "python -m pytest tests/test_shared.py"
                            ],
                            "nodes": [
                                {
                                    "node_id": "node_a",
                                    "title": "A",
                                    "summary": "A modifies shared",
                                    "modified_files": ["src/shared.py"],
                                },
                                {
                                    "node_id": "node_b",
                                    "title": "B",
                                    "summary": "B also modifies shared",
                                    "modified_files": ["src/shared.py"],
                                },
                            ],
                        },
                    }
                )
            )
            llm = FormattingViolationLLMClient()
            executor = FlexibleExecutor()
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                executor=executor,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                generation_concurrency=1,
            )

            result = orchestrator.execute_feature_request(
                requirement="Merge conflicting graph with strict semantic merge gate",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=False,
            )

            self.assertFalse(result)
            self.assertEqual(shared.read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_rejects_non_allowlisted_graph_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan.from_dict(
                    {
                        "feature_name": "Bad command",
                        "summary": "Should block disallowed binary",
                        "dependency_graph": {
                            "feature_name": "Bad command",
                            "summary": "Should block disallowed binary",
                            "nodes": [
                                {
                                    "node_id": "node_1",
                                    "title": "N1",
                                    "summary": "N1",
                                    "new_files": ["src/a.py"],
                                    "validation_commands": ["curl https://example.com"],
                                }
                            ],
                        },
                    }
                )
            )
            llm = PromptAwareLLMClient()
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Bad command",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertFalse(result)
            self.assertEqual(len(llm.prompts), 0)

    def test_fast_mode_retries_transient_planner_failure_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = FlakyPlanner(
                plan=ImplementationPlan(
                    feature_name="Retry planner",
                    summary="Recover from transient planner failure in fast mode.",
                    new_files=["src/module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 1\n"])
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Retry planner",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=True,
            )

            self.assertTrue(result)
            self.assertEqual(len(planner.calls), 2)
            self.assertTrue((workspace / "src" / "module.py").exists())

    def test_parallel_apply_treats_existing_new_file_as_modified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            shared = workspace / "src" / "shared.py"
            shared.parent.mkdir(parents=True, exist_ok=True)
            shared.write_text("VALUE = 1\n", encoding="utf-8")

            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Reclassify Existing New File",
                    summary="Treat existing file declared as new as a modification.",
                    new_files=["src/shared.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=[],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 2\n"])
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                generation_concurrency=4,
                enable_self_critique=False,
            )

            result = orchestrator.execute_feature_request(
                requirement="Reclassify Existing New File",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=True,
            )

            self.assertTrue(result)
            self.assertEqual(shared.read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertEqual(len(llm.prompts), 1)

    def test_fast_mode_sanitizes_non_allowlisted_graph_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan.from_dict(
                    {
                        "feature_name": "Sanitized command",
                        "summary": "Fast mode should drop non-allowlisted validation command",
                        "dependency_graph": {
                            "feature_name": "Sanitized command",
                            "summary": "Fast mode should drop non-allowlisted validation command",
                            "nodes": [
                                {
                                    "node_id": "node_1",
                                    "title": "N1",
                                    "summary": "N1",
                                    "new_files": ["src/a.py"],
                                    "validation_commands": ["ls -la"],
                                }
                            ],
                        },
                    }
                )
            )
            llm = PromptAwareLLMClient()
            orchestrator = MultiAgentOrchestrator(
                llm_client=llm,
                planner=planner,
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
            )

            result = orchestrator.execute_feature_request(
                requirement="Sanitize command",
                codebase_summary="Summary",
                workspace=workspace,
                fast_mode=True,
            )

            self.assertTrue(result)
            self.assertTrue((workspace / "src" / "a.py").exists())
            self.assertEqual(len(llm.prompts), 1)

    def test_persistent_daemon_cache_reuses_repeated_validation_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            planner = StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="Daemon cache",
                    summary="Reuse repeated validation command",
                    new_files=["module.py"],
                    modified_files=[],
                    steps=[],
                    validation_commands=["python -m py_compile module.py", "python -m py_compile module.py"],
                    design_guidance="",
                )
            )
            llm = QueueLLMClient(responses=["VALUE = 1\n"])
            executor = ScriptedExecutor(
                command_results={
                    "python -m py_compile module.py": [
                        CommandResult(
                            command="python -m py_compile module.py",
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
                enable_persistent_daemons=True,
            )

            result = orchestrator.execute_feature_request(
                requirement="Daemon cache",
                codebase_summary="Summary",
                workspace=workspace,
            )

            self.assertTrue(result)
            self.assertEqual(executor.command_calls, ["python -m py_compile module.py"])

    def test_persistent_validation_daemon_reuses_single_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            orchestrator = MultiAgentOrchestrator(
                llm_client=QueueLLMClient(responses=[]),
                planner=StaticPlanner(plan=ImplementationPlan(feature_name="x", summary="x")),
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                enable_persistent_daemons=True,
            )

            try:
                ok_first, _ = orchestrator._run_validation(
                    ("python -c \"print('first')\"",),
                    workspace,
                )
                self.assertTrue(ok_first)
                self.assertEqual(len(orchestrator._validation_daemons), 1)
                first_pid = next(iter(orchestrator._validation_daemons.values())).process.pid

                ok_second, _ = orchestrator._run_validation(
                    ("python -c \"print('second')\"",),
                    workspace,
                )
                self.assertTrue(ok_second)
                self.assertEqual(len(orchestrator._validation_daemons), 1)
                second_pid = next(iter(orchestrator._validation_daemons.values())).process.pid
                self.assertEqual(first_pid, second_pid)
            finally:
                orchestrator._shutdown_validation_daemons()

    def test_validation_timeout_reaper_marks_command_failed(self) -> None:
        def slow_executor(command: str, workspace: Path) -> CommandResult:
            time.sleep(0.3)
            return CommandResult(command=command, return_code=0, stdout="ok", stderr="")

        orchestrator = MultiAgentOrchestrator(
            llm_client=QueueLLMClient(responses=[]),
            planner=StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="x",
                    summary="x",
                )
            ),
            executor=slow_executor,
            test_writer=NoopTestWriter(),
            dependency_manager=StubDependencyManager(),
            style_mimic=StubStyleMimic(),
        )

        ok, result = orchestrator._run_validation(
            ("python -m pytest",),
            Path(".").resolve(),
            command_timeout_seconds=0.05,
        )

        self.assertFalse(ok)
        assert result is not None
        self.assertEqual(result.return_code, 124)
        self.assertIn("timed out", result.stderr)

    def test_validation_timeout_reaper_hard_kills_shell_process_tree(self) -> None:
        orchestrator = MultiAgentOrchestrator(
            llm_client=QueueLLMClient(responses=[]),
            planner=StaticPlanner(
                plan=ImplementationPlan(
                    feature_name="x",
                    summary="x",
                )
            ),
            test_writer=NoopTestWriter(),
            dependency_manager=StubDependencyManager(),
            style_mimic=StubStyleMimic(),
            enable_persistent_daemons=False,
        )

        command = (
            "python -c \"import subprocess,sys,time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "time.sleep(30)\""
        )
        started = time.monotonic()
        ok, result = orchestrator._run_validation(
            (command,),
            Path(".").resolve(),
            command_timeout_seconds=0.2,
        )
        elapsed = time.monotonic() - started

        self.assertFalse(ok)
        assert result is not None
        self.assertEqual(result.return_code, 124)
        self.assertIn("hard-killed process tree", result.stderr)
        self.assertLess(elapsed, 5.0)

    def test_hitl_conflict_resolution_timeout_returns_failure_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            graph = ImplementationPlan.from_dict(
                {
                    "feature_name": "Conflict",
                    "summary": "Conflict graph",
                    "dependency_graph": {
                        "feature_name": "Conflict",
                        "summary": "Conflict graph",
                        "nodes": [
                            {
                                "node_id": "a",
                                "title": "A",
                                "summary": "A",
                                "modified_files": ["src/shared.py"],
                            },
                            {
                                "node_id": "b",
                                "title": "B",
                                "summary": "B",
                                "modified_files": ["src/shared.py"],
                            },
                        ],
                    },
                }
            ).dependency_graph
            assert graph is not None
            orchestrator = MultiAgentOrchestrator(
                llm_client=QueueLLMClient(responses=[]),
                planner=StaticPlanner(plan=ImplementationPlan(feature_name="x", summary="x")),
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                enable_hitl_conflict_pause=True,
                hitl_conflict_timeout_seconds=0.1,
                hitl_poll_interval_seconds=0.05,
                conflict_resolution_attempts=1,
            )

            original_merge = orchestrator._merge_conflicting_graph_nodes

            def failing_merge(*args, **kwargs):
                raise ValueError("unresolved")

            orchestrator._merge_conflicting_graph_nodes = failing_merge  # type: ignore[assignment]
            try:
                resolved, note = orchestrator._resolve_dependency_graph_conflicts(
                    dependency_graph=graph,
                    workspace_root=workspace,
                )
            finally:
                orchestrator._merge_conflicting_graph_nodes = original_merge  # type: ignore[assignment]

            self.assertIsNone(resolved)
            assert note is not None
            self.assertIn("Manual steering timeout", note)

    def test_hitl_conflict_resolution_uses_manual_retry_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            graph = ImplementationPlan.from_dict(
                {
                    "feature_name": "Conflict",
                    "summary": "Conflict graph",
                    "dependency_graph": {
                        "feature_name": "Conflict",
                        "summary": "Conflict graph",
                        "nodes": [
                            {
                                "node_id": "a",
                                "title": "A",
                                "summary": "A",
                                "modified_files": ["src/shared.py"],
                            },
                            {
                                "node_id": "b",
                                "title": "B",
                                "summary": "B",
                                "modified_files": ["src/shared.py"],
                            },
                        ],
                    },
                }
            ).dependency_graph
            assert graph is not None
            steering_dir = workspace / ".senior_agent"
            steering_dir.mkdir(parents=True, exist_ok=True)
            steering_file = steering_dir / "conflict_resolution.json"
            steering_file.write_text('{"action":"retry"}', encoding="utf-8")

            orchestrator = MultiAgentOrchestrator(
                llm_client=QueueLLMClient(responses=[]),
                planner=StaticPlanner(plan=ImplementationPlan(feature_name="x", summary="x")),
                test_writer=NoopTestWriter(),
                dependency_manager=StubDependencyManager(),
                style_mimic=StubStyleMimic(),
                enable_hitl_conflict_pause=True,
                hitl_conflict_timeout_seconds=0.5,
                hitl_poll_interval_seconds=0.05,
                conflict_resolution_attempts=1,
            )

            original_merge = orchestrator._merge_conflicting_graph_nodes

            def failing_merge(*args, **kwargs):
                raise ValueError("unresolved")

            orchestrator._merge_conflicting_graph_nodes = failing_merge  # type: ignore[assignment]
            try:
                resolved, note = orchestrator._resolve_dependency_graph_conflicts(
                    dependency_graph=graph,
                    workspace_root=workspace,
                )
            finally:
                orchestrator._merge_conflicting_graph_nodes = original_merge  # type: ignore[assignment]

            self.assertIsNotNone(resolved)
            self.assertIsNone(note)

    def test_safe_feature_stem_is_bounded_for_artifact_filenames(self) -> None:
        long_feature = "Feature " + ("very-long-name-" * 40)
        stem = MultiAgentOrchestrator._safe_feature_stem(long_feature)
        self.assertLessEqual(len(stem), 96)
        self.assertTrue(stem)


if __name__ == "__main__":
    unittest.main()
