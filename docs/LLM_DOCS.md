# LLM Docs and Change Log

## Scope
This document captures the current LLM layer design and the full set of hardening changes made in the latest stabilization pass.

## LLM Layer Overview

### Router
File: `src/codegen_agent/llm/router.py`

The router resolves provider/model per role and supports:
- `gemini_cli` via local `gemini` binary
- `claude_cli` via local `claude` binary
- `anthropic_api` via HTTP API
- `openai_api` via HTTP API
- `codex_cli` via local `codex` binary

Environment-driven defaults:
- `CODEGEN_PROVIDER`
- `CODEGEN_MODEL`
- `CODEGEN_<ROLE>_PROVIDER`
- `CODEGEN_<ROLE>_MODEL`

### Clients
Files:
- `src/codegen_agent/llm/gemini_cli.py`
- `src/codegen_agent/llm/claude_cli.py`
- `src/codegen_agent/llm/anthropic_api.py`
- `src/codegen_agent/llm/openai_api.py`
- `src/codegen_agent/llm/codex_cli.py`

Notes:
- `OpenAIClient` supports both Chat Completions and Responses APIs based on model.
- `CodexCLIClient` supports both single-response and streamed JSON-event parsing modes.
- `LLMClient` protocol defines `generate(...)` and optional `astream(...)`.

## Full Stabilization Changes Applied
Date: 2026-02-27

### 1. Executor guardrails (completeness and node hygiene)
File: `src/codegen_agent/executor.py`

Changes:
- Added directory-node filtering so non-file nodes (for example `path/`) are skipped and tracked in `skipped_nodes`.
- In bulk generation mode, added strict completeness enforcement:
  - Validate response is a JSON object.
  - Validate every planned file path is present.
  - If any file is missing or malformed, fallback to wave-based generation.
- Preserved concurrency behavior for wave execution.

Why:
- Prevented false success when LLM bulk responses silently omitted planned files (root cause of missing package markers like `__init__.py`).

### 2. JSON extraction robustness
File: `src/codegen_agent/utils.py`

Changes:
- Replaced brittle first-brace parser with a resilient scanner using `json.JSONDecoder().raw_decode(...)` over candidate start positions.
- Parser now skips invalid early brace segments and continues searching.

Why:
- Prevented parse failure when LLM output contains prose/noise before valid JSON.

### 3. Source-consistency static checks before healing
File: `src/codegen_agent/orchestrator.py`

Changes:
- Added AST-based Python consistency analysis helpers:
  - Module name/path resolution
  - Relative import resolution
  - Defined symbol extraction
  - Internal import/symbol mismatch detection
- Added `_collect_python_consistency_issues(...)` and a pre-heal static fix phase.
- Stage 6 now applies targeted source fixes for consistency issues before running normal test-driven healing.

Why:
- Catches cross-file mismatches early (for example importing missing symbols/functions between generated files).

### 4. Healer source-first policy (no test edits by default)
File: `src/codegen_agent/healer.py`

Changes:
- Added `allow_test_file_edits` flag (default `False`).
- Added `_is_test_file(...)` filtering in:
  - target file extraction
  - most-recent fallback
  - write refusal path
- Added `heal_static_issues(...)` for deterministic source-targeted fix prompts.
- Refactored secure path resolution into `_resolve_target_path(...)`.

Why:
- Prevented healer from mutating tests to force green builds while leaving product code broken.

### 5. Test generation anti-placeholder hardening
File: `src/codegen_agent/test_writer.py`

Changes:
- Strengthened prompts to require tests that import/exercise real source modules.
- Explicitly banned placeholder/hypothetical commentary and fake substitute harnesses.

Why:
- Reduced low-signal mock-only tests that do not validate the generated app.

### 6. Test quality gate for executor-generated tests
File: `src/codegen_agent/orchestrator.py`

Changes:
- Added `_tests_need_regeneration(...)` heuristic.
- If executor-generated tests appear low-signal or disconnected from source modules, regenerate tests via `TestWriter`.

Why:
- Prevented accepting superficial tests that pass but do not verify real code paths.

### 7. Root pytest ergonomics and isolation
Files:
- `pyproject.toml`
- `tests/conftest.py`

Changes:
- Added `[tool.pytest.ini_options]`:
  - `testpaths = ["tests"]`
  - `norecursedirs = ["Legacy Reference", "benchmark_output", "test_output", ".venv", ".git"]`
- Added `tests/conftest.py` to insert `src/` into `sys.path`.

Why:
- Made `pytest -q` reliable from repo root without collecting legacy/generated artifacts.

## Tests Added in This Pass
Files:
- `tests/test_utils.py`
- `tests/test_executor.py`
- `tests/test_orchestrator_guards.py`
- `tests/test_healer.py` (extended)

Coverage added:
- JSON extraction with noisy prefixes and arrays
- Executor bulk fallback on incomplete JSON outputs
- Directory node skipping behavior
- Static import/symbol mismatch detection
- Low-signal test regeneration heuristics
- Healer test-file mutation guard and opt-in override behavior

## Verification Run
Commands run:
- `pytest -q` -> `21 passed`
- `python -m compileall -q src/codegen_agent` -> success

## Files Changed in This Stabilization Pass
- `src/codegen_agent/executor.py`
- `src/codegen_agent/utils.py`
- `src/codegen_agent/healer.py`
- `src/codegen_agent/orchestrator.py`
- `src/codegen_agent/test_writer.py`
- `pyproject.toml`
- `tests/conftest.py`
- `tests/test_utils.py`
- `tests/test_executor.py`
- `tests/test_orchestrator_guards.py`
- `tests/test_healer.py`
