# Codegen Agent V2 — Decision-Complete Architecture Spec

## Summary

This V2 spec replaces ambiguity with enforceable behavior and compatibility guarantees.
It is explicitly designed to fit the current dashboard/event model while fixing V1 failure patterns.

Locked decisions:

1. Gate policy: **Platform Hard-Stop**
   - Layers 1-6 are hard-stop gates.
   - Layers 7-8 never silently continue; they transition to `NEEDS_REVIEW`.
2. Rollout: **Adapter + Shadow Mode**
   - V1 remains default until V2 passes benchmark gates.
   - V2 runs shadow comparisons and publishes per-brief deltas before cutover.

---

## Scope

1. Define the V2 architecture and execution policy.
2. Keep V1 compatibility explicit and measurable.
3. Specify implementation-facing interfaces, states, events, recovery rules, and acceptance tests.
4. This document defines behavior only; it does not require code edits by itself.

---

## 1. System Overview

```
Brief
  |
  v
[Planner V2]       brief -> ProductPlan
  |
  v
[Architect V2]     ProductPlan -> LayeredPlan (contracts + validation specs)
  |
  v
[LayeredExecutor]  execute layer-by-layer with gate enforcement
  |   Layers 1-6: hard-stop on validation failure
  |   Layers 7-8: NEEDS_REVIEW on validation failure
  |
  v
[SystemicHealer]   cluster failures by root cause, fix once per cluster
  |
  v
[QAAuditor V2]     score against ProductPlan modules and brief requirements
  |
  v
[Report + Dashboard]
```

---

## 2. Gating Policy (Platform Hard-Stop)

### 2.1 Gate Invariant

The gate invariant is:

- Platform-critical layers (`foundation`, `domain_models`, `schemas`, `repositories`, `services`, `routers`) must pass before next layer starts.
- Non-platform layers (`tests`, `frontend`) do not allow silent pass-through:
  - Failure transitions to `NEEDS_REVIEW`.
  - Pipeline does not emit `COMPLETE` while unresolved.

### 2.2 Layer Classes

1. Platform-critical layers: 1-6
2. Review-required layers: 7-8

### 2.3 Gate Loop

```python
MAX_LAYER_RETRIES = 3

async def execute_layer(layer: Layer, context: list[GeneratedFile]) -> LayerResult:
    files = await generate_layer(layer, context)
    write_files(files)

    for attempt in range(1, MAX_LAYER_RETRIES + 1):
        result = await validate_layer(layer, files)
        if result.ok:
            return LayerResult(status="passed", files=files, attempts=attempt)

        if attempt < MAX_LAYER_RETRIES:
            files = await fix_layer_inline(layer, files, result.failures)
            write_files(files)
            continue

        if layer.validation.on_failure == "hard_stop":
            raise LayerGateError(layer=layer.name, failures=result.failures)

        # on_failure == "needs_review"
        raise NeedsReviewError(layer=layer.name, failures=result.failures)
```

### 2.4 Gate Outcome Rules

1. `hard_stop` failure -> state `LAYER_FAILED` -> no further layer generation.
2. `needs_review` failure -> state `NEEDS_REVIEW` -> no `COMPLETE` until retried or resolved.
3. Gate state and failures must be persisted to `.v2-state.json` immediately.

---

## 3. Validation Tiers, Prerequisites, and Fallback Behavior

### 3.1 Validator Prerequisites

Required tooling for V2 validators:

1. `pytest`
2. `pytest-timeout`
3. marker registration in `pytest.ini`:
   - `layer`
   - `unit`
   - `integration`
4. `alembic`

### 3.2 Validation Environment Errors

If a required tool or marker configuration is missing, validator must emit:

- `VALIDATION_ENV_ERROR`

This is not treated as product-code failure by default.
Handling is controlled by `env_error_policy` in `ValidationSpec`.

### 3.3 ValidationSpec

```python
@dataclass(frozen=True)
class ValidationSpec:
    tiers: list[str]                     # e.g. ["FastCheck", "UnitCheck"]
    timeout_seconds_key: str             # key in ValidationConfig
    on_failure: str                      # "hard_stop" | "needs_review"
    prerequisites: list[str]             # ["pytest", "pytest-timeout", "pytest_markers:layer"]
    env_error_policy: str                # "fail" | "needs_review"
```

### 3.4 Timeout Configuration (No Hardcoded Literals)

```python
@dataclass(frozen=True)
class ValidationConfig:
    fast_check_timeout_s: int = 10
    unit_check_timeout_s: int = 30
    integration_check_timeout_s: int = 60
    alembic_check_timeout_s: int = 20
    a11y_check_timeout_s: int = 30
    api_binding_check_timeout_s: int = 30
    full_suite_timeout_s: int = 120
```

### 3.5 Tier Assignment per Layer

| Layer | Tier(s) | On Failure | Timeout Key |
|---|---|---|---|
| 1 Foundation | FastCheck | hard_stop | fast_check_timeout_s |
| 2 Domain Models | FastCheck + AlembicCheck | hard_stop | alembic_check_timeout_s |
| 3 Schemas | FastCheck | hard_stop | fast_check_timeout_s |
| 4 Repositories | FastCheck + UnitCheck | hard_stop | unit_check_timeout_s |
| 5 Services | FastCheck + UnitCheck | hard_stop | unit_check_timeout_s |
| 6 Routers/API | FastCheck + IntegrationCheck | hard_stop | integration_check_timeout_s |
| 7 Tests | FullSuite | needs_review | full_suite_timeout_s |
| 8 Frontend | A11yCheck + APIBindingCheck | needs_review | a11y_check_timeout_s / api_binding_check_timeout_s |

---

## 4. Public Interfaces and Type Contracts

### 4.1 Planning Contracts

```python
@dataclass(frozen=True)
class ProductModule:
    name: str
    description: str
    user_roles: list[str]
    depends_on: list[str]
    priority: int

@dataclass(frozen=True)
class ProductPlan:
    stack: str
    modules: list[ProductModule]
    entities: list[str]
    api_prefix: str
    brief_hash: str
```

### 4.2 File Contracts

```python
@dataclass(frozen=True)
class PythonContract:
    file_path: str
    purpose: str
    layer: int
    exports: list[str]
    imports_from: dict[str, list[str]]
    signature_hints: list[str]

@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    auth_required: bool
    allowed_roles: list[str]
    request_schema: str
    response_schema: str

@dataclass(frozen=True)
class RouteContract:
    file_path: str
    purpose: str
    layer: int
    routes: list[RouteSpec]

@dataclass(frozen=True)
class MigrationContract:
    file_path: str
    purpose: str
    layer: int
    tables: list[str]
    depends_on_revision: str | None

@dataclass(frozen=True)
class FrontendContract:
    file_path: str
    purpose: str
    layer: int
    api_endpoints_used: list[str]
    aria_requirements: list[str]
    auth_required: bool
    allowed_roles: list[str]

FileContract = PythonContract | RouteContract | MigrationContract | FrontendContract
```

### 4.3 Failure + Healing Contracts

```python
@dataclass(frozen=True)
class FailureRecord:
    failure_id: str                     # required; deterministic hash of normalized failure
    kind: str
    file: str
    symbol: str | None
    test_node: str | None
    message: str
    raw: str

@dataclass(frozen=True)
class FailureCluster:
    cluster_id: str
    root_cause_type: str                # e.g. "missing_symbol", "url_mismatch", "fallback_single"
    failure_ids: list[str]
    affected_files: list[str]
    fix_strategy: str                   # "deterministic" | "llm"
```

### 4.4 Runtime State + Event Contracts

```python
@dataclass(frozen=True)
class PipelineState:
    project_id: str
    brief_hash: str
    stack_profile: str
    stack_profile_version: str
    plan_digest: str
    contracts_digest: str
    layers_passed: list[int]
    current_layer: int | None
    status: str
    needs_review_reason: str | None
    started_at: float
    updated_at: float

@dataclass(frozen=True)
class PipelineEvent:
    type: str
    project_id: str
    ts: float
    phase: str
    layer_index: int | None
    status: str
    details: dict[str, Any]
```

---

## 5. Systemic Healer — Failure Assignment Invariant

### 5.1 Invariant

1. Each `FailureRecord` belongs to exactly one `FailureCluster`.
2. Assignment order is deterministic:
   - symbol cluster
   - URL mismatch cluster
   - fallback singleton cluster
3. A file cannot be patched more than once in a single heal cycle for the same root cause.

### 5.2 Deterministic Assignment Algorithm

```python
def cluster_failures(failures: list[FailureRecord]) -> list[FailureCluster]:
    assigned: set[str] = set()              # failure_id set
    clusters: list[FailureCluster] = []

    # 1) symbol clusters
    for key, group in group_by_symbol(failures).items():
        ids = [f.failure_id for f in group if f.failure_id not in assigned]
        if not ids:
            continue
        clusters.append(build_symbol_cluster(key, ids, group))
        assigned.update(ids)

    # 2) URL mismatch clusters
    for pattern, group in group_by_url(failures).items():
        ids = [f.failure_id for f in group if f.failure_id not in assigned]
        if not ids:
            continue
        clusters.append(build_url_cluster(pattern, ids, group))
        assigned.update(ids)

    # 3) fallback singletons
    for f in failures:
        if f.failure_id in assigned:
            continue
        clusters.append(build_singleton_cluster(f))
        assigned.add(f.failure_id)

    return clusters
```

### 5.3 Patch Dedup Rule

During one heal cycle:

1. `(file_path, root_cause_type)` is a unique patch key.
2. If a later cluster attempts the same key, skip and log `patch_dedup_skipped`.

---

## 6. Runtime Lifecycle, Locking, and Resume Drift Protection

### 6.1 Canonical Lifecycle States

1. `QUEUED`
2. `PLANNING`
3. `ARCHITECTING`
4. `EXECUTING`
5. `HEALING`
6. `AUDITING`
7. `COMPLETE`
8. `LAYER_FAILED`
9. `NEEDS_REVIEW`
10. `FAILED`

State persistence rule:

- Write `.v2-state.json` after every state transition.

### 6.2 Transition Table

| Current | Allowed Next | Terminal |
|---|---|---|
| QUEUED | PLANNING, FAILED | No |
| PLANNING | ARCHITECTING, FAILED | No |
| ARCHITECTING | EXECUTING, FAILED | No |
| EXECUTING | HEALING, LAYER_FAILED, NEEDS_REVIEW, FAILED | No |
| HEALING | AUDITING, LAYER_FAILED, NEEDS_REVIEW, FAILED | No |
| AUDITING | COMPLETE, NEEDS_REVIEW, FAILED | No |
| COMPLETE | (none) | Yes |
| LAYER_FAILED | (none) | Yes |
| NEEDS_REVIEW | (retry -> EXECUTING/HEALING/AUDITING) | Yes (for auto-run) |
| FAILED | (none) | Yes |

### 6.3 Lock Semantics

Lock file: `{workspace}/.v2.lock`

Required lock payload:

1. `pid`
2. `process_start_time`
3. `cmd_fingerprint`
4. `lock_nonce`
5. `current_layer`

Stale-lock decision:

1. PID must be alive.
2. Process start time must match lock.
3. Command fingerprint must match lock.

If any check fails, treat as stale lock, delete lock, and resume safely.

**PID-only validation is forbidden.**

### 6.4 Resume Drift Protection

On resume, validate all of:

1. `brief_hash`
2. `plan_digest`
3. `contracts_digest`
4. `stack_profile_version`

If any mismatch:

1. Do not resume mid-layer.
2. Restart from `ARCHITECTING` by default.
3. Optional manual override can force full restart from `PLANNING`.

---

## 7. Dashboard/Event Compatibility Contract

### 7.1 V1-Compatible Events (must remain usable)

1. `project_started`
2. `state_change`
3. `build_complete`
4. `project_failed`

### 7.2 V2 Additive Events

1. `layer_started`
2. `layer_passed`
3. `layer_failed`
4. `needs_review`
5. `validation_env_error`

V2 events are additive only; existing V1 consumers must not break.

### 7.3 Event Payload Schema

All events must include:

1. `type: str`
2. `project_id: str`
3. `ts: float`
4. `phase: str`
5. `layer_index: int | null`
6. `status: str`
7. `details: object`

### 7.4 Event Mapping Rules

1. `layer_failed` -> must be followed by `state_change: LAYER_FAILED`.
2. `needs_review` -> must be followed by `state_change: NEEDS_REVIEW`.
3. `build_complete` must never fire from `NEEDS_REVIEW` or `LAYER_FAILED`.

---

## 8. Stack Profiles

Stack profiles are versioned and tested recipes, not arbitrary prompt hints.

### Rules

1. Every known pattern has `rationale` and `since`.
2. Every forbidden pattern has `rationale` and `replacement`.
3. Route-dependent settings (like OAuth token URL) are derived from route contracts, not hardcoded.
4. Profile changes require fixture-project validation in CI.

---

## 9. Rollout Strategy — Adapter + Shadow Mode

### 9.1 Execution Modes

1. Production mode:
   - V1 handles live project execution.
2. Shadow mode:
   - V2 runs the same benchmark briefs in parallel.
   - V2 outputs reports only; no production default switch.

### 9.2 Shadow Evaluation Outputs

Per brief:

1. QA score delta (`v2 - v1`)
2. Import errors first run
3. Alembic first-run pass/fail
4. Healing rounds to stable
5. Runtime and call-count deltas
6. Layer failure and `NEEDS_REVIEW` rates

Publish output to:

- `eval-results/YYYY-MM-DD.md`

### 9.3 Cutover Trigger

Set V2 as default only when benchmark gates pass for 3 consecutive runs:

1. QA score B1-B3 average >= 90
2. QA score B4-B5 average >= 82
3. Import errors first run == 0 for B1-B3
4. Alembic first run == true for all briefs
5. No regression on B1-B2 vs V1 (`qa_delta >= -2`)

### 9.4 Rollback Trigger

Revert default to V1 immediately if either condition is met after cutover:

1. Two consecutive benchmark regressions against any gate.
2. Production run shows repeated `LAYER_FAILED` or `NEEDS_REVIEW` spikes above configured SLO.

---

## 10. Evaluation Harness

### 10.1 Benchmark Suite

| ID | Brief | Expected Nodes | Complexity |
|---|---|---|---|
| B1 | Simple REST API (tasks app) | 15-20 | Low |
| B2 | Auth + RBAC app (2 roles) | 25-35 | Medium |
| B3 | LMS with tests + grading | 50-65 | High |
| B4 | Multi-tenant SaaS skeleton | 70-90 | Very High |
| B5 | Drishtikon Foundation (full brief) | 90-120 | Extreme |

### 10.2 Benchmark Result Contract

```python
@dataclass(frozen=True)
class BenchmarkResult:
    brief_id: str
    agent_version: str
    qa_score: float
    import_errors_on_first_run: int
    manual_fixes_needed: int
    healing_passes_to_stable: int
    alembic_works_first_run: bool
    test_pass_rate: float
    wall_clock_seconds: float
    llm_calls: int
    layer_failures: int
    needs_review_count: int
```

### 10.3 Harness Command

```bash
python -m codegen_agent_v2.eval.harness --suite all --runs 3 --compare v1
```

---

## 11. Acceptance Scenarios (Implementation Verification)

1. Gate semantics:
   - Layer 4 fails validation 3 times -> state `LAYER_FAILED`; Layer 5 generation never starts.
2. Review path:
   - Layer 8 A11y failure -> state `NEEDS_REVIEW`; `build_complete` is not emitted.
3. Cluster exclusivity:
   - One failure matching multiple heuristics -> assigned to exactly one cluster.
4. Stale lock:
   - Reused PID with mismatched process start time -> treated as stale lock and recovered.
5. Resume drift:
   - `contracts_digest` mismatch after restart -> forced restart from `ARCHITECTING`.
6. Validator env error:
   - Missing `pytest-timeout` -> `VALIDATION_ENV_ERROR` emitted; handling follows `env_error_policy`.
7. Event compatibility:
   - Existing dashboard still renders V1 events; additive V2 events do not break UI consumers.

---

## 12. Build Order

### Phase 1 — Core Contracts

1. `models.py`:
   - ProductPlan, LayeredPlan, ValidationSpec, FailureRecord, FailureCluster, PipelineState, PipelineEvent
2. `stack_profiles.py`:
   - versioned profile definitions + rationale metadata
3. `failure_taxonomy.py`:
   - failure normalization + deterministic ID generation

### Phase 2 — Planner/Architect

1. `planner_v2.py`: brief -> ProductPlan
2. `architect_v2.py`: ProductPlan -> LayeredPlan with typed contracts

### Phase 3 — Executor + Validators

1. `validators/fast_check.py`
2. `validators/unit_check.py`
3. `validators/integration_check.py`
4. `validators/alembic_check.py`
5. `validators/a11y_check.py`
6. `validators/api_binding_check.py`
7. `layered_executor.py`: gate loop + state transitions + event emission

### Phase 4 — Healing + QA

1. `failure_clusterer.py`: deterministic assignment order and exclusivity
2. `systemic_healer.py`: one fix per cluster with patch dedup
3. `qa_auditor_v2.py`: module scoring against ProductPlan

### Phase 5 — Integration + Rollout

1. `orchestrator_v2.py`
2. `adapters/base.py`, `adapters/v1_adapter.py`, `adapters/v2_adapter.py`
3. dashboard wiring via `AGENT_VERSION`
4. shadow mode benchmark reporting

### Phase 6 — Evaluation

1. `eval/harness.py`
2. `eval/report.py`
3. `eval/briefs/b1..b5`

---

## 13. File Layout

```
src/
  codegen_agent/                 # V1 (default until cutover)
  codegen_agent_v2/
    models.py
    stack_profiles.py
    failure_taxonomy.py
    planner_v2.py
    architect_v2.py
    layered_executor.py
    failure_clusterer.py
    systemic_healer.py
    qa_auditor_v2.py
    orchestrator_v2.py
    validators/
      fast_check.py
      unit_check.py
      integration_check.py
      alembic_check.py
      a11y_check.py
      api_binding_check.py
    adapters/
      base.py
      v1_adapter.py
      v2_adapter.py
    eval/
      harness.py
      report.py
      briefs/
        b1_tasks_api.txt
        b2_auth_rbac.txt
        b3_lms.txt
        b4_multitenant_saas.txt
        b5_drishtikon_full.txt
```

---

## 14. Assumptions and Defaults

1. V1 remains default runtime until V2 benchmark gates pass.
2. Benchmark gating uses the 5-brief suite (B1-B5) defined above.
3. Hard-stop applies only to platform-critical layers (1-6).
4. `NEEDS_REVIEW` is terminal for automated flow but retryable by an operator.
5. Determinism and debuggability are prioritized over maximum throughput.
