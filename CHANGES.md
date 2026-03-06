# Codegen Agent — Change Log

## Session: Parallel Post-Build Pipeline + Agent Intelligence Upgrades

---

### 1. Parallel Post-Build Pipeline (UI + Server)

**Problem:** After build, Docs → Heal → QA ran sequentially. UI showed no live feedback.

**Changes:**

- `server.py` — Restructured `_run_pipeline()`: after build completes, publishes `build_approved`
  event and launches Docs, Heal, and QA as three parallel `asyncio.gather()` tasks.
- `project_registry.py` — Added `BUILD_APPROVED` and `QA_RUNNING` states to `ProjectState`.
- New WebSocket events: `build_approved`, `heal_started`, `heal_complete`, `qa_file_reviewed`.
- `index.html` — Added CSS for animated `BUILD_APPROVED` (glowing green) and `QA_RUNNING`
  (glowing purple) state badges. Added Build Approved banner, parallel status badges, and
  QA live panel styles (file rows, score display, issues/suggestions).
- `app.js` — Per-project `qaFiles`, `qaFinal`, `healStatus` state tracking.
  New DOM helpers: `_renderPostBuildPanel`, `_appendQAFileRow`, `_patchHealBadge`,
  `_patchQAFinal` — surgically update the panel without re-rendering the full project view.
  New event formatters for all new event types.

---

### 2. QA Auditor — Streaming Per-File Review

**Problem:** QA read all files in one batch and returned a single score with no live feedback.

**Changes:**

- `qa_auditor.py` — Added `_ANTIPATTERNS`: 6 deterministic regex checks for common bugs
  (`datetime.utcnow`, `sessionmaker`, `ASGITransport`, `@app.on_event`, hardcoded secrets).
- Added `_quick_file_check(file_path, content)`: instant zero-LLM per-file scan.
- Added `audit_streaming(report, on_file_reviewed)`: Phase 1 scans each file and fires the
  callback (→ `qa_file_reviewed` WebSocket event); Phase 2 runs the full LLM batch audit.

---

### 3. Contract Verification (Post-Generation)

**Problem:** The architect planned `public_api` exports but the executor never verified they
were actually present in the generated file. Dependents would silently import missing names.

**Changes:**

- `executor.py` — Added `_verify_contract_exports(file_path, content, public_api)`:
  checks every planned export name against the generated file using regex.
  Supports Python (`def`/`class`/`async def`/assignments) and JS/TS (`export` statements).
- Called after every file write in all three write paths (bulk, stream_bulk, execute_node).
- Violations logged as `[ContractGuard] <file>: missing exports [...]`.

---

### 4. Actual Dependency Content in Wave Execution

**Problem:** `_execute_node` passed only planned `public_api` strings to the LLM.
In wave mode, dependency files are already on disk but their real content was never used.

**Changes:**

- `executor.py` — Added `_extract_dep_api_surface(file_path, content)`: extracts just
  the signature lines (functions, classes, imports) from a generated file — compact but
  complete enough to write correct import statements.
- Added `_build_dep_context(dep_nodes)`: for each dependency, reads the actual on-disk
  content (if available) and adds `actual_api_surface` to the dependency dict passed to
  the LLM. Falls back to planned `public_api` for wave-1 nodes not yet on disk.
- Increased per-node prompt budget from 12,000 to 14,000 chars.

---

### 5. Multi-Model Tier Selection

**Problem:** Every node — from a trivial `__init__.py` to a complex auth module — used the
same model at the same cost.

**Changes:**

- `executor.py` — Added `_node_complexity_tier(node)` → `"simple"` | `"standard"` | `"complex"`:
  - `complex`: auth/security/database/main/middleware files, or ≥5 dependencies
  - `simple`: config/schema/enum/init files, or 0 dependencies
  - `standard`: everything else
- Added `_select_client(node)` method to `Executor`: picks from `tier_clients` dict.
- `Executor.__init__` now accepts optional `tier_clients: dict[str, LLMClient]`.
- `router.py` — Added `_TIER_ENV_KEYS` and `get_tier_clients(base_role)` method.
  Reads `CODEGEN_FAST_PROVIDER/MODEL` and `CODEGEN_COMPLEX_PROVIDER/MODEL` from env.
- `orchestrator.py` — All `Executor(...)` instantiations now pass `tier_clients=router.get_tier_clients("executor")`.
- `.env.example` — Documented the four new env vars with examples.

---

### 6. Structured Pytest Output in Healer

**Problem:** The healer regex-scanned raw stderr for file paths. When a test file failed
because a *source* file was broken, the healer often tried to fix the test instead of the source.

**Changes:**

- New `pytest_parser.py` — `run_pytest_structured(cmd, workspace)` runs pytest with
  `--json-report` and parses the JSON report. Returns a `PytestReport` with:
  - Exact test IDs and outcomes
  - Per-failure tracebacks with source file paths (non-test files only)
  - `broken_source_files` dict: source file → failing tests that reference it
  - `format_structured_failures_for_prompt(report)`: compact healer-readable string
- `healer.py` — `heal()` now runs structured and plain pytest in parallel.
  When structured data is available, `broken_source_files` replaces regex-based file extraction.
  Structured failure context (test name + assertion + traceback) injected into healer prompts.
  Falls back gracefully to regex if `pytest-json-report` is not installed.

---

### 7. Cross-Project Pattern Store

**Problem:** The same bug (e.g. `sessionmaker` instead of `async_sessionmaker`) was
re-discovered from scratch on every new project, wasting LLM calls.

**Changes:**

- New `pattern_store.py` — `PatternStore`: persistent JSON store at
  `~/.codegen_agent/patterns.json`. Maps a failure fingerprint (SHA of failure type +
  first meaningful error line) to the fix description that resolved it.
  - `fingerprint(failure_type, error_text)` — stable 16-char hex key
  - `lookup(fp)` — returns known fix or None
  - `record(fp, fix_description)` — called after successful heals
  - `known_patterns_prompt(fingerprints)` — healer-prompt section of matched known fixes
  - Trimmed to 300 most-recent entries on every save
- `healer.py` — `Healer.__init__` creates a `PatternStore`.
  Before each LLM fix call, fingerprints are computed and matched known fixes are injected
  into the prompt as "Known fixes from previous projects".
  After successful heals, patterns are recorded for future runs.

---

### 8. Test Coverage

- Added `tests/test_pattern_store.py` — 10 tests covering fingerprinting, persistence,
  roundtrip, max-trim, overwrite, and prompt generation.
- Added `tests/test_pytest_parser.py` — 15 tests covering command detection, flag injection,
  JSON report parsing, source file attribution, and prompt formatting.
- Fixed `tests/test_qa_auditor.py` — removed outdated `score >= 85` assertion that relied
  on the now-removed artificial score floor.

---

## Environment Variables Added

| Variable | Purpose |
|---|---|
| `CODEGEN_FAST_PROVIDER` | Provider for "simple" tier nodes (config, init, schema files) |
| `CODEGEN_FAST_MODEL` | Model for "simple" tier (e.g. `claude-haiku-4-5-20251001`) |
| `CODEGEN_COMPLEX_PROVIDER` | Provider for "complex" tier nodes (auth, database, entrypoints) |
| `CODEGEN_COMPLEX_MODEL` | Model for "complex" tier (e.g. `claude-opus-4-6`) |

All four are optional. If unset, the standard executor client is used for all nodes.

---

## Files Changed

| File | Type |
|---|---|
| `src/codegen_agent/pattern_store.py` | **New** |
| `src/codegen_agent/pytest_parser.py` | **New** |
| `tests/test_pattern_store.py` | **New** |
| `tests/test_pytest_parser.py` | **New** |
| `src/codegen_agent/executor.py` | Modified |
| `src/codegen_agent/healer.py` | Modified |
| `src/codegen_agent/qa_auditor.py` | Modified |
| `src/codegen_agent/llm/router.py` | Modified |
| `src/codegen_agent/orchestrator.py` | Modified |
| `src/codegen_agent/dashboard/server.py` | Modified |
| `src/codegen_agent/dashboard/project_registry.py` | Modified |
| `src/codegen_agent/dashboard/static/index.html` | Modified |
| `src/codegen_agent/dashboard/static/app.js` | Modified |
| `tests/test_qa_auditor.py` | Fixed |
| `.env.example` | Modified |
