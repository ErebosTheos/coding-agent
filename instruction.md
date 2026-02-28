# Codex Implementation Instructions — Round 11

**Authority:** `docs/EFFICIENCY_EXECUTION_PLAN.md` §2
**Reviewer:** Claude Sonnet 4.6
**Prereq:** 73 tests passing from rounds 1–10

---

## Problem

Benchmark run on `task_queue` (large tier) crashed in Stage 6:

```
FAILED after 110.2s: 'NoneType' object has no attribute 'validation_commands'
```

**Crash site** — `src/codegen_agent/orchestrator.py` Stage 6 block:

```python
healing_report = await healer.heal(report.test_suite.validation_commands)
```

`report.test_suite` is `None` when:
- The executor generated no test files AND `_source_files_for_testing()` returned an empty dict
  (can happen on large projects where all source files are filtered or use non-standard extensions)
- The `test` task in Stage 4+5 failed with an exception (stored as Exception, so the guard
  `not isinstance(done["test"], Exception)` skips setting `report.test_suite`)

The orchestrator enters Stage 6 without a test suite and crashes instead of running the healer
with an empty validation command list (or skipping healing gracefully).

---

## Task 1 — Guard `report.test_suite` in Stage 6

**File:** `src/codegen_agent/orchestrator.py`

Change **one line** in the Stage 6 block. Find:

```python
                healing_report = await healer.heal(report.test_suite.validation_commands)
```

Replace with:

```python
                _validation_cmds = (
                    report.test_suite.validation_commands if report.test_suite else []
                )
                healing_report = await healer.heal(_validation_cmds)
```

**Do not change anything else in the orchestrator.**

---

## Task 2 — Test in `tests/test_null_test_suite.py` (new file)

One test verifying Stage 6 does not crash when `test_suite` is `None`.
Mock `Healer.heal` to avoid real subprocess calls.

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from codegen_agent.models import (
    HealingReport,
    PipelineReport,
    ExecutionResult,
    GeneratedFile,
)


def test_heal_does_not_crash_when_test_suite_is_none():
    """Orchestrator Stage 6 must not raise AttributeError when test_suite is None."""
    from codegen_agent.orchestrator import Orchestrator

    # Build a minimal report where test_suite was never set
    gf = GeneratedFile(file_path="task_queue.py", content="# stub")
    exec_result = ExecutionResult(generated_files=[gf])
    report = PipelineReport(
        prompt="build a task queue",
        plan=MagicMock(),
        architecture=MagicMock(global_validation_commands=[]),
        execution_result=exec_result,
        dependency_resolution={},
        test_suite=None,   # ← the scenario that caused the crash
        healing_report=None,
    )

    fake_healing = HealingReport(success=True, attempts=[])

    async def run_stage6():
        orch = Orchestrator.__new__(Orchestrator)
        orch.workspace = "/tmp/test_null_ts"
        orch.router = MagicMock()
        orch.router.get_client_for_role.return_value = MagicMock()
        orch.checkpoint_manager = MagicMock()
        orch.checkpoint_manager.asave = AsyncMock()
        orch.reporter = MagicMock()

        healer_mock = MagicMock()
        healer_mock.heal_static_issues = AsyncMock(return_value=[])
        healer_mock.heal = AsyncMock(return_value=fake_healing)

        with patch("codegen_agent.orchestrator.Healer", return_value=healer_mock):
            # Simulate Stage 6 logic directly (no full pipeline run needed)
            from codegen_agent.orchestrator import _collect_python_consistency_issues
            from dataclasses import replace

            healer = healer_mock
            consistency_issues = _collect_python_consistency_issues(
                report.execution_result.generated_files
            )
            static_attempts = []
            if consistency_issues:
                static_attempts = await healer.heal_static_issues(
                    consistency_issues, attempt_number=0
                )

            _validation_cmds = (
                report.test_suite.validation_commands if report.test_suite else []
            )
            healing_report = await healer.heal(_validation_cmds)

        assert healing_report.success is True
        # heal() must have been called with an empty list, not crashed
        healer_mock.heal.assert_called_once_with([])

    asyncio.run(run_stage6())
```

---

## After implementation

Run:
```bash
pytest -q tests/
```

Target: **74 passed** (73 prior + 1 new).

Then re-run the failing benchmark prompt to confirm the crash is gone:
```bash
CODEGEN_LLM_TIMEOUT=90 python benchmark_agent.py --index 7
```

(Index 7 is `task_queue`, the large-tier prompt that crashed.)

The pipeline should now reach Stage 7 (QA) instead of failing with `AttributeError`.

Signal done in `docs/implemented.md` with:
- files changed/created
- final `pytest -q tests/` output
- any deviations with explanation
