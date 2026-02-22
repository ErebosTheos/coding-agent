# Senior Autonomous Developer Agent (Bootstrap)

This repository contains a minimal, testable foundation for a coding agent that:

1. Classifies failure types from command output.
2. Runs a bounded autonomous recovery loop with pluggable fix strategies.
3. Produces structured reports of attempts and outcomes.
4. Enforces strict repository boundaries for all code edits.
5. Supports Codex CLI and Gemini CLI-backed LLM fix generation.
6. Supports optional post-fix validation commands (lint/type/build checks).
7. Returns diff summaries for applied fixes.
8. Rolls back file edits when post-fix verification fails.
9. Supports JSON session report persistence for interrupted-run recovery.
10. Provides LLM-driven feature planning (`FeaturePlanner`) with strict JSON output parsing.

## Quick Start

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

## Scope

The current implementation focuses on reliability primitives only:

1. Failure classification
2. Controlled fix attempts
3. Verification loop
4. Minimal, auditable strategy interface
5. Repo-scoped autonomous replacement strategies
6. LLM-driven file rewrite strategy from runtime error context
7. Post-fix validation command pipeline
8. Diff-aware fix reporting
9. Rollback safety after failed verification
10. Session state serialization (`SessionReport.to_json()` / `from_json()`)
11. Line-aware LLM prompt chunking (`+/-` context window around error lines)
12. Optional retry throttling with exponential backoff and jitter
13. Structured feature implementation planning (`ImplementationPlan`)

## Repository Scope Guard

All strategies must edit files inside the configured workspace. Any attempt to modify files outside the workspace is blocked and reported by the engine.

## LLM Defaults

`create_default_senior_agent(...)` constructs `SeniorAgent` with a default `LLMStrategy` using:

1. `CodexCLIClient` (`provider="codex"`)
2. `GeminiCLIClient` (`provider="gemini"`)

Both clients include timeout and rate-limit error handling.

`SeniorAgent` also supports optional retry pacing via:

1. `retry_backoff_base_seconds`
2. `retry_backoff_max_seconds`
3. `retry_backoff_jitter_seconds`

`LLMStrategy` includes a context buffer that loads up to three error-referenced files into the prompt, while only applying edits within repository boundaries.

When verification fails after a strategy applies changes, the engine restores strategy-provided rollback snapshots before continuing or terminating.

If a strategy reports changed files, it must also provide rollback snapshots for those files or the engine blocks the attempt.

## Checkpoint & Resume

Pass `checkpoint_path` to `SeniorAgent.heal(...)` to persist session progress after each attempt.

Call `SeniorAgent.resume(checkpoint_path=...)` to continue from the last persisted state.

Checkpoint files include:

1. `schema_version`
2. workspace fingerprint (resolved workspace path)
3. strategy fingerprint (ordered strategy identity hash)
4. validation fingerprint (ordered validation command hash)

`resume(...)` validates these fields before continuing and fails fast on incompatibilities.
