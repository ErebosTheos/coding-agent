# Codex Stabilization Pass — Approval Review
Date: 2026-02-27
Reviewer: Claude Sonnet 4.6
Scope: All files changed by Codex in the latest stabilization pass
Source of truth: `src/codegen_agent/llm/LLM_DOCS.md`

---

## Summary Verdict

**APPROVE with 1 flag**

All functional changes are correct, well-structured, and add genuine value.
One inconsistency in `openai_api.py` needs a follow-up fix (flagged below).

---

## File-by-File Review

---

### `src/codegen_agent/llm/LLM_DOCS.md` — NEW FILE
**APPROVE**

Accurate documentation of every change in the stabilization pass.
Dates match. File list is complete. Verification commands are included.
Good to have this as a permanent audit trail inside the llm/ package.

---

### `src/codegen_agent/llm/codex_cli.py` — NEW FILE
**APPROVE**

- `_generate_sync`: Uses `codex exec --full-auto --ephemeral --skip-git-repo-check --sandbox read-only -o FILE`. Writes output to a `tempfile`, reads it, cleans up in `finally`. Correct.
- Timeout: `subprocess.run(timeout=self.timeout_seconds)` raises `TimeoutExpired`, caught and re-raised as `LLMTimeoutError`. Correct.
- `astream()`: Uses `--json` flag, reads JSONL events line by line, yields only `agent_message` + `role=assistant` content. Correct filter to avoid tool-call noise.
- `asyncio.to_thread` wraps the sync call. Correct pattern for non-blocking.
- `process.stdout` is asserted not None before use. Good defensive guard.
- The `--sandbox read-only` flag is appropriate: prevents Codex from writing workspace files since the executor handles those writes.

**Minor note**: If a future `codex` binary version drops `--skip-git-repo-check`, this will silently fail. Consider catching that in a comment. Not a blocker.

---

### `src/codegen_agent/llm/router.py` — MODIFIED
**APPROVE**

- `codex` and `codex_cli` aliases added to `_PROVIDER_ALIASES`. Correct.
- `codex_cli: None` in `_DEFAULT_MODELS` (CLI picks its own default). Correct.
- `_create_client` handler for `codex_cli` instantiates `CodexCLIClient(model=model)`. Correct.
- `_ALL_ROLES` covers all six roles. Correct.
- Default fallback is `gemini_cli` when `CODEGEN_PROVIDER` is not set. Acceptable.

---

### `src/codegen_agent/llm/protocol.py` — MODIFIED
**APPROVE**

- `LLMError`, `LLMTimeoutError`, `LLMContextWindowError` exception hierarchy added. Clean.
- `astream()` mentioned in docstring as optional — intentionally not in the Protocol signature since Python structural typing allows optional methods without ABC enforcement. Correct design.
- `AsyncIterator` imported. Correct.

---

### `src/codegen_agent/llm/anthropic_api.py` — MODIFIED
**APPROVE**

- `astream()` added as a single-chunk fallback: `yield await self.generate(...)`. Clean and correct.
- No real SSE streaming but the interface is satisfied. Correct for now.
- Error handling wraps all exceptions as `LLMError`. Correct.

---

### `src/codegen_agent/llm/openai_api.py` — MODIFIED
**APPROVE with FLAG**

Good parts:
- Dual-API support: `_uses_responses_api()` detects Codex/o-series models and routes to `/v1/responses`. Clean.
- Fallback `output_text` shortcut vs `output[0]["content"][0]["text"]`. Correct.
- `astream()` single-chunk fallback. Correct.

**FLAG — Default model inconsistency:**
```python
# openai_api.py line 22:
model: str = "codex-mini-latest"

# router.py line 42:
"openai_api": "gpt-4o"
```
The `OpenAIClient` dataclass default (`codex-mini-latest`) conflicts with the router default (`gpt-4o`).
When the router creates the client, it passes `gpt-4o` so runtime behavior is fine.
But direct instantiation (`OpenAIClient()`) would get `codex-mini-latest` and hit the Responses API unexpectedly.
**Recommended fix**: Change `model: str = "codex-mini-latest"` to `model: str = "gpt-4o"` in `openai_api.py`.

---

### `src/codegen_agent/llm/gemini_cli.py` — MODIFIED
**APPROVE**

- `astream()` added: reads stdout in 4 KB chunks with deadline-based timeout. Correct.
- `DEFAULT_TRANSPORT_SAFETY_PROMPT` now includes:
  - "Do NOT use file-reading tools, search tools, or any other tools."
  - "Output ONLY the requested artifact. No reasoning, no 'I will...' text."
  This directly fixes the agentic prose contamination bug from the previous session.
- `build_transport_prompt()` wraps user prompt in `<<SYSTEM>>`, `<<SAFETY>>`, `<<USER_PROMPT>>` sections. Helps CLI models respect boundaries.
- Error propagation: re-raises `LLMTimeoutError` and `LLMError` unchanged, wraps others. Correct pattern.

---

### `src/codegen_agent/llm/claude_cli.py` — MODIFIED
**APPROVE**

- `astream()` added as single-chunk fallback. Correct (Claude CLI buffers output).
- `CLAUDECODE` env var stripped from child process env — required for nested invocation. Correct.
- `--dangerously-skip-permissions` flag present. Acceptable for headless automation.
- `--no-session-persistence` prevents session state leaking between calls. Correct.

---

## Changes Outside `llm/` (Stabilization Pass)

---

### `src/codegen_agent/executor.py` — MODIFIED
**APPROVE**

- `_is_directory_path()`: skips nodes whose `file_path` ends with `/`. Correct fix for architect emitting `path/` placeholders.
- `skipped_nodes` tracked and returned in `ExecutionResult`. Correct.
- Bulk completeness enforcement: validates response is a `dict`, checks every planned file is present, falls back to wave if not. This is the right fix for silent omissions that broke `__init__.py` generation.
- `_strip_leading_prose()` and `_CODE_START_RE` remove agentic "I will..." prefixes from code files. Correctly skips data/config extensions.

---

### `src/codegen_agent/utils.py` — MODIFIED
**APPROVE**

- `find_json_in_text()`: replaced brittle first-brace scan with `json.JSONDecoder().raw_decode()` over all candidate `{` and `[` positions. Skips invalid fragments and continues. This is significantly more robust.
- `_COMMAND_TIMEOUT = 120` and `TimeoutExpired` handling in `run_shell_command()`. Correct fix for healer hanging on GUI/infinite-loop processes.

---

### `src/codegen_agent/orchestrator.py` — MODIFIED
**APPROVE**

- `_collect_python_consistency_issues()`: AST-based analysis that catches missing internal modules and missing symbols before the healing stage. Correct use of `ast.parse`, `ast.walk`, relative import resolution.
- `_tests_need_regeneration()`: heuristic checking for suspicious phrases ("in a real scenario", "hypothetical", etc.) and verifying test files import actual source modules. Good signal for low-quality tests.
- Stage 6 now runs `heal_static_issues()` before test-driven healing. Correct ordering.
- `_test_suite_from_executor_files()`: avoids redundant LLM call when executor already generated good tests. Correct.
- `arch_cmds` override: uses `architecture.global_validation_commands` over TestWriter heuristics. Correct — the architect knows the tech stack.

**Minor note**: `_looks_internal()` in `_collect_python_consistency_issues` checks if an imported module matches any package root. This could produce false positives if a third-party library name collides with a generated package name (e.g., both generate and pip have a package called `utils`). Acceptable trade-off for now.

---

### `src/codegen_agent/healer.py` — MODIFIED
**APPROVE**

- `allow_test_file_edits: bool = False`: prevents healer from greening tests by mutating them. Critical safety guard.
- `_is_test_file()` applied in three places: target extraction, most-recent fallback, write guard. Thorough.
- `heal_static_issues()`: targeted LLM prompt for each file with static issues. Correct.
- `_resolve_target_path()`: path traversal guard using `.relative_to(workspace_path)`. Security-correct.
- Timeout bail-out: if `exit_code == -1` and `"timeout"` in stderr, returns `blocked_reason` immediately. Correct fix for repeated timeout healing loops.

---

### `src/codegen_agent/test_writer.py` — MODIFIED
**APPROVE**

- `default_framework = "pytest"` (was `unittest`). Correct — avoids `python3 file.py` commands that skip pytest discovery.
- Generic fallback command changed from `python3 file.py` to `python3 -m pytest file.py`. Correct.
- Bulk and single-test prompts now explicitly ban: fake harnesses, placeholder commentary, hypothetical wrappers, `cannot inspect` disclaimers. Quality improvement.

---

### `pyproject.toml` — MODIFIED
**APPROVE**

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
norecursedirs = ["Legacy Reference", "benchmark_output", "test_output", ".venv", ".git"]
```
Correct. Prevents pytest from crawling generated project dirs and legacy reference material.

---

### `tests/conftest.py` — NEW FILE
**APPROVE**

```python
sys.path.insert(0, str(ROOT / "src"))
```
Simple, correct. Makes `import codegen_agent` work from any test without installing the package.

---

### `tests/test_utils.py` — NEW FILE
**APPROVE**

4 tests covering: plain JSON, noisy prefix with invalid brace, array, no JSON present.
All are real assertions against real code. No mocks, no placeholders.

---

### `tests/test_executor.py` — NEW FILE
**APPROVE**

2 tests covering:
1. Bulk fallback when response is missing planned files — patches `_execute_wave_fallback` and verifies it's called.
2. Directory node skipping — verifies `skipped_nodes` contains `pkg/` and actual file is created.

Both are real behavioral tests. Good coverage of the new guardrails.

---

### `tests/test_orchestrator_guards.py` — NEW FILE
**APPROVE**

4 tests covering:
1. Missing symbol import detection (`calculate` not exported by `app.logic`).
2. Missing module detection (`app.missing_module` doesn't exist).
3. `_tests_need_regeneration` returns `True` for placeholder commentary.
4. `_tests_need_regeneration` returns `False` when test imports real source module.

Real assertions, no mocks. Exactly what these heuristics needed.

---

### `tests/test_healer.py` — MODIFIED (extended)
**APPROVE**

4 tests covering:
1. `_get_most_recent_file` filters by extension (ignores `.pyc`, `.bin`).
2. `_extract_target_file` allows `.py`, rejects `.pyc` even if file exists.
3. `_extract_target_file` skips test files by default.
4. `_extract_target_file` allows test files when `allow_test_file_edits=True`.

Tests use `tempfile.TemporaryDirectory`, create real files, assert on real behavior.

---

## Action Required

| Priority | File | Action |
|----------|------|---------|
| LOW | `src/codegen_agent/llm/openai_api.py` | Change `model: str = "codex-mini-latest"` to `"gpt-4o"` to match router default and avoid unexpected Responses API calls on direct instantiation |

---

## Final Decision

**APPROVE all changes.**
One low-priority fix recommended for `openai_api.py` default model.
The stabilization pass is a net positive: adds robustness, test coverage, security guards, and quality gates with no regressions introduced.
