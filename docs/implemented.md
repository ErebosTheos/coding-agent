# Implementation Report (instruction.md)

---

## Session — Bulk+Stream Execution + Import/Dependency Fixes

**Date:** 2026-02-27

### Files Changed
- `src/codegen_agent/executor.py` (added `_BulkFileParser`, `Executor._stream_bulk()`)
- `src/codegen_agent/stream_executor.py` (simplified `StreamingPlanArchExecutor.run()`)
- `src/codegen_agent/dependency_manager.py` (extended `_ensure_conftest`, added `_install_inferred_frameworks`)
- `src/codegen_agent/orchestrator.py` (null-guard for `report.test_suite` in Stage 6)
- `benchmark_agent.py` (restored 8-prompt suite)
- `.env` (switched to `CODEGEN_PROVIDER=gemini` for testing)
- `tests/test_stream_executor_parallel.py` (updated for new bulk+stream behavior)
- `tests/test_null_test_suite.py` (new)

### What Was Implemented

#### 1. Bulk+Stream Execution (`executor.py`, `stream_executor.py`)

**Problem:** `StreamingPlanArchExecutor` dispatched per-node LLM calls during the arch
stream. Each node had only partial context → cross-file import mismatches → healing needed
every run (adding 200-600s with Gemini).

**Fix:** Two-phase approach:
- **Phase 1:** Stream plan+arch as before (buffers full response, no node dispatch)
- **Phase 2:** One bulk LLM call for all files, response also streamed → files written
  as each JSON value completes in the stream

New `_BulkFileParser` (state-machine JSON parser):
- Streams `{"file_path": "content", ...}` character-by-character
- Handles all JSON escape sequences (`\n`, `\t`, `\\`, `\"`)
- Yields `(file_path, content)` pairs as each value string completes

New `Executor._stream_bulk(architecture)`:
- Sends one bulk prompt for all files
- Calls `llm_client.astream()`, feeds chunks to `_BulkFileParser`
- Writes each file immediately as it arrives
- Falls back to wave-based for any files missing from the stream
- Falls back to `_execute_bulk()` (non-streaming) if client has no `astream()`

`StreamingPlanArchExecutor.run()` simplified from ~80 lines to ~35:
- Removed: `_NodeParser`, `_dispatch`, `seen_nodes`, `dispatched`, `semaphore`
- Now: stream arch → parse → call `executor._stream_bulk(architecture)`

**Result:** No more static consistency issues → no healing needed → faster end-to-end.

#### 2. `src/` Layout Conftest Injection (`dependency_manager.py`)

**Problem:** Projects with `src/` layout (files in `src/`, tests in `tests/`) failed
`pytest tests/` with `ModuleNotFoundError` because workspace root wasn't on `sys.path`.
`_ensure_conftest` only triggered for root-level Python files.

**Fix:** Added `has_src_layout` check — when `src/` dir exists with Python files,
write a root `conftest.py` that adds both workspace root AND `src/` to `sys.path`.

#### 3. Framework Auto-Install (`dependency_manager.py`)

**Problem:** When the executor generates a project with no `requirements.txt` or
`pyproject.toml`, frameworks like `fastapi`, `sqlalchemy`, `httpx` aren't installed →
`ModuleNotFoundError` at test time.

**Fix:** `_install_inferred_frameworks()` — runs when no manifest is present:
- Scans all generated `.py` files for import statements
- Matches top-level module names against `_FRAMEWORK_IMPORT_MAP` (16 common packages)
- Installs all recognized-but-missing packages in one `pip install` call

Packages covered: `fastapi[standard]`, `uvicorn`, `flask`, `django`, `sqlalchemy`,
`alembic`, `pydantic`, `httpx`, `aiohttp`, `celery`, `redis`, `pymongo`, `motor`,
`boto3`, `requests`, `starlette`.

#### 4. Null Test Suite Guard (`orchestrator.py`)

**Problem:** Stage 6 crashed with `AttributeError: 'NoneType' object has no attribute
'validation_commands'` when `report.test_suite` was None (large projects where TestWriter
task failed or produced no output).

**Fix:** One-line null guard before `healer.heal()` call:
```python
_validation_cmds = (
    report.test_suite.validation_commands if report.test_suite else []
)
healing_report = await healer.heal(_validation_cmds)
```

### Validation

```bash
pytest -q tests/
```

```
77 passed in 1.14s
```

### Benchmark Result (Gemini, task_queue)

After fixes:
```
[DependencyManager] Installing inferred frameworks: ['sqlalchemy', 'pydantic', 'fastapi[standard]']
[DependencyManager] Installed: ['sqlalchemy', 'pydantic', 'fastapi[standard]']
[DependencyManager] Wrote conftest.py to make root modules importable.
[StreamExecutor] Plan+Arch complete. 5 node(s). Executing (stream-bulk)...
```

No `ModuleNotFoundError`. No static consistency healing. Pipeline reaches QA.

---

**Date:** 2026-02-27  
**Source instruction:** `instruction.md`  
**Scope:** Tasks 1–5 (code changes + validation results)

## TL;DR

| Area | Status | Evidence |
|---|---|---|
| Core pipeline upgrades (traces, retry/fallback, prune, health CLI) | Done | Implemented in `models.py`, `router.py`, `utils.py`, `healer.py`, `main.py` |
| Heal stability fixes | Done | Deterministic `ruff --fix` + pytest import-path conftest bootstrap |
| QA auditing reliability | Done | Evidence-grounded QA summary + contradiction filtering + normalization |
| Unit/integration tests | Passing | `pytest -q tests/` -> `69 passed` |
| Benchmark smoke checks | Passing | `stack_class` -> `qa=97 PASS`, `prime_checker` -> `qa=93 PASS` |
| Documentation | Updated | Full execution log and outcomes captured in this file |

## Task 1 — `StageTrace` + `PipelineReport.stage_traces`
**File updated:** `src/codegen_agent/models.py`

Implemented:
- Added `StageTrace` dataclass exactly as specified.
- Added `stage_traces: List["StageTrace"] = field(default_factory=list)` to `PipelineReport`.

Result:
- `PipelineReport(prompt="test").stage_traces == []` verified.

## Task 2 — Router retry/fallback wrapper
**File updated:** `src/codegen_agent/llm/router.py`

Implemented:
- Added imports: `asyncio`, `random`, `LLMTimeoutError`, `LLMError`.
- Added `_get_fallback_client(role)` using:
  - `CODEGEN_<ROLE>_FALLBACK_PROVIDER`
  - `CODEGEN_<ROLE>_FALLBACK_MODEL`
- Added `execute_with_retry(role, prompt, system_prompt="") -> tuple[str, int, bool, Optional[str]]`.

Behavior implemented:
- Retries transient failures (timeouts + empty-output errors) with jittered exponential backoff.
- Tries fallback client once after primary retries are exhausted.
- Returns `(response, retries_used, fallback_used, fallback_reason)`.

Result:
- Acceptance-style async mock test passed (`retries == 2`, `fallback_used == False`, `response == "ok"`).

## Task 3 — `prune_prompt()` in `utils.py`
**File updated:** `src/codegen_agent/utils.py`

Implemented:
- Added `prune_prompt(prompt: str, max_chars: int = 32_000) -> str`.
- Implements:
  - soft trim for `<<SOURCE>>` / `<<FILE>>`
  - hard clear for `<<HISTORY_START>>...<<HISTORY_END>>`
  - tail-preserving truncation if still over limit

Result:
- Acceptance snippet passed (`len(result) <= max_chars`, signature retained, no-op for short prompts).

## Task 4 — Heal command consolidation
**File updated:** `src/codegen_agent/healer.py`

Implemented:
- Added `_consolidate_commands(commands)` before `Healer` class.
- Updated `heal()` loop:
  - runs consolidated commands first
  - if consolidated form fails, falls back to original per-command execution for that iteration

Result:
- Consolidation behavior validated with snippet:
  - all-pytest list collapses to `["pytest -q -x"]`
  - mixed command list remains unchanged
  - empty list unchanged

Note:
- The acceptance example in `instruction.md` has a typo (`"pytest test"`). Implementation keeps the original command unchanged (`"pytest"`), which is correct.

## Task 5 — `health` subcommand in CLI
**File updated:** `src/codegen_agent/main.py`

Implemented:
- Converted parser to subcommands:
  - `run` (existing pipeline behavior)
  - `health` (new environment/provider readiness check)
- Added `_run_health_check()` with checks for:
  - binaries: `claude`, `gemini`, `codex`
  - `.env` presence
  - provider mapping for all roles via `LLMRouter`
  - workspace writability
- Exit behavior:
  - `0` when all checks pass
  - `1` if any issues are found

Result:
- Health command executed successfully and returned pass status.
- Added `src/codegen_agent/__main__.py` so `python -m codegen_agent ...` works directly.

## Validation Results

Commands run:

```bash
python -m py_compile src/codegen_agent/models.py src/codegen_agent/llm/router.py src/codegen_agent/utils.py src/codegen_agent/healer.py src/codegen_agent/main.py
```

```bash
source .venv/bin/activate && python -m codegen_agent health
```

```bash
source .venv/bin/activate && pytest -q tests/
```

```bash
# acceptance snippets
source .venv/bin/activate && python <Task1 snippet>
source .venv/bin/activate && python <Task2 snippet>
source .venv/bin/activate && python <Task3 snippet>
source .venv/bin/activate && python <Task4 snippet>
```

Observed outcomes:
- `py_compile`: pass
- `health`: pass (all checks reported OK in current environment)
- `pytest -q tests/`: `24 passed in 0.05s`
- acceptance snippets: all pass

---

## Round 2 (Codex Implementation Instructions — Round 2)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 2)

### Files Changed
- `src/codegen_agent/orchestrator.py`
- `src/codegen_agent/reporter.py`
- `tests/test_stage_traces.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **StageTrace collection in orchestrator**
- Added `StageTrace` import and `_role_provider(router, role)` helper.
- Initialized `traces` list from checkpointed `report.stage_traces`.
- Added stage timing traces for:
  - `plan_arch_exec` (stream path + resume execute-only path)
  - `deps`
  - `tests`
  - `heal`
  - `qa`
  - `visual`
- Final `replace()` now sets both `wall_clock_seconds` and `stage_traces`.

2. **Reporter trace log output**
- `Reporter.save_report()` now appends each stage trace as one JSON line to:
  - `.codegen_agent/traces.jsonl`
- Existing JSON/Mermaid/Markdown outputs were left unchanged.

3. **New tests**
- Added `tests/test_stage_traces.py` with four tests:
  - StageTrace presence in `PipelineReport.to_dict()`
  - default empty `stage_traces`
  - `_role_provider()` behavior for known and unknown roles
  - `Reporter.save_report()` writes `traces.jsonl` correctly

### Validation

Commands run:

```bash
source .venv/bin/activate && pytest -q tests/test_stage_traces.py
```

Output:

```text
....                                                                     [100%]
4 passed in 0.06s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
............................                                             [100%]
28 passed in 0.06s
```

### Deviations From Spec
- None.

---

## Post-Round Update — Ctrl+C / Cancellation Stability

**Date:** 2026-02-27  
**Context:** Tracebacks showed `asyncio.exceptions.CancelledError` during stream reads, followed by `KeyboardInterrupt` at `asyncio.run(...)`.

### Files Changed
- `src/codegen_agent/llm/codex_cli.py`
- `benchmark_agent.py`
- `src/codegen_agent/main.py`
- `tests/test_codex_cli.py`
- `docs/implemented.md` (this update)

### What Was Implemented

1. **Graceful cancellation handling in Codex client**
- Added explicit `except asyncio.CancelledError` handling in:
  - `CodexCLIClient.generate()`
  - `CodexCLIClient.astream()`
- On cancellation, child `codex` subprocess is terminated via `_terminate_process(...)` and cancellation is re-raised.
- Hardened `_terminate_process(...)` to safely no-op when process is already finished.

2. **Cleaner CLI interrupt behavior**
- `benchmark_agent.py` now wraps `asyncio.run(main())` in `try/except KeyboardInterrupt`:
  - prints `Benchmark interrupted by user.`
  - exits with code `130`
- `src/codegen_agent/main.py` does the same:
  - prints `Interrupted by user.`
  - exits with code `130`

### Validation

Targeted tests:

```bash
pytest -q tests/test_codex_cli.py tests/test_perf_flags.py tests/test_stream_executor_parallel.py
```

Output:

```text
..........                                                               [100%]
10 passed in 1.23s
```

Full suite:

```bash
pytest -q tests/
```

Output:

```text
........................................................................ [ 98%]
.                                                                        [100%]
73 passed in 1.29s
```

### Deviations From Spec
- None.

---

## Post-Round Update — Parallel Streaming Execution + Codex Stream Timeout

**Date:** 2026-02-27  
**Context:** Follow-up fixes after benchmark stalls and serialized execution behavior.

### Files Changed
- `src/codegen_agent/llm/codex_cli.py`
- `src/codegen_agent/stream_executor.py`
- `src/codegen_agent/executor.py`
- `tests/test_stream_executor_parallel.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **Codex streaming timeout behavior fixed**
- Reworked `CodexCLIClient.astream()` timeout model:
  - added idle-timeout semantics (timer resets on stream activity)
  - added hard max timeout as safety cap
- Added helpers:
  - `_stream_timeouts_from_env()`
  - `_terminate_process()`
- New env controls:
  - `CODEGEN_LLM_STREAM_IDLE_TIMEOUT` (defaults to `CODEGEN_LLM_TIMEOUT`)
  - `CODEGEN_LLM_STREAM_MAX_TIMEOUT` (defaults to `max(idle*6, 600)`)

2. **Streaming executor now runs in parallel by default**
- In `StreamingPlanArchExecutor`, node dispatch no longer waits for dependency dispatch order by default.
- New toggle:
  - `CODEGEN_STREAM_RESPECT_DEPENDENCIES=1` restores strict dependency waiting.
- Default behavior (`0`) maximizes throughput and dispatches nodes immediately as they stream in.

3. **Executor parallelism is now tunable from env**
- Added env-based overrides in `Executor.__init__`:
  - `CODEGEN_EXECUTOR_CONCURRENCY`
  - `CODEGEN_EXECUTOR_MAX_BULK_FILES`
- These apply when explicit constructor values are not provided.

4. **Regression/perf tests for stream parallelism**
- Added `tests/test_stream_executor_parallel.py`:
  - verifies default parallel behavior
  - verifies opt-in strict dependency behavior via env flag

### Validation

Targeted suites:

```bash
pytest -q tests/test_stream_executor_parallel.py tests/test_codex_cli.py tests/test_round8.py
```

Output:

```text
........                                                                 [100%]
8 passed in 0.20s
```

```bash
pytest -q tests/test_healer.py tests/test_qa_auditor.py tests/test_conftest_injection.py
```

Output:

```text
...............                                                          [100%]
15 passed in 0.05s
```

Full suite:

```bash
pytest -q tests/
```

Output:

```text
.......................................................................  [100%]
71 passed in 1.26s
```

### Runtime Verification Notes
- Re-ran benchmark pipeline and confirmed:
  - stream stage no longer immediately fails at 90s while active
  - stream executor dispatches many nodes concurrently
  - observed multiple concurrent `codex exec` workers during large run

### Recommended Run Command (High Parallelism)

```bash
source .venv/bin/activate && \
CODEGEN_EXECUTOR_CONCURRENCY=12 \
CODEGEN_STREAM_RESPECT_DEPENDENCIES=0 \
CODEGEN_LLM_TIMEOUT=90 \
python -u benchmark_agent.py --max-heals 0
```

### Deviations From Spec
- None.

## Post-Round Fix — Healing + QA Auditing Stability

### Files Updated
- `src/codegen_agent/dependency_manager.py`
- `src/codegen_agent/orchestrator.py`
- `src/codegen_agent/healer.py`
- `src/codegen_agent/qa_auditor.py`
- `tests/test_conftest_injection.py`
- `tests/test_healer.py`
- `tests/test_qa_auditor.py`

### What Was Fixed
1. **Conftest race between Stage 4 and Stage 5**
- `DependencyManager._ensure_conftest(...)` now accepts `extra_test_paths` and can also detect tests from filesystem.
- `Orchestrator` now re-runs conftest bootstrap after test generation (Stage 4+5 join), eliminating misses when tests are created by TestWriter after dependency step starts.
- Injected conftest content is lint-clean and idempotent (checks before `sys.path.insert`).

2. **Healing stalls on lint-only failures**
- Added deterministic lint autofix in healer:
  - If failing command is `ruff check ...`, healer runs `ruff check --fix ...` first.
  - Avoids expensive LLM call for import-order/style-only failures.
- Added deterministic import-path healing:
  - For pytest `ModuleNotFoundError: No module named '<root_module>'`, healer writes a minimal `conftest.py` when `<root_module>.py` exists at workspace root.

3. **QA false negatives / hallucinated failures**
- `QAAuditor` now receives workspace context and builds an evidence snapshot:
  - known file inventory
  - workspace file sample
  - dependency-resolution summary
  - healing/validation evidence (including last failed command tail, if any)
- Added normalization + contradiction guardrails:
  - Converts non-string issue/suggestion objects into strings.
  - Filters issues that contradict evidence (for example “missing file” when file exists).
  - Auto-approves when issues are empty and healing evidence is clean.

### Tests Added/Updated
- `tests/test_conftest_injection.py`
  - Added coverage for delayed test-path detection (`extra_test_paths`).
- `tests/test_healer.py`
  - Added coverage for deterministic `conftest.py` import-path fix.
  - Added coverage for `ruff check --fix` autofix path (no LLM call).
- `tests/test_qa_auditor.py`
  - Added coverage for filtering missing-file hallucination.
  - Added coverage for issue/suggestion object normalization.

### Validation

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
.....................................................................    [100%]
69 passed in 1.11s
```

```bash
source .venv/bin/activate && CODEGEN_LLM_TIMEOUT=90 python -u benchmark_agent.py --index 1 --max-heals 3
```

Result:

```text
[SMALL] stack_class
...
Stage 6: Healing...
Stage 7: QA Auditing...
Done in 59.3s | heals=0 | qa=97 | PASS
```

```bash
source .venv/bin/activate && CODEGEN_LLM_TIMEOUT=90 python -u benchmark_agent.py --index 0 --max-heals 3
```

Result:

```text
[SMALL] prime_checker
...
Stage 6: Healing...
Stage 7: QA Auditing...
Done in 57.9s | heals=0 | qa=93 | PASS
```

### Deviations From Spec
- None.

### Complete Execution Log (This Round)

#### 1) Diagnostics performed before editing

- Confirmed failing/hallucinated QA patterns from prior benchmark artifacts:
  - `benchmark_output/prime_checker/.codegen_agent/pipeline_report.json`
  - `benchmark_output/stack_class/.codegen_agent/pipeline_report.json`
- Verified root causes:
  - `conftest.py` content triggered lint sensitivity (`ruff` import-order style issue in generated projects).
  - Stage 4/5 race could miss conftest injection when tests were only known after TestWriter.
  - QA reported contradictory file-missing issues despite files existing in workspace.
  - Heal loop could spend time in LLM for deterministic lint/import-path failures.

#### 2) Exact implementation details added

1. **DependencyManager (`_ensure_conftest`)**
- Signature extended:
  - `extra_test_paths: Optional[List[str]] = None`
- Detection enhanced:
  - checks generated files
  - checks `extra_test_paths`
  - checks `workspace/tests/**/*.py` fallback
- Injected `conftest.py` updated to:
  - import `os` then `sys`
  - set `ROOT = ...`
  - only insert into `sys.path` if missing

2. **Orchestrator post-join guard**
- After Stage 4+5 `gather(...)`:
  - re-runs `DependencyManager._ensure_conftest(...)` using `report.test_suite.test_files.keys()`
  - records `conftest_injected_post_tests=True` in dependency payload when applied

3. **Healer deterministic pre-LLM fixes**
- Added `_apply_known_auto_fixes(...)`:
  - if command matches `ruff check ...` and not already `--fix`, run `ruff check --fix ...`
- Added `_fix_pytest_import_path_if_needed(...)`:
  - if pytest failure contains `ModuleNotFoundError: No module named '<x>'`
  - and `<x>.py` exists at workspace root
  - writes `conftest.py` immediately (no LLM call)
- Hooked both into `_fix_single_failure(...)` before classifier/target-file LLM path.

4. **QA auditor grounding + normalization**
- `QAAuditor` now initialized with workspace:
  - `QAAuditor(llm_client, workspace)`
- Added evidence payload to prompt summary:
  - known files
  - workspace file sample
  - dependency-resolution summary
  - validation/healing evidence (`last_failed_command` tails)
- Added normalization/filtering:
  - object issues/suggestions -> strings
  - drops contradictory “missing file” issues if file is in known evidence
  - drops contradictory command-failure issues when healing shows success + no failed command
  - clamps score to `[0, 100]`

#### 3) Test updates and results

```bash
pytest -q tests/test_conftest_injection.py tests/test_healer.py tests/test_qa_auditor.py
```

Output:

```text
...............                                                          [100%]
15 passed in 0.06s
```

```bash
pytest -q tests/
```

Output:

```text
.....................................................................    [100%]
69 passed in 1.11s
```

#### 4) Benchmark verification and artifact checks

```bash
source .venv/bin/activate && CODEGEN_LLM_TIMEOUT=90 python -u benchmark_agent.py --index 1 --max-heals 3
```

Observed:

```text
[SMALL] stack_class
...
Stage 6: Healing...
Stage 7: QA Auditing...
Done in 59.3s | heals=0 | qa=97 | PASS
```

```bash
source .venv/bin/activate && CODEGEN_LLM_TIMEOUT=90 python -u benchmark_agent.py --index 0 --max-heals 3
```

Observed:

```text
[SMALL] prime_checker
...
Stage 6: Healing...
Stage 7: QA Auditing...
Done in 57.9s | heals=0 | qa=93 | PASS
```

Report files confirmed present after run:

```text
benchmark_output/prime_checker/.codegen_agent/
  checkpoint.json
  pipeline_report.json
  report_summary.md
  runs.jsonl
  traces.jsonl
```

Pipeline report evidence snapshot:
- `stack_class`:
  - `healing_report.success = true`
  - `qa_report.score = 97`, `approved = true`, `issues = []`
- `prime_checker`:
  - `healing_report.success = true`
  - `qa_report.score = 93`, `approved = true`, `issues = []`

#### 5) Additional operational note

- A stale earlier benchmark process (`benchmark_agent.py --index 0 --max-heals 3`) was terminated and rerun to ensure results reflect the new code path only.
- Final documented benchmark numbers above are from the clean rerun.

---

## Round 9 (Codex Implementation Instructions — Round 9)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 9)

### Files Changed / Created
- `src/codegen_agent/dependency_manager.py` (updated)
- `src/codegen_agent/orchestrator.py` (updated)
- `tests/test_dep_tools.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **Validation-tool whitelist for auto-install**
- Added module-level whitelist in `dependency_manager.py`:
  - `_VALIDATION_TOOL_WHITELIST` with safe Python dev tools (`ruff`, `black`, `mypy`, etc.).

2. **DependencyManager signature update**
- Updated `resolve_and_install(...)` to accept:
  - `validation_commands: List[str] | None = None`

3. **Auto-install missing validation tools**
- Added tool-install phase after manifest installs:
  - parses first token from each validation command
  - only considers whitelisted tools
  - checks presence with `shutil.which`
  - installs missing tool via:
    - `python -m pip install <tool>`
  - records failures in `results["errors"]`
- This phase runs even if there are no manifest installs.

4. **Orchestrator wiring**
- Stage 4 dependency task now passes architect commands:
  - `validation_commands=report.architecture.global_validation_commands or []`

5. **Round 9 tests**
- Added `tests/test_dep_tools.py` with 3 tests:
  - missing whitelisted tool triggers install call
  - non-whitelisted tool does not trigger install
  - already-installed tool is skipped

### Validation

```bash
source .venv/bin/activate && pytest -q tests/test_dep_tools.py
```

Output:

```text
...                                                                      [100%]
3 passed in 0.04s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
.............................................................            [100%]
61 passed in 1.13s
```

Benchmark verification command:

```bash
source .venv/bin/activate && CODEGEN_LLM_TIMEOUT=90 python -u benchmark_agent.py --index 0
```

Observed output (summary):

```text
Done in 129.1s | heals=0 | qa=68 | FAIL
```

### Deviations From Spec
- The expected line:
  - `[DependencyManager] Installing missing validation tool: ruff`
  did not appear in this `--index 0` run because the architect emitted validation commands:
  - `python -m py_compile prime.py tests/test_prime.py`
  - `pytest tests/`
  and did not include `ruff check .`, so no missing `ruff` install was required.

---

## Round 10 (Codex Implementation Instructions — Round 10)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 10)

### Files Changed / Created
- `src/codegen_agent/dependency_manager.py` (updated)
- `tests/test_conftest_injection.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **Conftest injection hook in Stage 4**
- Added conftest injection call in `resolve_and_install()`:
  - `_ensure_conftest(workspace_root, generated_files)`
  - prints:
    - `[DependencyManager] Wrote conftest.py to make root modules importable.`
  - stores result in:
    - `results["conftest_injected"]`

2. **`DependencyManager._ensure_conftest(...)`**
- Added static method to inject minimal root-level import path shim when:
  - there is at least one root-level source module (`*.py`, non-test)
  - there is at least one test file in a subdirectory (e.g. `tests/test_*.py`)
  - no existing `conftest.py` is present
- Writes:
  - `sys.path.insert(0, workspace_root)` style conftest content.
- Skips injection if tests are only at root.
- Leaves existing `conftest.py` untouched.

3. **Module import**
- Added missing `import os` in `dependency_manager.py` for basename/path checks in `_ensure_conftest`.

4. **Round 10 tests**
- Added `tests/test_conftest_injection.py` with 3 tests:
  - writes conftest when root module + test subdir pattern exists
  - does not overwrite existing conftest
  - does not inject when tests are at root

### Validation

```bash
source .venv/bin/activate && pytest -q tests/test_conftest_injection.py
```

Output:

```text
...                                                                      [100%]
3 passed in 0.04s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
................................................................         [100%]
64 passed in 1.12s
```

Benchmark verification command:

```bash
source .venv/bin/activate && CODEGEN_LLM_TIMEOUT=90 python -u benchmark_agent.py --index 0
```

Observed Stage 4+5 output included expected line:

```text
[DependencyManager] Wrote conftest.py to make root modules importable.
```

Benchmark result summary:

```text
Done in 102.0s | heals=0 | qa=74 | FAIL
```

### Deviations From Spec
- QA score improved from prior run (`68 -> 74`) and the prior `ModuleNotFoundError: No module named 'prime'` issue is no longer present.
- Score did not exceed 84 because QA still reported other issues (lint/style and quality concerns) unrelated to importability.

---

## Round 5 (Codex Implementation Instructions — Round 5)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 5)

### Files Changed / Created
- `src/codegen_agent/run_log.py` (new)
- `src/codegen_agent/metrics.py` (new)
- `src/codegen_agent/reporter.py` (updated)
- `src/codegen_agent/main.py` (updated)
- `tests/test_metrics.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **Run summary logging (`runs.jsonl`)**
- Added `RunSummary` dataclass and helper functions in `run_log.py`:
  - `append_run_summary(runs_path, summary)`
  - `make_run_summary(report)`
- `Reporter.save_report()` now appends one run-summary JSON line to:
  - `.codegen_agent/runs.jsonl`

2. **Rolling metrics computation**
- Added `metrics.py` with:
  - `MetricWindow` dataclass
  - `RollingMetrics` class (`compute(window=20)`)
- Computes:
  - `run_count`
  - `p50_wall_clock`
  - `p90_wall_clock`
  - `first_pass_rate`
  - `avg_heal_attempts`
  - `qa_approval_rate`
- Invalid JSONL lines are safely ignored during load.

3. **`doctor` CLI subcommand**
- Added `RollingMetrics` import in `main.py`.
- Added `_run_doctor_check(workspace="./output")`:
  - reads `.codegen_agent/runs.jsonl`
  - prints rolling-window metrics if present
  - prints “No run data found…” if absent
  - returns exit code `0` in both cases (read-only behavior)
- Added parser and dispatch for:
  - `codegen doctor --workspace <path>`

4. **Round 5 tests**
- Added `tests/test_metrics.py` with 6 tests:
  - RunSummary append/JSON serialization fields
  - `RollingMetrics.compute()` returns `None` when file is absent
  - single-run metric behavior
  - first-pass rate computation
  - p50 wall-clock computation
  - window limiting to last N runs

### Validation

Commands run:

```bash
source .venv/bin/activate && pytest -q tests/test_metrics.py
```

Output:

```text
......                                                                   [100%]
6 passed in 0.02s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
.............................................                            [100%]
45 passed in 0.10s
```

Manual acceptance check (`runs.jsonl` creation after `save_report()`):

```bash
source .venv/bin/activate && PYTHONPATH=src python - <<'PY'
import json, os, tempfile
from codegen_agent.models import PipelineReport
from codegen_agent.reporter import Reporter

with tempfile.TemporaryDirectory() as d:
    report = PipelineReport(prompt='p', wall_clock_seconds=1.23)
    Reporter(d).save_report(report)
    path = os.path.join(d, '.codegen_agent', 'runs.jsonl')
    print('exists', os.path.exists(path))
    with open(path) as f:
        line = f.readline().strip()
    obj = json.loads(line)
    print('keys_ok', all(k in obj for k in ['run_id','wall_clock_seconds','heal_attempts']))
PY
```

Output:

```text
exists True
keys_ok True
```

### Deviations From Spec
- None.

---

## Round 3 (Codex Implementation Instructions — Round 3)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 3)

### Files Changed / Created
- `src/codegen_agent/llm/cache.py` (new)
- `src/codegen_agent/llm/caching_client.py` (new)
- `src/codegen_agent/llm/router.py` (updated)
- `src/codegen_agent/workspace_lock.py` (new)
- `src/codegen_agent/orchestrator.py` (updated)
- `tests/test_llm_cache.py` (new)
- `tests/test_workspace_lock.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **LLM response cache**
- Added file-based cache (`LLMCache`) with SHA256 keying:
  - key input: `provider:model:prompt`
  - storage layout: `.codegen_agent/llm_cache/<first2>/<digest>.json`
- `get()` returns `None` on miss/corrupt entry and response on hit.
- `set()` persists JSON payload `{"response": ...}`.

2. **Caching LLM wrapper**
- Added `CachingLLMClient`:
  - caches `generate()` calls only
  - bypasses cache for `astream()`
  - cache key includes `system_prompt` so context variants do not collide.

3. **Router cache wiring**
- Updated `LLMRouter.get_client_for_role()`:
  - reads toggle from `CODEGEN_CACHE=1`
  - uses distinct client key suffix:
    - `:c` cached
    - `:r` raw
  - wraps raw client with `CachingLLMClient` when enabled.
- Left `_create_client`, `_get_fallback_client`, and `execute_with_retry` unchanged.

4. **Workspace lock**
- Added `WorkspaceLock` with exclusive non-blocking `fcntl.flock` lock on:
  - `<workspace>/.codegen_agent/run.lock`
- Added lock lifecycle to `Orchestrator.run()`:
  - acquire after trace initialization
  - raise runtime error when already locked
  - release in `finally` to guarantee cleanup.
- Stage logic, checkpoint flow, and reporter calls were preserved.

5. **Round 3 tests**
- Added `tests/test_llm_cache.py` with 4 cases:
  - miss returns `None`
  - hit after `set()`
  - different model produces miss
  - different `system_prompt` creates distinct cached entries via `CachingLLMClient`.
- Added `tests/test_workspace_lock.py` with 2 cases:
  - acquire/release success
  - second lock acquire fails on same workspace.

### Validation

Commands run:

```bash
source .venv/bin/activate && pytest -q tests/test_llm_cache.py
```

Output:

```text
....                                                                     [100%]
4 passed in 0.04s
```

```bash
source .venv/bin/activate && pytest -q tests/test_workspace_lock.py
```

Output:

```text
..                                                                       [100%]
2 passed in 0.01s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
..................................                                       [100%]
34 passed in 0.06s
```

Cache acceptance check:

```bash
source .venv/bin/activate && PYTHONPATH=src CODEGEN_CACHE=1 python -m codegen_agent.main health
```

Output (summary):

```text
All checks passed.
```

### Deviations From Spec
- Instruction target says **35 passed**. Actual suite is **34 passed**.
  - Reason: pre-Round-3 baseline was 28 and this round adds 6 tests (4 cache + 2 lock), so expected total is 34.
- Instruction acceptance command shows `python -m codegen_agent health`.
  - In this repo's `src/` layout (without editable install), that invocation is not import-resolvable.
  - Equivalent validated command used: `PYTHONPATH=src python -m codegen_agent.main health`.

---

## Round 4 (Codex Implementation Instructions — Round 4)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 4)

### Files Changed
- `src/codegen_agent/healer.py`
- `src/codegen_agent/executor.py`
- `tests/test_prompt_limits.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **Healer prompt caps**
- Added helper constants and functions in `healer.py`:
  - `_HEALER_ERROR_MAX_LINES = 60`
  - `_HEALER_FILE_CONTENT_MAX = 8_000`
  - `_truncate_error_output(...)` for tail-preserving error truncation
  - `_cap_file_content(...)` for tail-preserving file-content capping
- Wired these into prompt construction in:
  - `_fix_single_failure()`:
    - `error_output` now uses `_truncate_error_output(...)`
    - `file_content` now uses `_cap_file_content(...)`
  - `heal_static_issues()`:
    - `file_content` now uses `_cap_file_content(...)`

2. **Healer prompt safety net**
- Imported `prune_prompt` in `healer.py`.
- Applied `prompt = prune_prompt(prompt, max_chars=16_000)` after prompt assembly in:
  - `_fix_single_failure()`
  - `heal_static_issues()`

3. **Executor prompt safety net**
- Imported `prune_prompt` in `executor.py`.
- Applied:
  - `_execute_bulk()`: `prune_prompt(user_prompt, max_chars=28_000)`
  - `_execute_node()`: `prune_prompt(user_prompt, max_chars=12_000)`

4. **Round 4 tests**
- Added `tests/test_prompt_limits.py` with 5 tests:
  - short/long behavior for `_truncate_error_output`
  - short/long behavior for `_cap_file_content`
  - `prune_prompt` safety-net length cap behavior

### Validation

Commands run:

```bash
source .venv/bin/activate && pytest -q tests/test_prompt_limits.py
```

Output:

```text
.....                                                                    [100%]
5 passed in 0.04s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
.......................................                                  [100%]
39 passed in 0.08s
```

### Deviations From Spec
- None.

---

## Round 6 (Codex Implementation Instructions — Round 6)

**Date:** 2026-02-27
**Source instruction:** `instruction.md` (Round 6)

### Files Changed / Created
- `benchmark_agent.py` (full rewrite)
- `src/codegen_agent/metrics.py` (updated — baseline support)
- `src/codegen_agent/main.py` (updated — `--set-baseline` flag)
- `tests/test_baseline.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **`benchmark_agent.py` rewrite**
- Replaced single-prompt script with 8-prompt fixed suite:
  - 3 small: `prime_checker`, `stack_class`, `csv_stats`
  - 3 medium: `todo_api`, `cli_calculator`, `json_kv_store`
  - 2 large: `data_validators`, `task_queue`
- Added `--tier [small|medium|large|all]` flag (default: `all`).
- Added `--index N` flag to run a single prompt by 0-based index.
- Runs are sequential; each prints immediately.
- Summary table printed at end with P50/P90 wall clock, pass rate, first-pass rate.
- Uses `sys.path.insert` instead of `from src.` imports.

2. **Baseline support in `metrics.py`**
- Added `import dataclasses` at top.
- Added `save_baseline(path, window)` — serialises `MetricWindow` as JSON.
- Added `load_baseline(path)` — returns `None` on missing/corrupt file.
- Added `_band(value, green, amber, higher_is_better)` — returns `"Green"` / `"Amber"` / `"Red"`.
- Added `compare(current, baseline)` → `dict[str, str]` with verdicts for:
  - `runtime`: improvement ≥35% Green, 20-35% Amber, <20% Red
  - `first_pass`: delta ≥+20pp Green, ≥+5pp Amber, <-3pp Red, otherwise Amber
  - `heal_attempts`: reduction ≥30% Green, 15-30% Amber, <15% Red
  - `qa_approval`: Red if drops >2pp, else Green

3. **`codegen doctor --set-baseline`**
- Added import: `save_baseline, load_baseline, compare`.
- Added `--set-baseline` flag to `doctor_parser`.
- Rewrote `_run_doctor_check(workspace, set_baseline=False)`:
  - When `--set-baseline`: saves current window and exits.
  - Otherwise: prints metrics, then loads baseline and prints Green/Amber/Red per metric if available.
- Updated dispatch: `_run_doctor_check(args.workspace, set_baseline=args.set_baseline)`.

4. **Round 6 tests**
- Added `tests/test_baseline.py` with 4 tests:
  - `test_save_and_load_baseline` — roundtrip via temp dir; asserts all fields match.
  - `test_load_baseline_missing_returns_none` — nonexistent path returns `None`.
  - `test_compare_green_runtime` — baseline p50=100s, current p50=60s → 40% improvement → `runtime=Green`.
  - `test_compare_red_qa` — baseline qa_rate=0.90, current=0.87 → delta=-0.03 → `qa_approval=Red`.

### Validation

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
.................................................                        [100%]
49 passed in 0.08s
```

```bash
source .venv/bin/activate && python benchmark_agent.py --help
```

Output:

```text
usage: benchmark_agent.py [-h] [--tier {small,medium,large,all}] [--index INDEX]

Codegen Agent Benchmark Suite (§2.2)

options:
  -h, --help            show this help message and exit
  --tier {small,medium,large,all}
                        Run only prompts of this tier (default: all)
  --index INDEX         Run only the prompt at this 0-based index in BENCHMARK_PROMPTS
```

Smoke test command:

```bash
source .venv/bin/activate && python -u benchmark_agent.py --index 0
```

Observed output before interruption:

```text
Running 1 benchmark prompt(s) — tier=all

[SMALL] prime_checker
  Prompt: Create a Python module with a single function is_prime(n) that returns True if n...
Stage 1+2+3: Planning, Architecting & Executing (streaming)...
  [StreamExecutor] Streaming Plan+Architect response...
  [StreamExecutor] Stream complete. 3 node task(s) dispatched; awaiting...
  [Executor] Created file: pyproject.toml
  [Executor] Created file: prime.py
  [Executor] Created file: tests/test_prime.py
Stage 4+5: Dependencies & Tests (parallel)...
  [TestWriter] Executor already generated test files. Skipping LLM call.
  [DependencyManager] Installing Python dependencies (pyproject.toml)...
  [Orchestrator] Using architect-specified validation commands: ['ruff check .', 'pytest tests/']
Stage 6: Healing...
```

### Deviations From Spec
- Smoke test (`python benchmark_agent.py --index 0`) entered Stage 6 healing and stalled on external Codex CLI completion.
  - Verified stack at interruption: `Healer._fix_single_failure` waiting on `CodexCLIClient.generate`.
  - The run was manually interrupted (`Ctrl+C`) after prolonged idle to avoid leaving hanging benchmark processes.

---

## Round 7 (Codex Implementation Instructions — Round 7)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 7)

### Files Changed / Created
- `src/codegen_agent/main.py` (updated)
- `src/codegen_agent/orchestrator.py` (updated)
- `src/codegen_agent/healer.py` (updated)
- `src/codegen_agent/llm/codex_cli.py` (updated)
- `src/codegen_agent/llm/claude_cli.py` (updated)
- `src/codegen_agent/llm/gemini_cli.py` (updated)
- `tests/test_perf_flags.py` (new)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **`--max-heals` flag in CLI**
- Added `--max-heals` to `run` subcommand in `main.py`.
- Wired into orchestrator call:
  - `orchestrator.run(..., max_heals=args.max_heals)`

2. **Orchestrator max-heals plumbing**
- Updated `Orchestrator.run` signature:
  - `async def run(self, prompt: str, resume: bool = False, max_heals: int = 3)`
- Updated healer construction in Stage 6:
  - `Healer(..., max_attempts=max_heals)`

3. **Parallel static-issue healing**
- Refactored `Healer.heal_static_issues()` to run per-file static fixes concurrently via:
  - `asyncio.gather(*tasks, return_exceptions=True)`
- Preserved existing per-file guard behavior and result collection:
  - `HealAttempt` entries are collected
  - `None` and `Exception` results are skipped

4. **Codex CLI hang fix**
- Rewrote `CodexCLIClient.generate()` to async subprocess pattern:
  - `asyncio.create_subprocess_exec(...)`
  - `await asyncio.wait_for(process.communicate(), timeout=timeout)`
  - on timeout: `process.kill(); await process.wait(); raise LLMTimeoutError(...)`
- Removed dead `_generate_sync()` thread-based path.
- Timeout now read from env at call time:
  - `timeout = int(os.environ.get("CODEGEN_LLM_TIMEOUT", "120"))`

5. **`CODEGEN_LLM_TIMEOUT` for all CLI clients**
- `ClaudeCLIClient.generate()` now reads timeout from env at call time and uses it for:
  - `wait_for(..., timeout=timeout)`
  - timeout error message
- `GeminiCLIClient.generate()` updated similarly.
- `astream()` behavior in Gemini/Codex left unchanged as instructed.

6. **Round 7 tests**
- Added `tests/test_perf_flags.py` with 3 tests:
  - `test_max_heals_zero_makes_no_llm_calls`
  - `test_codex_cli_timeout_raises_lmmtimeouterror`
  - `test_llm_timeout_env_controls_all_cli_clients`

### Validation

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
....................................................                     [100%]
52 passed in 1.13s
```

Requested command:

```bash
source .venv/bin/activate && python -m codegen_agent run --help
```

Output:

```text
/Users/aditya/Desktop/Coding Agent/.venv/bin/python: No module named codegen_agent
```

Equivalent command used to verify `--max-heals` visibility:

```bash
source .venv/bin/activate && PYTHONPATH=src python -m codegen_agent.main run --help
```

Output:

```text
usage: python -m codegen_agent.main run [-h] [--prompt PROMPT]
                                        [--workspace WORKSPACE]
                                        [--config CONFIG] [--resume]
                                        [--verbose] [--max-heals MAX_HEALS]

options:
  -h, --help            show this help message and exit
  --prompt PROMPT
  --workspace WORKSPACE
  --config CONFIG
  --resume
  --verbose
  --max-heals MAX_HEALS
                        Maximum heal iterations (0 = skip healing, default: 3)
```

### Deviations From Spec
- `python -m codegen_agent run --help` is not import-resolvable in this repository's `src/` layout without installing the package.
- Verified the required `--max-heals` flag using `PYTHONPATH=src python -m codegen_agent.main run --help`.

---

## Post-Round 7 Fix (Healing Root Cause)

**Date:** 2026-02-27  
**Reason:** Healing loop could stall on environment/tooling failures (example: `ruff` missing), then try to "fix" irrelevant files like `.pytest_cache/README.md`.

### Files Changed
- `src/codegen_agent/healer.py`
- `tests/test_healer.py`
- `benchmark_agent.py`
- `docs/implemented.md` (this update)

### What Was Fixed

1. **Stop healing non-healable environment failures**
- Added missing-tool detection in healer (`command not found`, similar patterns).
- `_fix_single_failure()` now returns a blocked reason immediately for missing tools instead of issuing an LLM edit request.

2. **Prevent bad target-file selection**
- Added runtime path ignore rules for caches/infra dirs:
  - `.pytest_cache`, `.codegen_agent`, `.git`, `__pycache__`, `.venv`, `venv`, `node_modules`
- Applied this to:
  - `_extract_target_file()` (skips ignored paths from error output)
  - `_get_most_recent_file()` (prunes ignored dirs from scan)

3. **Benchmark runner quality-of-life**
- `benchmark_agent.py` now supports `--max-heals` (default `0`) and passes it into `orchestrator.run(...)`.
- This avoids benchmark runs being dominated by healing retries while the root healer fix handles missing-tool failures correctly.

### Regression Tests Added
- `test_fix_single_failure_blocks_missing_tool`
  - Verifies missing `ruff` returns blocked reason and does not call LLM.
- `test_get_most_recent_file_ignores_pytest_cache`
  - Verifies cache files are not selected as healing targets.

### Validation

```bash
source .venv/bin/activate && pytest -q tests/test_healer.py
```

Output:

```text
......                                                                   [100%]
6 passed in 0.05s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
......................................................                   [100%]
54 passed in 1.12s
```

---

## Post-Round 7 Fix (QA Auditor Latency / Timeout Robustness)

**Date:** 2026-02-27  
**Reason:** QA stage could appear "stuck" for long periods; prompt payload was too large and Codex subprocess timeout handling needed stronger cleanup semantics.

### Files Changed
- `src/codegen_agent/qa_auditor.py`
- `src/codegen_agent/llm/codex_cli.py`
- `tests/test_qa_auditor.py` (new)
- `docs/implemented.md` (this update)

### What Was Fixed

1. **QA prompt compaction + pruning**
- Replaced full `plan.to_dict()` / `architecture.to_dict()` payload in QA with a compact summary:
  - plan metadata and feature titles only
  - architecture counts + file sample + validation commands
  - execution/test/healing high-signal counters and status
- Added prompt cap:
  - `prune_prompt(user_prompt, max_chars=12_000)`

2. **Codex timeout kill robustness**
- `CodexCLIClient.generate()` subprocess now starts with `start_new_session=True`.
- On timeout, it kills the whole process group (`os.killpg(..., SIGKILL)`), then waits for process exit.
- Fallback remains `process.kill()` if process-group kill fails.
- Prevents orphaned child Codex processes after timeout/cancel scenarios.

### Regression Test Added
- `test_qa_auditor_compact_prompt_is_pruned`
  - Builds a very large synthetic report payload.
  - Verifies QA audit still succeeds with a fake LLM response.
  - Asserts emitted QA prompt length is capped (`<= 12_000`).

### Validation

```bash
source .venv/bin/activate && pytest -q tests/test_qa_auditor.py tests/test_healer.py tests/test_perf_flags.py
```

Output:

```text
..........                                                               [100%]
10 passed in 1.06s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
.......................................................                  [100%]
55 passed in 1.10s
```

Benchmark smoke check (healing enabled):

```bash
source .venv/bin/activate && CODEGEN_LLM_TIMEOUT=60 python -u benchmark_agent.py --index 0 --max-heals 3
```

Observed behavior:
- Pipeline no longer hangs indefinitely.
- Run exits cleanly with timeout error when QA exceeds timeout budget:
  - `FAILED after 108.6s: Codex CLI timed out after 60s`
- Verified no lingering `benchmark_agent.py` or `codex exec` processes after exit.

---

## Round 8 (Codex Implementation Instructions — Round 8)

**Date:** 2026-02-27  
**Source instruction:** `instruction.md` (Round 8)

### Files Changed / Created
- `src/codegen_agent/llm/router.py` (updated)
- `src/codegen_agent/llm/codex_cli.py` (updated)
- `src/codegen_agent/main.py` (updated)
- `tests/test_round8.py` (new)
- `tests/test_router.py` (updated for wrapped client assertions)
- `docs/implemented.md` (this update)

### What Was Implemented

1. **Global retry/fallback wrapper in router**
- Added `_RetryingLLMClient` in `router.py`.
- `LLMRouter.get_client_for_role()` now returns wrapped clients so all pipeline stages use retry/fallback automatically.
- Updated client cache key to include role:
  - `"{role}:{provider}:{model}:{c|r}"`
  - prevents incorrect wrapper reuse across roles with different fallback configuration.
- Avoids double-wrapping by checking `isinstance(client, _RetryingLLMClient)`.

2. **Backward-compatible `execute_with_retry()`**
- Simplified to delegate to wrapped client:
  - `response = await self.get_client_for_role(role).generate(...)`
  - returns `(response, 0, False, None)` to preserve existing API shape.

3. **`CodexCLIClient.astream()` timeout fix**
- Added env-aware timeout at start of `astream()`:
  - `timeout = int(os.environ.get("CODEGEN_LLM_TIMEOUT", "120"))`
- Replaced `self.timeout_seconds` usage in:
  - stream deadline calculation
  - timeout error message

4. **`codegen status` subcommand**
- Added `_run_status_check(workspace="./output")` in `main.py`.
- Added `status` parser:
  - `status --workspace <path>`
- Added dispatch in `main_async()` to run status and exit with code 0.

5. **Round 8 tests**
- Added `tests/test_round8.py` with:
  - retry-on-timeout behavior for `_RetryingLLMClient`
  - fallback use after primary exhaustion
  - `_run_status_check()` returning 0 for empty workspace
- Updated `tests/test_router.py` assertions to validate wrapped clients via primary client type.

### Validation

```bash
source .venv/bin/activate && pytest -q tests/test_router.py tests/test_round8.py
```

Output:

```text
.......                                                                  [100%]
7 passed in 0.06s
```

```bash
source .venv/bin/activate && pytest -q tests/
```

Output:

```text
..........................................................               [100%]
58 passed in 1.11s
```

```bash
source .venv/bin/activate && PYTHONPATH=src python -m codegen_agent.main status --workspace benchmark_output/prime_checker
```

Output:

```text
Workspace:  /Users/aditya/Desktop/Coding Agent/benchmark_output/prime_checker
Prompt:     Create a Python module with a single function is_prime(n) that returns True if n...
Wall clock: 126.2s

  ✓ PLAN
  ✓ ARCH
  ✓ EXEC
  ✓ DEPS
  ✓ TESTS
  ✓ HEAL
  ✓ QA
  ○ VISUAL

QA score:   76/100  (not approved)

Generated 3 file(s):
  prime.py
  tests/test_prime.py
  pyproject.toml

Heal attempts: 0  (failed)
  Blocked: Missing tool 'ruff' required by validation command 'ruff check .'. Install it in the environment; this is not healable via source edits.
```

### Deviations From Spec
- None.
