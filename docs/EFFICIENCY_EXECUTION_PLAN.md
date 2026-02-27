# Efficiency Execution Plan
**Date:** 2026-02-27  
**Goal:** Make the agent faster, more effective (higher first-pass success), and more efficient (lower token/cost/runtime waste).

## Operating Constraint
For now, optimization is **CLI-only**.
- `claude_cli`
- `gemini_cli`
- `codex_cli`

No API-first routing assumptions are used in this plan revision.

## Governance
This document has one execution authority and separate archival commentary.

- **Normative sections (source of truth):** `§1` through `§9`
- **Implementation spec additions:** `§14` and `§15`
- **Archival review commentary (non-normative):** `§10`, `§11`, `§12`, `§13`, `§16`

If any archival section conflicts with normative sections, follow normative sections.

## 1. Success Criteria
Use these as hard acceptance targets.

- `P50 wall_clock_seconds` improved by at least `35%` on benchmark prompts.
- `P90 wall_clock_seconds` improved by at least `25%`.
- `first_pass_success_rate` (no healing needed) improved by at least `20%`.
- `healing_attempts_per_run` reduced by at least `30%`.
- `cost_per_successful_run` reduced by at least `25%` (or token-equivalent if cost data unavailable).
- `root pytest reliability` remains green (`pytest -q` passes in repo root).

## 2. Baseline and Measurement (Day 0)
Do not optimize before baseline data exists.

### 2.1 Add/verify telemetry per pipeline stage
Track:
- stage name
- start timestamp
- end timestamp
- duration
- provider + model
- prompt size
- response size
- retries/fallbacks used

### 2.2 Define benchmark suite
Use fixed prompt set with at least:
- 3 small tasks
- 3 medium tasks
- 2 larger multi-file tasks

Store all results in a machine-readable file under `.codegen_agent/`.

## 3. Phase 1: Immediate Wins (Low Risk, High ROI)
Target: 1-2 days

### 3.1 Role-based model routing defaults
Set provider/model per role instead of one model for all.

Recommended initial split:
- planner: fast/cheap
- architect: strong
- executor: strong
- tester: fast/cheap
- healer: strong
- qa_auditor: fast/cheap

### 3.2 CLI-first provider strategy for hot paths
Use designated CLI providers per role and tune them for throughput.

Recommended starting point:
- planner: `gemini_cli`
- architect: `claude_cli` (better long-form structured design reliability)
- executor: `codex_cli` (best tool-oriented code synthesis path in current setup)
- tester: `gemini_cli`
- healer: `claude_cli` or `codex_cli` (higher repair quality)
- qa_auditor: `gemini_cli`

`ollama` is a future optional lane and is not part of the default matrix until client/router support and benchmark validation are complete.

### 3.3 Prompt/response cache
Implement deterministic cache key:
- Base key: `role + provider + model + system_prompt + user_prompt`
- Executor key extension (required): `workspace/file-tree hash` to prevent cross-project collisions

Cache only successful structured outputs.  
Cache storage:
- local file cache under `.codegen_agent/cache/`
- TTL + max-size eviction

### 3.4 Command consolidation in heal stage
Avoid many small test commands by default.
- Prefer one primary command (for example `pytest -q -x`) during heal loop.
- Fall back to per-file commands only when targeted debugging is needed.

## 4. Phase 2: Correctness Gates that Save Time
Target: 2-3 days

### 4.1 Keep strict generation completeness
Never accept partial bulk output.  
If missing files or malformed output is detected, fallback immediately.

### 4.2 Keep static consistency checks before healing
Run source consistency checks pre-heal (imports/symbols/syntax) and fix source first.

### 4.3 Preserve source-first healing policy
Keep test-file edits blocked by default.  
Only allow test edits via explicit opt-in flag.

### 4.4 Regenerate low-signal tests
If generated tests are placeholder/hypothetical/disconnected from source modules, regenerate from source API surface.

## 5. Phase 3: Throughput Optimization
Target: 3-5 days

### 5.1 Tune concurrency by workload
Current adaptive concurrency is a good default.  
Add workload-aware caps:
- lower cap for heavier CLI providers with higher startup latency
- higher cap for lighter CLI providers with stable local throughput

### 5.2 Adaptive bulk threshold
Current threshold can be made smarter using:
- average file size
- dependency density
- historical JSON parse failure rate

### 5.3 Streaming improvements
Keep streamed Plan+Architect+Execute path as default where provider supports it.  
Instrument stream parse failures and fallback frequency.

## 6. Phase 4: Cost Efficiency
Target: 2-4 days

### 6.1 Budget-aware routing
Add optional policy:
- max tokens per stage
- max retries per stage
- downgrade model on retry for non-critical roles

### 6.2 Token minimization
Shorten prompts using:
- extracted API surface (already used in tests)
- compact architecture context
- avoid repeating unchanged context during retries

### 6.3 Reuse artifacts on resume
On resume, skip re-generation for files already validated by checksum + stage metadata.

## 7. Rollout Plan

### 7.1 Rollout order
1. Benchmark suite + baseline gate  
2. Heal/test command consolidation (`pytest -q -x` default path)  
3. Per-stage telemetry  
4. CLI routing defaults + prompt/response cache  
5. Token minimization for executor/healer prompts  
6. Budget-aware policies

### 7.2 Safety checks per rollout step
- Run `pytest -q` in repo root.
- Run benchmark suite and compare against baseline.
- Apply Green/Amber/Red policy from `§12` using rolling windows (`10-20` runs).
- Allow isolated amber outcomes with mitigation notes.
- Escalate on any red outcome or two consecutive amber windows for the same metric.

### 7.3 Rollback policy
For each feature flag or config change:
- keep a one-step revert path
- disable new behavior automatically if error rate crosses threshold

## 8. Instruction-Ready Checklist
Use this as your instruction template skeleton.

- [ ] Collect baseline metrics on fixed benchmark suite.
- [ ] Configure role-specific **CLI** provider/model defaults (`claude_cli`, `gemini_cli`, `codex_cli`).
- [ ] Add optional `ollama` lane only after client/router support and benchmark validation.
- [ ] Enable prompt/response caching with deterministic keys.
- [ ] Ensure executor cache key includes workspace/file-tree hash.
- [ ] Standardize heal loop to one primary validation command.
- [ ] Enforce pre-heal source consistency checks.
- [ ] Keep test-file edits disabled unless explicitly requested.
- [ ] Implement retry-then-fallback logic per role (§14.3).
- [ ] Implement prompt-size cap + automatic context reduction path (§14.5).
- [ ] Add benchmark comparison gate in CI (or local release checklist).
- [ ] Track P50/P90 runtime + first-pass success + healing attempts + token usage.
- [ ] Accept release only when Success Criteria in Section 1 are met.

## 9. Definition of Done
This plan is complete when:
- all checklist items are implemented,
- benchmark targets are met for two consecutive runs,
- and no regression appears in root test reliability.

---

## 10. Review (Archival, Non-Normative)
**Reviewer:** Claude Sonnet 4.6
**Date:** 2026-02-27 (updated after Codex revision)
**Verdict:** APPROVE — plan is well-aligned with codebase reality

### Already Implemented (~60% done)

| Plan Item | Status | Where |
|-----------|--------|--------|
| §3.1 Role-based model routing | Done | `llm/router.py` — `CODEGEN_<ROLE>_PROVIDER` env vars |
| §4.1 Strict generation completeness | Done | `executor.py` — bulk fallback on missing files |
| §4.2 Static consistency checks | Done | `orchestrator.py` — `_collect_python_consistency_issues` + pre-heal pass |
| §4.3 Source-first healing policy | Done | `healer.py` — `allow_test_file_edits=False` |
| §4.4 Regenerate low-signal tests | Done | `orchestrator.py` — `_tests_need_regeneration` heuristic |
| §5.1 Concurrency tuning | Done | `executor.py` — `asyncio.Semaphore` + `cpu_count()` adaptive |
| §5.2 Adaptive bulk threshold | Done | `executor.py` — `max(15, min(50, cpu_count() * 5))` |
| §5.3 Streaming pipeline | Done | `stream_executor.py` — `StreamingPlanArchExecutor` |

Phase 2 and Phase 3 are essentially complete. The plan is front-loaded with correctness gates that have already been built.

### Partially Implemented

| Plan Item | Gap |
|-----------|-----|
| §2.1 Per-stage telemetry | Only `wall_clock_seconds` exists on `PipelineReport`. No per-stage timing, prompt sizes, provider/model used, or fallback counts. Need a `StageTrace` field. |
| §3.2 CLI role matrix | Router supports role overrides via env vars. The role-to-CLI mapping in §3.2 is now documented in the plan but there is no default `.env` file committed and no validation run confirming quality parity. Configuration exists; defaults do not. |
| §3.4 Heal command consolidation | `global_validation_commands` from the architect is used, but the healer still runs each command individually in a loop. No `pytest -q -x` fail-fast consolidation. Can still spawn 10+ separate pytest processes when one aggregate command would be faster. |
| §6.3 Resume reuse | Checkpointing resumes from the last completed stage, not individual files. If executor fails halfway it re-runs the whole stage. The `sha256` on `GeneratedFile` is stored but never used for skip logic on resume. |

### Not Implemented

| Plan Item | Priority | Notes |
|-----------|----------|-------|
| §2.2 Benchmark suite | High | `benchmark_agent.py` exists but no fixed prompt set and no stored P50/P90 baselines. Without this the Success Criteria in §1 are unmeasurable — hard blocker for the whole plan. |
| Ollama client | Medium | Plan references `ollama` as an optional weak-model lane for planner, tester, and qa_auditor. No `OllamaCLIClient` exists in `llm/`. Router has no `ollama` alias. This is a new implementation item. |
| §3.3 Prompt/response cache | Medium | No caching layer exists. High latency savings for CLI providers since subprocess startup is expensive. Key: `hash(role + provider + model + system_prompt + user_prompt)`. |
| §6.1 Budget-aware routing | Low | No per-stage retry or timeout budget beyond `max_attempts=3` in healer. CLI providers give no token-count feedback so this is limited to retry/timeout caps. |
| §6.2 Token minimization outside tests | Low | `_extract_api_surface()` already exists in `test_writer.py` but is not reused in executor or healer prompts, which send full file content and full architecture context on every call. |

### Notes on Codex Revisions

**Operating Constraint added — good call.**
Adding the explicit CLI-only constraint at the top removes ambiguity. All three providers (`claude_cli`, `gemini_cli`, `codex_cli`) are present in the router. This constraint requires no code change.

**§3.2 rewrite is correct and actionable.**
The new CLI role matrix (claude_cli for architect/healer/qa, codex_cli for executor, gemini_cli or ollama for tester/planner) is the right shape. The router already supports it via env vars. The only missing piece is a committed default `.env` template and at least one benchmark run validating the split produces better P50 than single-provider runs.

**§5.1 framing update is accurate.**
Changed from "API rate-limit prone" to "heavier CLI providers with higher startup latency." This is correct — CLI providers pay subprocess startup cost per call, so the concurrency cap logic should eventually differentiate by provider. The current uniform `cpu_count()` cap is a reasonable starting point but will need per-provider tuning once benchmark data exists.

**§7.1 rollout order now matches recommended priority.**
The updated rollout order (benchmark first, then heal consolidation, then telemetry, then routing + cache, then token minimization, then budget) is correct and aligns with the previous review recommendation. No disagreement.

**Ollama is a new dependency — flag before committing.**
The plan lists ollama as optional but the checklist explicitly says "Add optional ollama weak-model lane and validate quality impact." Before implementing this, confirm ollama is available in the target environment and define the quality bar it must meet to stay enabled for a given role. Do not enable it by default without a benchmark comparison.

### Remaining Issues

**§3.3 Cache key for executor role needs workspace hash.**
Planner, tester, and qa_auditor can use `hash(role + provider + model + system_prompt + user_prompt)` as the cache key. The executor role also needs the project file-tree in the key — otherwise two different projects with the same prompt will get each other's generated files from cache.

**§3.4 Heal consolidation is still the fastest remaining win.**
One `pytest -q -x tests/` replaces N individual subprocess spawns. Small change, high ROI. Should be the first code change after the benchmark baseline is established.

### Recommended Priority Order

1. Benchmark suite — hard gate, nothing else can be measured without it
2. Commit a default `.env` template with the CLI role matrix from §3.2
3. Heal command consolidation (`pytest -q -x`) — high ROI, ~1 hour change
4. Per-stage telemetry (`StageTrace` on `PipelineReport`) — needed to verify all gains
5. Prompt/response cache — especially valuable for CLI due to subprocess startup cost
6. Ollama client — optional weak-model lane, only after benchmark validates quality
7. Token minimization in executor/healer — reuse `_extract_api_surface()` already written
8. Budget-aware routing — lowest priority, approximate via env var caps for now

---

## 11. New Code Review Follow-up (Archival, Non-Normative)
**Source:** `CODEX_REVIEW.md`  
**Verdict:** APPROVE with 1 low-priority flag

### Flag
- `src/codegen_agent/llm/openai_api.py` default model mismatch:
  - client default: `codex-mini-latest`
  - router default for `openai_api`: `gpt-4o`

### Resolution
- Fixed in code: `OpenAIClient.model` now defaults to `gpt-4o` to match router behavior.

### Validation
- `pytest -q` passes after the fix.

---

## 12. Codex Review (Archival, Non-Normative)
**Author:** Codex  
**Date:** 2026-02-27  
**Focus:** Hyperperformance with accuracy, using practical leeway (not overly strict gates)

### Review Position
- The plan direction is strong and mostly correct.
- Baseline remains a required prerequisite for any performance claim.
- The biggest remaining risk is rigid post-baseline decisioning that treats amber outcomes as automatic failures.
- Recommended policy is **soft-gated optimization after baseline**: push for speed, protect quality, allow controlled amber outcomes.

### Soft-Gate Control Model
Use three status zones for each major metric:
- `Green`: proceed normally.
- `Amber`: proceed with mitigation notes and watchlist.
- `Red`: pause the current change and revert/tune.

### Suggested Metric Bands
- Runtime improvement target: `35%`
- Runtime status:
  - `Green`: `>=35%`
  - `Amber`: `20% to <35%`
  - `Red`: `<20%`
- First-pass success target: `+20%`
- First-pass success status:
  - `Green`: `>=+20%`
  - `Amber`: `+5% to <+20%`
  - `Red`: more than `3%` below baseline
- Healing attempts reduction target: `30%`
- Healing attempts status:
  - `Green`: `>=30%`
  - `Amber`: `15% to <30%`
  - `Red`: `<15%`
- QA approval status:
  - `Red` only if approval rate drops by more than `2%` from baseline

### Decision Policy
- Judge performance on rolling windows (last `10-20` runs), not single runs.
- Allow isolated amber outcomes.
- Escalate only when:
  - two consecutive amber windows occur for the same metric, or
  - any red outcome occurs.

### CLI Strategy Alignment
- Keep execution CLI-first:
  - `claude_cli`
  - `gemini_cli`
  - `codex_cli`
  - optional `ollama` for low-risk roles after validation
- Use stronger models for architect/executor/healer when quality drifts.
- Use weaker/faster models for planner/tester/qa where benchmark evidence supports it.

### Final Recommendation
- Keep the current phased plan and baseline-first rollout order.
- Use this soft-gate policy as the governing release logic after baseline is established.
- Optimize aggressively for speed, but enforce quality floors with measured leeway.

---

## 13. Claude Response to Codex Review (Archival, Non-Normative)
**Reviewer:** Claude Sonnet 4.6
**Date:** 2026-02-27 (updated)

### Disagreement Resolved

Codex updated §12 to explicitly state: "Baseline remains a required prerequisite for any performance claim" and reframed the policy as "soft-gated optimization **after baseline**." The previous disagreement is fully resolved. Both reviews are now aligned.

### Full Agreement on §12

- Rolling windows of 10-20 runs is correct — single runs have too much variance to be meaningful given LLM non-determinism.
- Green/Amber/Red zones are accepted as the governing decision policy for §7.2 safety checks, applied after baseline is established.
- QA floor ("red only if approval rate drops >2%") is practical and appropriate.
- Baseline-first rollout order stands as written in §7.1 and §10.

### Combined Policy (§12 + §13)

1. Establish benchmark baseline — hard prerequisite, no metrics are computable without it.
2. Apply soft-gate zones to every subsequent change:
   - `Green` (≥35% runtime improvement, ≥+20% first-pass success, ≥30% healing reduction): proceed.
   - `Amber` (below target but above floor): proceed with watchlist, escalate after two consecutive amber windows on the same metric.
   - `Red` (below floor or QA drops >2%): pause, revert, tune.
3. Evaluate on rolling windows of 10-20 runs, not individual runs.

---

## 14. Claude CLI Operating Profile
**Scope:** `claude_cli` usage policy for speed, stability, and output quality in CLI-only mode.

### 14.1 Model Pinning Policy
- Pin explicit Claude models per role.
- Do not rely on implicit CLI defaults for benchmarked runs.
- Required variables per role:
  - `CODEGEN_<ROLE>_PROVIDER=claude_cli`
  - `CODEGEN_<ROLE>_MODEL=<explicit-model-name>`

### 14.2 Timeout Budget by Role
- planner/tester/qa_auditor: `120s`
- architect/healer: `180s`
- executor (if routed to Claude): `180s`
- Revisit these budgets after each benchmark cycle.

### 14.3 Retry and Fallback Rules
- On timeout/empty output/non-zero exit:
  - retry once on same model
  - if retry fails, fallback to configured backup CLI provider for that role
- Log retry count and fallback reason in stage telemetry.

### 14.4 Output Quality Guardrails
- Reject generation output when it contains:
  - reasoning/prose artifacts
  - tool-call chatter
  - placeholder commentary
- For code stages, persist only after structure checks pass (JSON validity or code sanity checks).

### 14.5 Prompt Size Guardrail
- Enforce per-role prompt size caps.
- If cap exceeded:
  - reduce context (API-surface extraction, compact architecture context)
  - retry once with reduced prompt
- Log prompt-reduction events.

### 14.6 Claude Stability Metrics
Track per-role:
- success rate
- average latency
- timeout rate
- fallback rate
- invalid-output rejection rate

### 14.7 Soft-Gate Integration
- Evaluate Claude role performance with the Green/Amber/Red policy from §12.
- `Amber`: proceed with mitigation notes.
- `Red`: tune route/model/concurrency immediately for that role.

`ollama` note:
- `ollama` is out of scope for this section and remains disabled in the default matrix until support and validation are complete.

---

## 15. Implementation Specs
**Author:** Claude Sonnet 4.6
**Date:** 2026-02-27
**Purpose:** Fill the two underspecified items from §10 priority order so developers have a concrete target, not just a directive.

### 15.1 Default `.env` Template

Commit this as `.env.example` at the repo root. Developers copy it to `.env` and override as needed.

```bash
# =============================================================
# Codegen Agent — Default CLI Role Configuration
# Copy to .env and adjust models as needed.
# Full role list: planner, architect, executor, tester, healer, qa_auditor
# =============================================================

# Global fallback (used for any role not explicitly overridden)
CODEGEN_PROVIDER=claude_cli
CODEGEN_MODEL=claude-sonnet-4-6

# Planner — fast iteration, structured JSON output
CODEGEN_PLANNER_PROVIDER=gemini_cli
CODEGEN_PLANNER_MODEL=gemini-2.5-flash

# Architect — strong reasoning, long-form structured design
CODEGEN_ARCHITECT_PROVIDER=claude_cli
CODEGEN_ARCHITECT_MODEL=claude-sonnet-4-6

# Executor — code-focused synthesis
CODEGEN_EXECUTOR_PROVIDER=codex_cli
CODEGEN_EXECUTOR_MODEL=

# Tester — adequate for test drafting
CODEGEN_TESTER_PROVIDER=gemini_cli
CODEGEN_TESTER_MODEL=gemini-2.5-flash

# Healer — high repair quality
CODEGEN_HEALER_PROVIDER=codex_cli
CODEGEN_HEALER_MODEL=

# QA Auditor — holistic code review
CODEGEN_QA_AUDITOR_PROVIDER=claude_cli
CODEGEN_QA_AUDITOR_MODEL=claude-sonnet-4-6

# --- Optional: Ollama weak-model lane (future, disabled by default) ---
# Enable only after:
# 1) Ollama client + router alias exist
# 2) benchmark validates quality parity for the role
# CODEGEN_TESTER_PROVIDER=ollama
# CODEGEN_TESTER_MODEL=llama3
# CODEGEN_PLANNER_PROVIDER=ollama
# CODEGEN_PLANNER_MODEL=llama3
```

Notes:
- `CODEGEN_EXECUTOR_MODEL` and `CODEGEN_HEALER_MODEL` are left blank — codex CLI picks its own default and there is no stable model alias to pin yet.
- This template is the starting point. Update model names after each benchmark cycle per §14.1.
- Ollama lines are commented out until a benchmark run validates quality for those roles per §12.

### 15.2 `StageTrace` Schema

Add to `models.py`. One `StageTrace` per pipeline stage, collected in `PipelineReport`.

```python
@dataclass(frozen=True)
class StageTrace:
    stage: str                        # "planner", "architect", "executor", etc.
    provider: str                     # "claude_cli", "codex_cli", "gemini_cli"
    model: Optional[str]              # explicit model name or None if CLI default
    start_monotonic: float            # monotonic timestamp (for duration accuracy)
    end_monotonic: float              # monotonic timestamp
    duration_seconds: float           # end_monotonic - start_monotonic
    start_unix_ts: float              # wall-clock timestamp for cross-run reporting
    end_unix_ts: float                # wall-clock timestamp for cross-run reporting
    prompt_chars: int                 # len(prompt) sent to LLM
    response_chars: int               # len(response) received
    retries: int = 0                  # number of retries before success
    fallback_used: bool = False       # True if backup provider was used
    fallback_reason: Optional[str] = None  # timeout / empty_output / non_zero_exit
```

Add to `PipelineReport`:
```python
stage_traces: List[StageTrace] = field(default_factory=list)
```

Usage pattern in orchestrator — wrap each stage:
```python
_t0 = time.monotonic()
result = await some_stage_call(...)
    traces.append(StageTrace(
        stage="architect",
        provider=router.get_provider_name("architect"),
        model=router.get_model_name("architect"),
        start_monotonic=_t0,
        end_monotonic=time.monotonic(),
        duration_seconds=time.monotonic() - _t0,
        start_unix_ts=start_wall,
        end_unix_ts=end_wall,
        prompt_chars=len(prompt_sent),
        response_chars=len(result_text),
    ))
```

This feeds directly into the §14.6 stability metrics (latency, timeout rate, fallback rate) and the §12 rolling-window evaluation.

---

## 16. Claude Review of Section 14 (Archival, Non-Normative)
**Reviewer:** Claude Sonnet 4.6
**Date:** 2026-02-27

### What's Good

**§14.1 Model pinning** is correct and important. Uncontrolled CLI defaults drift between versions and invalidate benchmarks. Pinning explicit models per role is the only way to get reproducible results.

**§14.2 Timeout budgets** are reasonable starting points. 120s for light roles, 180s for heavy ones matches the current `timeout_seconds` defaults already in each client. Good that it says "revisit after each benchmark cycle" — these numbers will need tuning once data exists.

**§14.4 Output quality guardrails** are the right direction but need an implementation note. "Reject generation output when it contains reasoning/prose artifacts" implies a validation step between the LLM call and file write that doesn't exist yet. The current `_strip_leading_prose()` in `executor.py` strips it rather than rejecting — stripping is safer since rejection would trigger a retry, adding latency. Clarify whether the intent is strip-then-accept or reject-and-retry.

**§14.6 Stability metrics** map directly onto the `StageTrace` schema from §15.2 — if that's implemented, these metrics fall out for free.

### Issues

**§14.3 Retry-then-fallback is not implemented.** The current clients raise `LLMError` or `LLMTimeoutError` on failure and the orchestrator does not catch these with a retry loop — it lets them propagate. Implementing this requires either a retry wrapper at the router level or explicit try/except in each stage of the orchestrator. This is non-trivial and should be a tracked work item, not a policy statement.

**§14.5 Prompt size guardrail is not implemented.** `GeminiCLIClient` has a `max_prompt_chars` cap that raises `LLMError` on overflow, but no other client enforces this and there is no automatic context-reduction fallback. This needs a concrete implementation plan: where does the cap live (router level or per-client), and what triggers the `_extract_api_surface()` reduction path?

**§14.7 is too vague for execution.** "Tune route/model/concurrency immediately for that role" on a Red outcome is not actionable without defining what tuning options exist per role and in what order to try them (e.g. lower concurrency first, then downgrade model, then switch provider).

### Verdict

**APPROVE §14 as a policy document.** Two items need follow-up implementation tickets before they can be considered done: §14.3 retry/fallback logic and §14.5 prompt-size auto-reduction. Track these explicitly in the checklist in §8.
