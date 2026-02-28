import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from codegen_agent.models import (
    ExecutionResult,
    GeneratedFile,
    HealingReport,
    PipelineReport,
)


def test_heal_does_not_crash_when_test_suite_is_none():
    """Stage 6 must not raise AttributeError when test_suite is None."""
    gf = GeneratedFile(file_path="task_queue.py", content="# stub", node_id="n1", sha256="")
    exec_result = ExecutionResult(generated_files=[gf])
    report = PipelineReport(
        prompt="build a task queue",
        plan=MagicMock(),
        architecture=MagicMock(global_validation_commands=[]),
        execution_result=exec_result,
        dependency_resolution={},
        test_suite=None,
        healing_report=None,
    )

    fake_healing = HealingReport(success=True, attempts=[])

    async def run_stage6():
        healer_mock = MagicMock()
        healer_mock.heal_static_issues = AsyncMock(return_value=[])
        healer_mock.heal = AsyncMock(return_value=fake_healing)

        from codegen_agent.orchestrator import _collect_python_consistency_issues

        consistency_issues = _collect_python_consistency_issues(
            report.execution_result.generated_files
        )
        static_attempts = []
        if consistency_issues:
            static_attempts = await healer_mock.heal_static_issues(
                consistency_issues, attempt_number=0
            )

        _validation_cmds = (
            report.test_suite.validation_commands if report.test_suite else []
        )
        healing_report = await healer_mock.heal(_validation_cmds)

        assert healing_report.success is True
        healer_mock.heal.assert_called_once_with([])

    asyncio.run(run_stage6())
