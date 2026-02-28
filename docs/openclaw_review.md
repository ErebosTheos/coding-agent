# OpenClaw Integration Review

**Date:** 2026-02-27  
**Source Reviewed:** `openclaw/openclaw`  
**Snapshot:** `f943c76cde0030ee51c205ced64850a1a261962c`

## Scope
Review OpenClaw patterns that can improve this coding agent for speed, accuracy, and operational reliability.

## Current Repo Fit
This repo already has:
- role-based provider routing via `LLMRouter`
- streaming plan/architect/execute path
- stage fallback behavior in orchestrator

Main missing pieces vs OpenClaw:
- robust retry + structured fallback policy
- context pruning/compaction for long prompts
- standardized health/doctor diagnostics
- deeper telemetry for policy tuning

## Highest-Value Integrations

## 1) Router-Level Retry + Model/Provider Failover
**Why:** Biggest reliability gain for minimal architecture change.  
**OpenClaw pattern:** request retry policy + model failover after profile/provider failure.  
**Apply here:** add retry/fallback wrapper in `src/codegen_agent/llm/router.py`, used by orchestrator stage calls.

Implementation direction:
- retry only transient errors (`timeout`, `429`, empty output, transient CLI exit)
- bounded attempts with jittered backoff
- role fallback chain (`primary -> fallback provider/model`)
- record retry/fallback reason in stage telemetry

## 2) Prompt Pruning for Executor/Healer
**Why:** Improves speed and reduces context-window failures in long runs.  
**OpenClaw pattern:** session pruning (trim old heavy tool outputs while preserving key context).  
**Apply here:** preprocess large prompt context in executor/healer before LLM call.

Implementation direction:
- soft trim oversized sections first
- hard clear low-signal historic blobs when threshold exceeded
- preserve latest source, failing test output, and contracts

## 3) Queue/Lane Concurrency Guard
**Why:** Prevents run collisions and unstable behavior under multiple runs.  
**OpenClaw pattern:** per-session lane + global concurrency cap.  
**Apply here:** lightweight run queue in orchestrator entry path.

Implementation direction:
- workspace/session lane lock
- global max concurrent pipeline runs
- avoid interleaved writes/checkpoint races

## 4) `health` + `doctor` Commands
**Why:** Reduces setup/debug time and benchmark friction.  
**OpenClaw pattern:** structured diagnostics + safe repair paths.  
**Apply here:** add CLI diagnostics for environment, binaries, auth/config, workspace state, and optional repair hints.

Implementation direction:
- `codegen health`: fast read-only checks
- `codegen doctor`: deep checks + optional repair suggestions
- include checks for CLI binaries (`claude`, `gemini`, `codex`), `.env`, provider mappings, and state integrity

## 5) Telemetry and Usage Surfaces
**Why:** Needed to tune performance policy objectively.  
**OpenClaw pattern:** usage/health surfaces and model status views.  
**Apply here:** implement per-stage trace schema and aggregate reports.

Implementation direction:
- per-stage timing, provider/model, prompt/response chars, retries/fallbacks
- rolling-window metrics for Green/Amber/Red policy
- benchmark output artifacts under `.codegen_agent/`

## Medium-Value Integrations

## 6) Skill Gating Metadata
**Why:** Prevents skill/tool misfires when dependencies are absent.  
**OpenClaw pattern:** skill eligibility filters (`requires.bins`, `requires.env`, `requires.config`).  
**Apply here:** gate local skills by runtime readiness before exposure.

## 7) Tool Policy Tiers + Optional Sandbox/Elevated Mode
**Why:** Better safety boundaries without blocking capability.  
**OpenClaw pattern:** separate sandbox runtime, tool allow/deny, elevated execution policy.  
**Apply here:** add policy layer for risky tool usage and optional isolated execution.

## Lower Priority / Non-Goals
- channel/messaging integrations
- telephony/voice paths
- social platform plugin surface

These are out of scope for a coding-agent-first system.

## Recommended Adoption Order
1. Router retry/failover
2. Prompt pruning
3. Telemetry completion
4. Queue/lane concurrency control
5. `health`/`doctor`
6. Skill gating
7. Tool policy tiers/sandbox

## Notes for This Repo
- Keep CLI-first execution (`claude_cli`, `gemini_cli`, `codex_cli`) as already defined in plan docs.
- Implement failover and pruning before aggressive throughput tuning, otherwise benchmarks will be noisy and unstable.
- Treat `ollama` as optional weak-model lane only after telemetry and quality gates are in place.

## Reference Material
- `docs/concepts/retry.md`
- `docs/concepts/model-failover.md`
- `docs/concepts/session-pruning.md`
- `docs/concepts/queue.md`
- `docs/cli/doctor.md`
- `docs/cli/health.md`
- `docs/tools/skills.md`
- `docs/gateway/sandbox-vs-tool-policy-vs-elevated.md`
- `docs/concepts/usage-tracking.md`
