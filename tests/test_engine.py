import sys
import tempfile
import unittest
import json
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.engine import SeniorAgent, create_default_senior_agent
from senior_agent.models import (
    CommandResult,
    FailureContext,
    FailureType,
    FileRollback,
    FixOutcome,
)
from senior_agent.strategies import LLMStrategy, RegexReplaceStrategy


class FakeExecutor:
    def __init__(self, results: list[CommandResult]) -> None:
        self._results = results
        self.calls = 0

    def __call__(self, command: str, workspace: Path) -> CommandResult:
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        result = self._results[index]
        return CommandResult(
            command=command,
            return_code=result.return_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )


class ScriptedExecutor:
    def __init__(self, command_results: dict[str, list[CommandResult]]) -> None:
        self.command_results = command_results
        self.command_calls: list[str] = []

    def __call__(self, command: str, workspace: Path) -> CommandResult:
        self.command_calls.append(command)
        results = self.command_results.get(command)
        if not results:
            return CommandResult(command=command, return_code=1, stderr="unconfigured command")
        result = results.pop(0)
        return CommandResult(
            command=command,
            return_code=result.return_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )


@dataclass
class StaticStrategy:
    name: str
    outcome: FixOutcome
    seen_failures: list[FailureType] = field(default_factory=list)

    def apply(self, context: FailureContext) -> FixOutcome:
        self.seen_failures.append(context.failure_type)
        return self.outcome


class SeniorAgentTests(unittest.TestCase):
    def test_returns_success_when_initial_command_passes(self) -> None:
        executor = FakeExecutor([CommandResult("cmd", 0, "ok", "")])
        agent = SeniorAgent(executor=executor)

        report = agent.heal("echo ok", [])

        self.assertTrue(report.success)
        self.assertEqual(len(report.attempts), 0)
        self.assertEqual(executor.calls, 1)
        self.assertIsNone(report.blocked_reason)

    def test_applies_strategy_and_recovers(self) -> None:
        executor = FakeExecutor(
            [
                CommandResult("cmd", 1, "", "assertionerror"),
                CommandResult("cmd", 0, "ok", ""),
            ]
        )
        strategy = StaticStrategy(
            name="fix-one",
            outcome=FixOutcome(
                applied=True,
                note="patched",
                diff_summary=("Modified file.py: +1/-1 lines.",),
            ),
        )
        agent = SeniorAgent(executor=executor)

        report = agent.heal("python -m pytest", [strategy])

        self.assertTrue(report.success)
        self.assertEqual(len(report.attempts), 1)
        self.assertEqual(report.attempts[0].strategy_name, "fix-one")
        self.assertEqual(report.attempts[0].failure_type, FailureType.TEST_FAILURE)
        self.assertEqual(
            report.attempts[0].diff_summary,
            ("Modified file.py: +1/-1 lines.",),
        )
        self.assertEqual(executor.calls, 2)

    def test_continues_when_strategy_raises_exception(self) -> None:
        class ExplodingStrategy:
            name = "explode"

            def apply(self, context: FailureContext) -> FixOutcome:
                raise RuntimeError("boom")

        executor = FakeExecutor(
            [
                CommandResult("cmd", 1, "", "assertionerror"),
                CommandResult("cmd", 0, "ok", ""),
            ]
        )
        fallback = StaticStrategy(
            name="fallback-fix",
            outcome=FixOutcome(applied=True, note="patched"),
        )
        agent = SeniorAgent(executor=executor)

        report = agent.heal("python -m pytest", [ExplodingStrategy(), fallback])

        self.assertTrue(report.success)
        self.assertEqual(len(report.attempts), 2)
        self.assertEqual(report.attempts[0].strategy_name, "explode")
        self.assertFalse(report.attempts[0].applied)
        self.assertIn("raised exception", report.attempts[0].note)
        self.assertEqual(report.attempts[1].strategy_name, "fallback-fix")
        self.assertTrue(report.attempts[1].applied)
        self.assertEqual(executor.calls, 2)

    def test_stops_after_max_attempts_when_not_fixed(self) -> None:
        executor = FakeExecutor([CommandResult("cmd", 1, "", "Traceback")])
        strategies = [
            StaticStrategy("noop-1", FixOutcome(applied=False, note="skip")),
            StaticStrategy("noop-2", FixOutcome(applied=False, note="skip")),
            StaticStrategy("noop-3", FixOutcome(applied=False, note="skip")),
        ]
        agent = SeniorAgent(max_attempts=2, executor=executor)

        report = agent.heal("python app.py", strategies)

        self.assertFalse(report.success)
        self.assertEqual(len(report.attempts), 2)
        self.assertIn("Reached max attempts (2)", report.blocked_reason or "")
        self.assertEqual(executor.calls, 1)

    def test_reports_missing_strategies_when_command_fails(self) -> None:
        executor = FakeExecutor([CommandResult("cmd", 1, "", "Compilation failed")])
        agent = SeniorAgent(executor=executor)

        report = agent.heal("make", [])

        self.assertFalse(report.success)
        self.assertEqual(report.blocked_reason, "No fix strategies configured.")
        self.assertEqual(len(report.attempts), 0)
        self.assertEqual(executor.calls, 1)

    def test_raises_for_invalid_max_attempts(self) -> None:
        with self.assertRaises(ValueError):
            SeniorAgent(max_attempts=0)

    def test_raises_for_invalid_backoff_configuration(self) -> None:
        with self.assertRaises(ValueError):
            SeniorAgent(retry_backoff_base_seconds=-0.1)
        with self.assertRaises(ValueError):
            SeniorAgent(retry_backoff_max_seconds=-1.0)
        with self.assertRaises(ValueError):
            SeniorAgent(retry_backoff_jitter_seconds=-0.1)
        with self.assertRaises(ValueError):
            SeniorAgent(
                retry_backoff_base_seconds=1.0,
                retry_backoff_max_seconds=0.0,
            )
        with self.assertRaises(ValueError):
            SeniorAgent(
                retry_backoff_base_seconds=2.0,
                retry_backoff_max_seconds=1.0,
            )

    def test_applies_exponential_backoff_between_failed_attempts(self) -> None:
        executor = FakeExecutor([CommandResult("cmd", 1, "", "Traceback")])
        strategies = [
            StaticStrategy("noop-1", FixOutcome(applied=False, note="skip")),
            StaticStrategy("noop-2", FixOutcome(applied=False, note="skip")),
            StaticStrategy("noop-3", FixOutcome(applied=False, note="skip")),
        ]
        delays: list[float] = []
        agent = SeniorAgent(
            max_attempts=3,
            executor=executor,
            retry_backoff_base_seconds=0.5,
            retry_backoff_max_seconds=5.0,
            sleep_func=delays.append,
        )

        report = agent.heal("python app.py", strategies)

        self.assertFalse(report.success)
        self.assertEqual(len(delays), 2)
        self.assertAlmostEqual(delays[0], 0.5)
        self.assertAlmostEqual(delays[1], 1.0)

    def test_backoff_jitter_is_capped_by_max_delay(self) -> None:
        executor = FakeExecutor([CommandResult("cmd", 1, "", "Traceback")])
        strategies = [
            StaticStrategy("noop-1", FixOutcome(applied=False, note="skip")),
            StaticStrategy("noop-2", FixOutcome(applied=False, note="skip")),
        ]
        delays: list[float] = []
        agent = SeniorAgent(
            max_attempts=2,
            executor=executor,
            retry_backoff_base_seconds=1.0,
            retry_backoff_max_seconds=1.1,
            retry_backoff_jitter_seconds=0.5,
            random_func=lambda: 1.0,
            sleep_func=delays.append,
        )

        report = agent.heal("python app.py", strategies)

        self.assertFalse(report.success)
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 1.1)

    def test_blocks_out_of_repo_strategy_change(self) -> None:
        executor = FakeExecutor(
            [
                CommandResult("cmd", 1, "", "assertionerror"),
                CommandResult("cmd", 0, "ok", ""),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            outside_file = Path("/tmp/blocked.txt")
            strategy = StaticStrategy(
                name="malicious",
                outcome=FixOutcome(
                    applied=True,
                    note="attempted out-of-repo write",
                    changed_files=(outside_file,),
                ),
            )
            agent = SeniorAgent(executor=executor)

            report = agent.heal("python -m pytest", [strategy], workspace=workspace)

            self.assertFalse(report.success)
            self.assertIn("out-of-repo modification attempt", report.blocked_reason or "")
            self.assertEqual(len(report.attempts), 1)
            self.assertEqual(executor.calls, 1)

    def test_uses_default_strategies_when_none_are_passed(self) -> None:
        executor = FakeExecutor(
            [
                CommandResult("cmd", 1, "", "assertionerror"),
                CommandResult("cmd", 0, "ok", ""),
            ]
        )
        strategy = StaticStrategy(
            name="default-fix",
            outcome=FixOutcome(applied=True, note="patched"),
        )
        agent = SeniorAgent(
            executor=executor,
            default_strategies=(strategy,),
        )

        report = agent.heal("python -m pytest")

        self.assertTrue(report.success)
        self.assertEqual(len(report.attempts), 1)
        self.assertEqual(report.attempts[0].strategy_name, "default-fix")
        self.assertEqual(executor.calls, 2)

    def test_blocks_mutating_strategy_without_rollback_snapshots(self) -> None:
        executor = FakeExecutor([CommandResult("cmd", 1, "", "assertionerror")])
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            strategy = StaticStrategy(
                name="mutate-without-rollback",
                outcome=FixOutcome(
                    applied=True,
                    note="mutated file",
                    changed_files=(workspace / "module.py",),
                ),
            )
            agent = SeniorAgent(executor=executor)

            report = agent.heal("python -m pytest", [strategy], workspace=workspace)

            self.assertFalse(report.success)
            self.assertIn("without rollback snapshots", report.blocked_reason or "")
            self.assertEqual(len(report.attempts), 1)
            self.assertEqual(executor.calls, 1)

    def test_blocks_when_rollback_snapshots_do_not_cover_all_changed_files(self) -> None:
        executor = FakeExecutor([CommandResult("cmd", 1, "", "assertionerror")])
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            changed_one = workspace / "one.py"
            changed_two = workspace / "two.py"
            strategy = StaticStrategy(
                name="partial-rollback",
                outcome=FixOutcome(
                    applied=True,
                    note="mutated files",
                    changed_files=(changed_one, changed_two),
                    rollback_entries=(
                        FileRollback(
                            path=changed_one,
                            existed_before=True,
                            content="x = 1\n",
                        ),
                    ),
                ),
            )
            agent = SeniorAgent(executor=executor)

            report = agent.heal("python -m pytest", [strategy], workspace=workspace)

            self.assertFalse(report.success)
            self.assertIn("missing for changed files", report.blocked_reason or "")
            self.assertEqual(len(report.attempts), 1)
            self.assertEqual(executor.calls, 1)

    def test_create_default_senior_agent_builds_llm_default_strategy(self) -> None:
        agent = create_default_senior_agent(provider="codex", workspace=Path.cwd())

        self.assertEqual(len(agent.default_strategies), 1)
        self.assertIsInstance(agent.default_strategies[0], LLMStrategy)

    def test_create_default_senior_agent_rejects_unknown_provider(self) -> None:
        with self.assertRaises(ValueError):
            create_default_senior_agent(provider="unknown")

    def test_validation_command_runs_after_primary_success(self) -> None:
        executor = ScriptedExecutor(
            {
                "python -m pytest": [CommandResult("python -m pytest", 0, "ok", "")],
                "python -m ruff check .": [
                    CommandResult("python -m ruff check .", 1, "", "lint failure")
                ],
            }
        )
        agent = SeniorAgent(
            executor=executor,
            default_validation_commands=("python -m ruff check .",),
        )

        report = agent.heal("python -m pytest")

        self.assertFalse(report.success)
        self.assertEqual(
            executor.command_calls,
            ["python -m pytest", "python -m ruff check ."],
        )
        self.assertEqual(report.final_result.command, "python -m ruff check .")

    def test_validation_failure_after_fix_causes_session_failure(self) -> None:
        executor = ScriptedExecutor(
            {
                "python -m pytest": [
                    CommandResult("python -m pytest", 1, "", "assertionerror"),
                    CommandResult("python -m pytest", 0, "ok", ""),
                ],
                "python -m ruff check .": [
                    CommandResult("python -m ruff check .", 1, "", "lint failure")
                ],
            }
        )
        strategy = StaticStrategy(
            name="fix-one",
            outcome=FixOutcome(applied=True, note="patched"),
        )
        agent = SeniorAgent(
            max_attempts=1,
            executor=executor,
            default_validation_commands=("python -m ruff check .",),
        )

        report = agent.heal("python -m pytest", [strategy])

        self.assertFalse(report.success)
        self.assertIn("rollback was not possible", report.blocked_reason or "")
        self.assertEqual(
            executor.command_calls,
            ["python -m pytest", "python -m pytest", "python -m ruff check ."],
        )
        self.assertEqual(report.final_result.command, "python -m ruff check .")

    def test_validation_success_after_fix_marks_session_success(self) -> None:
        executor = ScriptedExecutor(
            {
                "python -m pytest": [
                    CommandResult("python -m pytest", 1, "", "assertionerror"),
                    CommandResult("python -m pytest", 0, "ok", ""),
                ],
                "python -m ruff check .": [
                    CommandResult("python -m ruff check .", 0, "lint ok", "")
                ],
            }
        )
        strategy = StaticStrategy(
            name="fix-one",
            outcome=FixOutcome(applied=True, note="patched"),
        )
        agent = SeniorAgent(
            max_attempts=1,
            executor=executor,
            default_validation_commands=("python -m ruff check .",),
        )

        report = agent.heal("python -m pytest", [strategy])

        self.assertTrue(report.success)
        self.assertEqual(report.final_result.command, "python -m ruff check .")

    def test_rolls_back_files_after_failed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            target_file = workspace / "module.py"
            original_text = "value = 'original'\n"
            target_file.write_text(original_text, encoding="utf-8")

            class MutatingStrategy:
                name = "mutating"

                def apply(self, context: FailureContext) -> FixOutcome:
                    changed_text = "value = 'broken'\n"
                    target_file.write_text(changed_text, encoding="utf-8")
                    return FixOutcome(
                        applied=True,
                        note="changed target file",
                        changed_files=(target_file,),
                        rollback_entries=(
                            FileRollback(
                                path=target_file,
                                existed_before=True,
                                content=original_text,
                            ),
                        ),
                    )

            executor = ScriptedExecutor(
                {
                    "python -m pytest": [
                        CommandResult("python -m pytest", 1, "", "initial failure"),
                        CommandResult("python -m pytest", 0, "ok", ""),
                        CommandResult("python -m pytest", 1, "", "initial failure"),
                    ],
                    "python -m ruff check .": [
                        CommandResult("python -m ruff check .", 1, "", "lint failure")
                    ],
                }
            )
            agent = SeniorAgent(
                max_attempts=1,
                executor=executor,
                default_validation_commands=("python -m ruff check .",),
            )

            report = agent.heal("python -m pytest", [MutatingStrategy()], workspace=workspace)

            self.assertFalse(report.success)
            self.assertIn("Reached max attempts (1)", report.blocked_reason or "")
            self.assertIn("Rollback restored 1 file(s).", report.attempts[0].note)
            self.assertEqual(target_file.read_text(encoding="utf-8"), original_text)

    def test_blocks_when_rollback_path_is_outside_workspace(self) -> None:
        executor = ScriptedExecutor(
            {
                "python -m pytest": [
                    CommandResult("python -m pytest", 1, "", "initial failure"),
                    CommandResult("python -m pytest", 0, "ok", ""),
                ],
                "python -m ruff check .": [
                    CommandResult("python -m ruff check .", 1, "", "lint failure")
                ],
            }
        )
        outside = Path("/tmp/outside_rollback.py")
        strategy = StaticStrategy(
            name="bad-rollback",
            outcome=FixOutcome(
                applied=True,
                note="changed",
                changed_files=(),
                rollback_entries=(
                    FileRollback(path=outside, existed_before=True, content="x = 1\n"),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            agent = SeniorAgent(
                max_attempts=1,
                executor=executor,
                default_validation_commands=("python -m ruff check .",),
            )
            report = agent.heal("python -m pytest", [strategy], workspace=workspace)

            self.assertFalse(report.success)
            self.assertIn("outside workspace", report.blocked_reason or "")

    def test_writes_checkpoint_file_for_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / ".state" / "session.json"
            executor = FakeExecutor([CommandResult("cmd", 1, "", "Traceback")])
            strategy = StaticStrategy("noop", FixOutcome(applied=False, note="skip"))
            agent = SeniorAgent(max_attempts=1, executor=executor)

            report = agent.heal(
                "python app.py",
                [strategy],
                workspace=workspace,
                checkpoint_path=checkpoint_file,
            )

            self.assertFalse(report.success)
            self.assertTrue(checkpoint_file.exists())
            persisted, metadata = SeniorAgent._load_checkpoint(checkpoint_file)
            self.assertEqual(persisted.command, report.command)
            self.assertEqual(persisted.blocked_reason, report.blocked_reason)
            self.assertEqual(len(persisted.attempts), 1)
            self.assertIsNotNone(metadata)
            self.assertEqual(metadata["schema_version"], 1)
            self.assertEqual(metadata["workspace"], str(workspace.resolve()))
            self.assertIn("validation_fingerprint", metadata)

    def test_resume_continues_from_checkpointed_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / ".state" / "session.json"
            executor = ScriptedExecutor(
                {
                    "python -m pytest": [
                        CommandResult("python -m pytest", 1, "", "assertionerror"),
                        CommandResult("python -m pytest", 0, "ok", ""),
                    ]
                }
            )
            first_strategy = StaticStrategy(
                name="first-noop",
                outcome=FixOutcome(applied=False, note="skip"),
            )
            second_strategy = StaticStrategy(
                name="second-fix",
                outcome=FixOutcome(applied=True, note="patched"),
            )
            first_agent = SeniorAgent(max_attempts=1, executor=executor)

            first_report = first_agent.heal(
                "python -m pytest",
                [first_strategy, second_strategy],
                workspace=workspace,
                checkpoint_path=checkpoint_file,
            )
            self.assertFalse(first_report.success)
            self.assertEqual(len(first_report.attempts), 1)

            resume_agent = SeniorAgent(max_attempts=2, executor=executor)
            resumed_report = resume_agent.resume(
                checkpoint_path=checkpoint_file,
                strategies=[first_strategy, second_strategy],
                workspace=workspace,
            )

            self.assertTrue(resumed_report.success)
            self.assertEqual(len(resumed_report.attempts), 2)
            self.assertEqual(resumed_report.attempts[1].strategy_name, "second-fix")
            persisted, _ = SeniorAgent._load_checkpoint(checkpoint_file)
            self.assertTrue(persisted.success)
            self.assertEqual(len(persisted.attempts), 2)

    def test_resume_returns_completed_success_report_without_reexecuting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / "session.json"

            seed_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            seed_report = seed_agent.heal(
                "python -m pytest",
                workspace=workspace,
                checkpoint_path=checkpoint_file,
            )
            self.assertTrue(seed_report.success)

            class RaisingExecutor:
                def __call__(self, command: str, workspace_path: Path) -> CommandResult:
                    raise AssertionError("resume should not execute commands on success")

            agent = SeniorAgent(executor=RaisingExecutor())
            report = agent.resume(checkpoint_path=checkpoint_file, workspace=workspace)

            self.assertTrue(report.success)
            self.assertEqual(report.command, "python -m pytest")

    def test_resume_rejects_workspace_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / "session.json"
            seed_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            seed_agent.heal(
                "python -m pytest",
                workspace=workspace,
                checkpoint_path=checkpoint_file,
            )

            payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            payload["workspace"] = "/tmp/other-workspace"
            checkpoint_file.write_text(json.dumps(payload), encoding="utf-8")

            resume_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            with self.assertRaisesRegex(ValueError, "workspace mismatch"):
                resume_agent.resume(checkpoint_path=checkpoint_file, workspace=workspace)

    def test_resume_rejects_strategy_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / "session.json"
            seed_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            seed_agent.heal(
                "python -m pytest",
                workspace=workspace,
                checkpoint_path=checkpoint_file,
            )

            resume_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            with self.assertRaisesRegex(ValueError, "strategy fingerprint mismatch"):
                resume_agent.resume(
                    checkpoint_path=checkpoint_file,
                    strategies=[
                        StaticStrategy(
                            name="extra-strategy",
                            outcome=FixOutcome(applied=False, note="skip"),
                        )
                    ],
                    workspace=workspace,
                )

    def test_resume_rejects_strategy_config_mismatch_for_same_strategy_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / "session.json"
            seed_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            seed_strategy = RegexReplaceStrategy(
                name="cfg",
                target_file="sample.py",
                pattern=r"buggy",
                replacement="fixed",
            )
            seed_agent.heal(
                "python -m pytest",
                strategies=[seed_strategy],
                workspace=workspace,
                checkpoint_path=checkpoint_file,
            )

            resume_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            changed_strategy = RegexReplaceStrategy(
                name="cfg",
                target_file="sample.py",
                pattern=r"buggy",
                replacement="patched",
            )
            with self.assertRaisesRegex(ValueError, "strategy fingerprint mismatch"):
                resume_agent.resume(
                    checkpoint_path=checkpoint_file,
                    strategies=[changed_strategy],
                    workspace=workspace,
                )

    def test_resume_rejects_validation_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / "session.json"
            seed_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            seed_agent.heal(
                "python -m pytest",
                workspace=workspace,
                checkpoint_path=checkpoint_file,
            )

            resume_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            with self.assertRaisesRegex(ValueError, "validation fingerprint mismatch"):
                resume_agent.resume(
                    checkpoint_path=checkpoint_file,
                    workspace=workspace,
                    validation_commands=("python -m ruff check .",),
                )

    def test_resume_rejects_legacy_checkpoint_without_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            checkpoint_file = workspace / "legacy.json"
            legacy_payload = {
                "command": "python -m pytest",
                "initial_result": {
                    "command": "python -m pytest",
                    "return_code": 1,
                    "stdout": "",
                    "stderr": "initial failure",
                },
                "final_result": {
                    "command": "python -m pytest",
                    "return_code": 1,
                    "stdout": "",
                    "stderr": "initial failure",
                },
                "attempts": [],
                "success": False,
                "blocked_reason": None,
            }
            checkpoint_file.write_text(json.dumps(legacy_payload), encoding="utf-8")

            resume_agent = SeniorAgent(
                executor=FakeExecutor([CommandResult("python -m pytest", 0, "ok", "")])
            )
            with self.assertRaisesRegex(ValueError, "missing compatibility metadata"):
                resume_agent.resume(checkpoint_path=checkpoint_file, workspace=workspace)


if __name__ == "__main__":
    unittest.main()
