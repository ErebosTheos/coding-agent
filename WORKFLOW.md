# 🎯 Dual-Agent Operational Workflow

**Document:** Complete operational workflow for the Senior Autonomous Developer Agent  
**Last Updated:** February 23, 2026  
**Status:** Design Complete, Operational Hardening Required

---

## High-Level Overview: The Three Gates

```
HUMAN BRIEFING
    ↓
ANALYSIS PHASE (Gemini architect)
├─ Decompose requirement → Dependency Graph
├─ Define node contracts + DoD checklist
└─ Publish to queue
    ↓
PARALLEL BUILD PHASE (Codex executor + Gemini auditor)
├─ Node 1..N RED tests in parallel ✓
├─ Node 1..N implementation in parallel ✓
└─ Pre-audit automation (lint/type/coverage/contract compliance)
    ↓
AUDIT GATE (Gemini gatekeep → accept/reject/request changes)
    ↓
LEVEL 2 VALIDATION (Global DoD: tests + semantic integrity + perf)
    ↓
MERGE & SHIP
```

---

## Phase 1: INGESTION & ANALYSIS

**Owner:** Gemini (Chief Architect & Senior Reviewer)  
**Duration:** ~10-20 minutes  
**Parallelizable:** No (downstream blocker)

### Input
- User requirement (natural language or structured brief)
- Codebase summary (architecture overview, existing constraints)

### Process

1. **Decompose requirement into atomic work units**
   - Identify logical nodes based on scope size:
     - Small scope: 1-5 nodes
     - Medium scope: 6-12 nodes
     - Large scope: 10-20 nodes
   - Each node has single purpose and single public interface
   - No hidden dependencies

2. **Define Dependency Graph (DAG)**
   - Create edges showing parent→child relationships
   - Ensure no circular dependencies
   - Mark "Contract Nodes" (API/interface changes) as parents

3. **Publish Node Contracts** (for each node)
   - Purpose (1–3 sentences)
   - Inputs (types + examples)
   - Outputs (types + examples)
   - Public API surface (functions/classes/endpoints)
   - Invariants (statements that must always hold)
   - Errors (error codes, messages, when thrown)
   - Edge cases (tricky conditions)
   - Performance budget (if relevant)
   - Security/privacy constraints
   - DoD Checklist (binary acceptance criteria)

4. **Define Shared Contracts**
   - Error taxonomy (all error codes + meanings)
   - Data schemas (types, validation rules)
   - API interface definitions

5. **Publish Change Window**
   - "Contracts are FROZEN during Phase 2"
   - Any contract changes require Phase 4c (Change Request)

6. **Emit Machine-Readable Planning Artifacts (Required)**
   - `dependencies.json` MUST validate against the dependency graph schema
   - Every `nodes/*/contract.md` MUST have a matching `nodes/*/contract.json`
   - A planning run is invalid unless all schemas pass local validation

7. **Handoff Verification (Phase 1.6)**
   - Generate a `handoff.checksum` containing hashes of all frozen contracts.
   - Any modification to these files during Phase 2 will invalidate the Audit Gate.

### Artifacts Generated

```
.senior_agent/
├── handoff.checksum           # SHA-256 hashes of all frozen contracts
├── dependencies.json          # DAG: node IDs + edges + rationale
├── shared_contracts.md        # Error taxonomy, schemas, interfaces
├── global_dod.md             # Level 2 acceptance criteria
└── nodes/
    ├── n0_repo_tooling/
    │   └── contract.md
    ├── n1_error_taxonomy/
    │   └── contract.md
    ├── n2_shared_types/
    │   └── contract.md
    └── ... (n3–n17)
```

### Quality Gate

✅ **Contracts must be concrete enough that Codex cannot misinterpret them.**
- Each contract includes 2+ worked examples
- All error cases explicitly defined
- Invariants testable via property tests

---

## Phase 2a: RED TEST WRITING

**Owner:** Codex (Lead Developer / Coder)  
**Duration:** ~20-30 minutes per node (parallel safe)  
**Parallelizable:** YES (after Phase 1 contracts frozen)

### Input
- Node Contract from Phase 1
- Shared Contracts (error taxonomy, schemas)

### Process

1. **Write Minimum Test Set**

   ```python
   # tests/test_n{X}.py
   
   # Contract tests: input → output mapping
   def test_happy_path_example_1():
       """Verify primary use case from contract."""
       input_data = {...}  # from contract
       output = function(input_data)
       assert output == expected  # from contract
   
   def test_happy_path_example_2():
       """Verify second worked example."""
       ...
   
   # Error tests: invalid input → correct error code
   def test_invalid_input_raises_contract_error():
       """Verify error taxonomy compliance."""
       with pytest.raises(ValueError) as exc_info:
           function(invalid_input)
       assert "ERROR_CODE_FROM_TAXONOMY" in str(exc_info.value)
   
   # Invariant tests: property always true
   def test_invariant_never_negative_balance():
       """Verify: balance >= 0 always holds."""
       for _ in range(100):
           service.debit(random.randint(1, 1000))
           assert service.balance >= 0
   
   # Golden fixtures: realistic data
   @pytest.mark.parametrize("dataset", [
       load_golden_fixture("production_like_1"),
       load_golden_fixture("production_like_2"),
   ])
   def test_with_production_like_dataset(dataset):
       output = function(dataset)
       assert output.is_valid()
   ```

2. **Validate tests fail first (RED) for changed contract clauses**
   ```bash
   pytest tests/test_n{X}.py -v
   # Expected: Newly added or modified contract tests FAIL before implementation
   ```

3. **Tag each test with contract clause**
   ```python
   def test_happy_path_example_1():
       """Maps to contract: Outputs section, example 1."""
       ...
   ```

### Artifacts Generated

```
.senior_agent/nodes/n{X}/
├── contract.md
└── tests/
    └── test_n{X}.py          # All tests RED (failing)
```

### Quality Gate

✅ **Changed-contract tests must be RED.**
- Existing unaffected tests may remain GREEN.
- Any newly introduced contract test that is GREEN before implementation is a process violation.

---

## Phase 2b: GREEN IMPLEMENTATION

**Owner:** Codex (Lead Developer / Coder)  
**Duration:** ~30-60 minutes per node (parallel safe)  
**Parallelizable:** YES (after Phase 1 contracts frozen)

### Input
- Node Contract from Phase 1
- RED tests from Phase 2a
- Codebase context (Symbol Graph, style guidelines)

### Process

1. **Implement minimal functionality to pass RED tests**
   - No extra features beyond contract
   - No public APIs beyond contract
   - Keep internal details private

2. **All tests must turn GREEN**
   ```bash
   pytest tests/test_n{X}.py -v
   # Expected: All tests PASS
   
   pytest tests/test_n{X}.py --cov=src/node_n{X} --cov-report=term
   # Expected: Coverage > 80%
   ```

3. **Run pre-audit automation**
   ```bash
   ruff check src/node_n{X}/
   # Expected: PASS
   
   mypy src/node_n{X}/ --strict
   # Expected: No type errors
   
   pylint src/node_n{X}/
   # Expected: rating >= 8.0
   ```

4. **Document contract mapping**
   ```json
   {
     "node_id": "n4_domain_services",
     "contract_mapping": {
       "test_charge_happy_path": "Maps to Output section, example 1",
       "test_overcharge_raises_error": "Maps to Errors section, insufficient_funds",
       "test_invariant_balance_never_negative": "Maps to Invariants section, item 1"
     },
     "coverage": 85,
     "lint_status": "pass",
     "type_check_status": "pass"
   }
   ```

5. **Write release note** (5–10 lines)
   ```
   ## N4: Domain Services (v1.0)
   
   Implements core business logic for the Account domain:
   - Charge operation with balance enforcement
   - Concurrent transaction safety via locking
   - Error taxonomy compliance for all failure modes
   
   Tests: 8 unit tests, 85% coverage
   API: charge(amount: float) -> Transaction, balance() -> float
   ```

### Key Rules

- **No contract changes.** If needed → Phase 4c (Change Request)
- **No extra public APIs.** Only what contract specifies
- **Tests are the spec.** Implementation must satisfy all RED tests

### Artifacts Generated

```
.senior_agent/nodes/n{X}/
├── contract.md
├── tests/
│   └── test_n{X}.py              # All tests GREEN
├── implementation/
│   └── n{X}_domain_service.py    # Minimal impl
├── pre_audit.json                # lint/type/coverage results
└── release_note.md               # 5–10 lines
```

### Quality Gate

✅ **All tests GREEN, coverage >80%, lint/type clean, no contract violations.**

---

## Phase 3: AUDIT GATE

**Owner:** Gemini (Chief Architect & Senior Reviewer)  
**Duration:** ~10-15 minutes per node (batch all nodes first)  
**Parallelizable:** YES (but recommend batching all audits together)

### Input
- Codex implementation + pre-audit results (from Phase 2b)
- Node Contract (from Phase 1)
- Shared Contracts (error taxonomy, schemas)

### Process (Diff-Based Only)

Gemini checks **only what changed**, not line-by-line:

1. **Contract Compliance**
   - ✅ Does output shape match contract?
   - ✅ Are all Inputs tested?
   - ✅ Are all defined Errors in taxonomy?
   - ✅ Do function signatures match contract?

2. **Invariant Validation**
   - ✅ Is there a test for each invariant?
   - ✅ Do tests actually verify the invariant? (not just assert True)

3. **Edge Cases & Adversarial Pack**
   - ✅ Compare against Adversarial Pack (boundary values, null/empty, overlong)
   - ✅ Are edge cases tested that aren't covered by contract examples?

4. **Security & Privacy** (if applicable)
   - ✅ Permissions checked at boundaries?
   - ✅ Sensitive data not logged/cached?
   - ✅ Input validation enforced?

5. **Deterministic Audit Scoring (Required)**
   - ✅ Contract clause coverage = 100% (every clause mapped to at least one test)
   - ✅ Invariant coverage = 100% (each invariant has at least one explicit test)
   - ✅ Pre-audit checks pass (`lint`, `types`, node test command)
   - ✅ No unresolved HIGH severity findings

### Outcome Options

#### ACCEPT ✅
```
ACCEPT: N4 (Domain Services)

Contract compliance: ✅ Full compliance
- All inputs tested against examples
- All error codes match taxonomy
- Invariants: 3/3 tested

Edge cases: ✅ Complete
- Boundary tests for amounts [0, 1, 1_000_000]
- Concurrent operation safety validated
- Null input rejected correctly

Security: ✅ Approved
- Input validation enforced at entry
- Sensitive data not cached
- Error messages don't leak internals

Status: READY FOR MERGE
```

#### REJECT ❌
```
REJECT: N4 (Domain Services)

Defect 1 - Missing error case:
  Location: test_overcharge_raises_error
  Input: charge(quantity=-5, price=10)
  Expected: RuntimeError("quantity must be >= 0")
  Actual: Silently returns -50
  Contract clause violated: "Errors" section, line 3
  Minimal repro: Call charge(-5, 10); assert output < 0 fails

Defect 2 - Partial invariant coverage:
  Invariant: "balance must never go negative"
  Issue: Has tests for single-threaded debit, but NO TEST for concurrent operations
  Expected test: test_concurrent_operations_maintain_invariant()
  Severity: HIGH (invariant could be violated at runtime)

Required fixes (BLOCKING):
1. Add input validation: quantity >= 0, raise ValueError
2. Add concurrent operation test

Suggested (non-blocking):
- Parameterize amount tests: [0, 1, 100, 1_000_000]
- Add performance test: 10k operations < 100ms

Resubmit when fixes applied.
```

#### CHANGE REQUEST 🔄
```
CHANGE REQUEST: N4 (Domain Services)

What changes:
- Contract Invariant #2: Add "transaction_timeout_seconds" parameter
- Required downstream: N5 (Policies) must enforce timeout rules

Why needed:
- N4 audit revealed race condition risk in long-lived transactions
- Policies need explicit timeout control

Downstream impact:
- N5 tests must include timeout scenarios
- N10 (API adapter) must pass timeout from HTTP headers

Migration plan:
- v1.0 → v1.1 (backward compatible; timeout defaults to 60s)
- N5 must add test_timeout_enforcement_blocks_stale_transaction()

Timeline: Blocker for N5 merge

New version: N4 v1.1 (Contract updated)
```

---

## Phase 4: CHANGE REQUEST LOOP (Optional)

**Owner:** Codex + Gemini (collaborative)  
**Duration:** ~20-30 minutes per round (assume 1-2 rounds)  
**Parallelizable:** No (blocks Phase 5 until resolved)

### If Codex receives REJECT

1. **Codex files Change Request reply (internal)**
   ```
   Status: ADDRESSING DEFECTS
   
   Defect 1 - Missing error case:
   - Fix: Added validation `if quantity < 0: raise ValueError(...)`
   - Test: test_negative_quantity_raises_error() added
   - Verification: Pre-audit confirms test GREEN
   
   Defect 2 - Concurrent operations:
   - Fix: Added threading.Lock() in charge()
   - Test: test_concurrent_debit_maintains_invariant() added
   - Verification: Parameterized test with 100 concurrent threads, all pass
   
   Resubmitting for audit.
   ```

2. **Return to Phase 3 (Audit Gate) with updated code**

### If Codex receives CHANGE REQUEST

1. **Codex files Change Request acknowledgment + timeline**
   ```
   Status: CHANGE REQUEST ACKNOWLEDGED
   
   Required contract change:
   - N4 v1.0 → v1.1
   - Add parameter: transaction_timeout_seconds: int = 60
   - Update Invariant #2 to include timeout enforcement
   
   Proposed changes:
   - def charge(amount, quantity, timeout_seconds=60)
   - Raise TimeoutError if operation exceeds timeout
   
   New tests planned:
   - test_timeout_enforcement_blocks_stale_transaction()
   - test_default_timeout_is_60_seconds()
   
   Timeline: Ready for contract update, then 1h implementation
   Downstream: N5 + N10 depend on this; flagged as blocker
   ```

2. **Gemini updates Contract → v1.1**

3. **Codex goes back to Phase 2b with updated contract**

### Contract Versioning Policy (Required)
- **PATCH**: Internal clarification only, no API/behavioral change.
- **MINOR**: Backward-compatible API or behavior extension.
- **MAJOR**: Backward-incompatible change.
- Any MINOR/MAJOR change MUST include:
  - impacted node list
  - migration steps
  - rollback strategy
  - downstream test delta

---

## Phase 5: PARALLEL NODE MERGE

**Owner:** Orchestrator (automated, [MultiAgentOrchestrator](orchestrator.py#L88))  
**Duration:** ~2-5 minutes  
**Parallelizable:** YES (all nodes in parallel)

### Preconditions
- All parent nodes passed Phase 3 (Audit Gate)
- All child nodes passed Phase 3 (Audit Gate)
- No file ownership conflicts (if conflict exists, Gemini resolves)

### Node State Machine (Enforced)
Each node must progress through explicit states:
`planned -> red -> green -> pre_audit_passed -> audited -> merged`

Invalid transitions (must fail run):
- `planned -> green` (skips RED)
- `green -> merged` (skips audit)
- `audited -> planned` without recorded change request

### Process

1. **Dependency check:** All parents of node N passed audit ✓
2. **File ownership check:** No two nodes write same file ✓
3. **Contract check:** No conflicting public APIs ✓
4. **Merge:** Apply all node changes atomically

```python
# In orchestrator.py: _execute_dependency_graph()
if all_nodes_audit_passed:
    for node in ready_nodes:
        apply_node_files(node)
    if any_file_conflict_detected:
        raise ConflictError("See conflict_graph.json")
    else:
        commit_transaction()
```

### If Conflicts Detected
- Gemini resolves via `_resolve_dependency_graph_conflicts()`
- Creates merged node if needed (contract-first sequencing)
- Outputs conflict_graph.json for human review if unresolvable after 3 attempts

### Artifacts Generated

```
workspace/
├── src/
│   ├── node_n0_files/
│   ├── node_n1_files/
│   └── ... (all nodes merged)
└── tests/
    ├── test_n0.py
    ├── test_n1.py
    └── ... (all tests integrated)
```

---

## Phase 6: LEVEL 2 VALIDATION (Global DoD)

**Owner:** Orchestrator + Gemini (reviewer)  
**Duration:** ~5-10 minutes (can run in background)  
**Parallelizable:** Partially (run async, report sequentially)

### Process

1. **Full Integration Suite**
   ```bash
   pytest tests/ -v --cov=src/ --cov-report=term
   # Expected: 100% tests PASS, coverage > 80% workspace-wide
   ```

2. **Semantic Integrity Check**
   - Cross-node interface validation (Symbol Graph)
   - Type consistency across API boundaries
   - No orphaned dependencies
   - All imports resolve

3. **Performance Budget Validation** (if specified)
   ```bash
   pytest tests/perf/ --benchmark
   # Compare against contract budgets
   ```

4. **Gatekeeper Review** (LLM-based, final audit)
   - Semantic code quality (not lint, but logic)
   - "Does this code make sense?"
   - Final safety/security sweep
   - Suggests improvements (non-blocking)

### Outcome

- ✅ **PASS**: All checks green; proceed to Phase 7 (Ship)
- ⚠️ **WARN**: Minor issues detected; log but allow override
- ❌ **FAIL**: Critical regression; block merge, create rollback plan

---

## Phase 7: SHIP

**Owner:** Human (button press) or CI/CD automation  
**Duration:** ~1-5 minutes  
**Parallelizable:** No (final gate)

### Process

1. **Final approval** (human or CI gate)
2. **Commit to version control**
   ```bash
   git commit -m "Feature: $(plan.feature_name)
   
   Nodes merged:
   - N0: Repo tooling
   - N1: Error taxonomy
   - N2: Shared types
   
   Level 1 DoD: All nodes PASS
   Level 2 DoD: Integration suite PASS
   Gatekeeper: APPROVED
   "
   ```
3. **Deploy** (if applicable)
4. **Generate session report**
   ```json
   {
     "feature_name": "Logging Framework",
     "nodes_merged": 17,
     "total_time_minutes": 120,
     "parallel_gain": 3.2,
     "test_coverage": 86,
     "status": "SUCCESS"
   }
   ```

---

## Folder Structure for This Workflow

```
workspace/
.senior_agent/
├── dependencies.json                    # DAG: node IDs + edges
├── dependencies.schema.json             # DAG schema (validation)
├── shared_contracts.md                 # Error taxonomy, schemas
├── global_dod.md                       # Level 2 acceptance criteria
│
├── nodes/
│   ├── n0_repo_tooling/
│   │   ├── contract.md                 # Gemini publishes Phase 1
│   │   ├── contract.json               # Machine-readable contract
│   │   ├── tests/
│   │   │   └── test_n0.py             # Codex RED tests Phase 2a
│   │   ├── implementation/
│   │   │   └── n0_tooling.py          # Codex GREEN code Phase 2b
│   │   ├── pre_audit.json             # Codex pre-audit Phase 2b
│   │   ├── audit_return.md            # Gemini audit Phase 3
│   │   ├── audit_scorecard.json       # Deterministic audit metrics
│   │   └── release_note.md            # Codex release note Phase 2b
│   │
│   ├── n1_error_taxonomy/
│   │   ├── contract.md
│   │   ├── tests/test_n1.py
│   │   ├── implementation/n1_errors.py
│   │   ├── pre_audit.json
│   │   ├── audit_return.md
│   │   └── release_note.md
│   │
│   ├── n2_shared_types/
│   │   └── ... (same structure)
│   │
│   └── ... (n3 through n17, same pattern)
│
├── conflict_graph.json                 # If conflicts encountered (Phase 5)
└── session_report.json                 # After Phase 7 (Ship)
```

---

## Timing & Parallelization Strategy

### Sequential Blocks (Hard Dependencies)

```
Phase 1 (Analysis)           ← Gemini only, ~10-20 min
   ↓
[Phase 1 complete; contracts frozen]
   ↓
├─ Phase 2a (RED tests)      ← Codex parallel, ~20-30 min
├─ Phase 2b (GREEN code)     ← Codex parallel, ~30-60 min per node
└─ Phase 3 (Audit Gate)      ← Gemini batch, ~10-15 min per node
   ↓ [Recommend: batch all audits, then proceed]
├─ Phase 5 (Merge)           ← Orchestrator parallel, ~2-5 min
├─ Phase 6 (Level 2 DoD)     ← Orchestrator + Gemini, ~5-10 min
└─ Phase 7 (Ship)            ← Human gate, ~1-5 min
```

### Recommended Wave Schedule (17-node project)

```
Day 1 (45 min):
  Phase 1: Gemini creates DAG + contracts for N0-N17

Day 2 (2-3 hours):
  Phase 2a-2b: Parallel waves
    Wave 1 (N0-N2 foundations):      30-60 min
    Wave 2 (N3-N5 core):             30-60 min
    Wave 3 (N6-N8 ports):            30-60 min
    Wave 4 (N9-N11 adapters):        30-60 min
    Wave 5 (N12-N15 observability):  30-60 min
    Wave 6 (N16-N17 E2E + perf):     30-60 min
  Phase 3: Gemini audits all in batch (30-60 min)
  Phase 4 (if needed): Change requests loop (20-30 min per issue)

Day 3 (20 min):
  Phase 5: Merge all (2-5 min)
  Phase 6: Level 2 validation (5-10 min)
  Phase 7: Ship (1-5 min)

Total: ~3-4 hours for 17-node feature (highly parallel after Phase 1)
```

### Safe Parallelization Matrix

| Nodes | Safe to parallelize? | Reason |
|-------|----------------------|--------|
| N0-N2 | ✅ Fully | Foundation; no dependencies |
| N3-N5 | ✅ Fully | After N0-N2 done; independent logic |
| N6-N8 | ✅ Fully | After N2 schemas frozen; interface defs |
| N9-N11 | ✅ Fully | After N6-N8 done; adapter implementations |
| N12-N15 | ✅ Fully | Independent observability; can start early |
| N16-N17 | ❌ Sequential | E2E tests need all prior nodes; perf needs baseline |

---

## Integration with Current Codebase

Your `orchestrator.py` **already has:**

✅ Dependency Graph execution → `_execute_dependency_graph()`  
✅ Conflict resolution → `_resolve_dependency_graph_conflicts()`  
✅ Adaptive throttling → Auto-reduce concurrency on low parallel gain  
✅ Audit support → `reviewer_llm_client` + `_run_gatekeeper_review()`  
✅ Rollback safety → `FileRollback`, `_critical_failure_and_rollback()`  
✅ Telemetry → `OrchestrationTelemetry`, flight recorder  

### What's Missing Operationally

⚠️ **Phase 1 → Phase 2 handoff artifact export**
- Need: Systemized way to emit node contracts from orchestrator to `.senior_agent/nodes/`
- Suggestion: Add `export_node_contracts()` method

⚠️ **Pre-audit automation checkpoint**
- Need: Enforce pre-audit results before Phase 3 audit
- Suggestion: Add validation in `_apply_plan()` to require pre_audit.json

⚠️ **Minimal repro capability**
- Need: When audit rejects, emit exact failing input + expected vs actual
- Suggestion: Enhance audit report format

⚠️ **Schema validation gates**
- Need: Validate `dependencies.json`, `contract.json`, and audit verdict payloads against JSON schemas
- Suggestion: Add strict schema checks before any phase transition

⚠️ **Deterministic acceptance policy**
- Need: Convert narrative recommendations into hard pass/fail thresholds
- Suggestion: Persist `audit_scorecard.json` per node with objective metrics

---

## Implementation Roadmap

### This Week
1. ✅ Code review complete (current codebase is 9/10 quality)
2. Add `export_node_contracts()` CLI flag for Phase 1 handoff
3. Add mandatory `pre_audit_pack` validation before Phase 3
4. Create `.senior_agent/nodes/` folder structure with templates

### Next Week
5. Run test execution of full 7-phase flow on small feature
6. Refine based on real experience
7. Document lessons learned

### Later
8. Add AI-native dashboard to visualize DAG + node status
9. Auto-generate Change Request templates from audit defects

---

## Validation Checklist Before Launch

- [ ] **Contract Freeze Window:** Can Codex implement without touching contracts?
  - Recommendation: 100% freeze; use Change Request if needed
  
- [ ] **Audit Timing:** Should Gemini audit one-by-one or batch all?
  - Recommendation: Batch all; easier to spot cross-node conflicts
  
- [ ] **Level 2 Validation:** Is it a soft gate or hard gate?
  - Recommendation: **Hard gate by default.**
  - Emergency override is allowed only with explicit human sign-off including rationale, risk, and rollback owner.
  
- [ ] **Max Parallel Nodes:** What's your actual limit?
  - Recommendation: Cap at `node_concurrency=8` for token cost control
  
- [ ] **Change Request Overhead:** Will process slow down with many change requests?
  - Recommendation: Invest in Phase 1 contract quality to minimize request loop

---

## TL;DR: Quick Reference

| Phase | Owner | Duration | Parallelizable? | Artifact |
|-------|-------|----------|-----------------|----------|
| **1. Analysis** | Gemini | 10-20 min | No | Contracts + DAG |
| **2a. RED Tests** | Codex | 20-30 min | Yes | test_node.py |
| **2b. GREEN Code** | Codex | 30-60 min | Yes | implementation + pre_audit |
| **3. Audit** | Gemini | 10-15 min | Yes (batch) | audit_return.md |
| **4. Change Request** | Both | 20-30 min | No | Updated contract v1.1 |
| **5. Merge** | Orchestrator | 2-5 min | Yes | Merged workspace |
| **6. Level 2 DoD** | Orchestrator | 5-10 min | Partial | session_report.json |
| **7. Ship** | Human | 1-5 min | No | Deployed + release notes |

---

## Key Success Factors

1. **Contract-First:** No implementation without frozen contract
2. **No Self-Approval:** Coder (Codex) cannot approve completion; Architect (Gemini) audits
3. **Minimal Repro:** Every rejection includes exact failing input
4. **Atomic Merge:** All nodes apply together or rollback together
5. **Parallel Safety:** Dependency graph enforces single-writer per file
6. **Adaptive Throttling:** Reduce concurrency if overhead > parallelization gain

---

**END OF WORKFLOW DOCUMENT**

Next steps: Implement Phase 1 → Phase 2 handoff artifact export in orchestrator.py
