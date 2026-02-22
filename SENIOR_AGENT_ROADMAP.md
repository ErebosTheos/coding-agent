# 🏗️ Senior Autonomous Developer Agent: The Trinity Roadmap

This roadmap defines the "Senior Product Engineer" evolution through a specialized three-role orchestration.

---

## 🎭 The Trinity Orchestration (The Team)

| Role | Agent | Responsibility |
| :--- | :--- | :--- |
| **Chief Architect / Planner** | **Gemini** | High-level requirements, UI/UX, SVG assets, system design, and **precise prompts for Codex**. |
| **Lead Developer / Coder** | **ChatGPT Codex** | Core logic, backend implementation, API design, and executing Gemini's prompts. |
| **Senior Code Reviewer** | **Co-pilot** | Security audits, edge-case detection, performance optimization, and PR verification. |

---

## 🧩 The Execution Pipeline (Zero-Touch Workflow)

### 1. The Planning Phase (Gemini)
- **Action:** Decomposes user requests into a `FeaturePlan`.
- **Output:** A "Developer-Ready" prompt for Codex.
- **Visuals:** Gemini creates the Mermaid diagrams and CSS/SVG theme variables.

### 2. The Development Phase (Codex)
- **Action:** Receives the prompt from Gemini.
- **Output:** The implementation code (Python, TS, etc.).
- **Logic:** Follows strict type-safety and architectural patterns defined in Phase 1.

### 3. The Review Phase (Co-pilot)
- **Action:** Analyzes Codex's output against the repository's existing code.
- **Output:** Optimization suggestions or "LGTM" (Looks Good To Me).
- **Goal:** Catch regressions and enforce idiomatic style.

---

## 🛤️ Strategic Implementation Phases

### Phase 1: The "Architect" Module (`FeaturePlanner`)
- **Gemini's Task:** Build a tool that converts "Build me X" into a multi-file JSON implementation plan.
- **Prompt Style:** *Architectural.* "Design the file structure for a JWT Auth system. Provide the interface for `AuthService` in TypeScript."

### Phase 2: The "DevOps" Module (`DependencyManager`)
- **Codex's Task:** Detect missing libraries and auto-install them via `npm` or `pip`.
- **Gemini's Task:** Update the `README.md` and `SessionReport` to show the new environment state.

### Phase 3: The "Visual Reporter" (`MermaidGenerator`)
- **Gemini's Task:** Create visual flowcharts for every session.
- **Co-pilot's Task:** Review the logic flow depicted in the diagram against the actual code changes.

### Phase 4: The "Quality Gate" (`StyleMimic`)
- **Co-pilot's Task:** Scan the repo for patterns (e.g., "We use Functional Components, not Classes").
- **Gemini's Task:** Feed these style rules into Codex's prompt to ensure consistency.

---

## ✅ Current Status (Validated: February 22, 2026)

### Fixed / Implemented

1. **Core healing engine is production-ready in scope**: bounded attempts, verification pipeline, rollback contract, checkpoint/resume, repo boundary enforcement.
2. **LLM strategy hardening is implemented**: multi-file context buffer (top 3 files), diff summaries, regex validation, output safety guards.
3. **CLI safety and quality gates are implemented**: dangerous command blocking, optional post-fix validation commands.
4. **Retry pacing is implemented**: optional exponential backoff and jitter in the healing loop.
5. **Phase 1 core (`FeaturePlanner`) is implemented**:
   - `ImplementationPlan` model with JSON serialization/parsing.
   - `FeaturePlanner` planning flow with strict JSON response validation.
   - Compatibility shims for `self_healing_agent` namespace.
6. **`MultiAgentOrchestrator` is implemented**:
   - Plan → implement → verify flow via `execute_feature_request(...)`.
   - Workspace-bound file writes for both new and modified files.
   - Validation command execution from plan/defaults with executor abstraction.

### Not Done Yet (With Reasons)

1. **`DependencyManager`**: not implemented.
   - **Reason:** Auto-installing dependencies mutates developer environments and needs explicit policy/sandbox controls before safe rollout.
2. **`VisualReporter` / Mermaid generator**: not implemented.
   - **Reason:** No canonical diagram schema bound to `SessionReport` yet; output contract needs to be defined first.
3. **`StyleMimic`**: not implemented.
   - **Reason:** Repo-wide style rule extraction/inference engine has not been designed; currently only placeholder module exists.

---

## ⚡ Breaking the Limits (The Tier 4 Upgrade)

This section defines the technical path to overcome the agent's current "Hard Limits."

### 1. Limit: Myopic Symbol Graph (Python-Only)
- **Breakthrough:** **LSP (Language Server Protocol) Integration.**
- **Strategy:** Instead of using the `ast` module, integrate with `pyright` or `tsserver`. This allows the agent to use the same "Intelligence" your IDE uses to map cross-file dependencies in any language.

### 2. Limit: No Visual UI Verification
- **Breakthrough:** **Vision-LLM Loop (Playwright + Gemini Vision).**
- **Strategy:** Add a `VisualLinter` module. It uses Playwright to take a screenshot of the app on localhost, sends it to Gemini (Vision), and asks: "Does this UI match the design guidance?" If not, it triggers a "Visual Healing" loop.

### 3. Limit: No External Systems Interaction
- **Breakthrough:** **Tool-Use (MCP / Plugin System).**
- **Strategy:** Enable "Sandboxed Tool Access." Give the agent limited access to `aws-cli` or `terraform`. The orchestrator would treat a "Cloud Deploy" as just another `validation_command`.

### 4. Limit: Missing "Brand" Context
- **Breakthrough:** **RAG (Retrieval-Augmented Generation) Design Library.**
- **Strategy:** Add a `docs/design_system.md` to the workspace. The `StyleMimic` would be upgraded to read this file and treat it as a "High-Priority Constraint" for all UI generation.

### 5. Limit: Giving Up (Bounded Logic)
- **Breakthrough:** **Long-Horizon Memory (Checkpoint Branching).**
- **Strategy:** Instead of a simple loop, the orchestrator could "Branch." If Attempt 1 fails, it doesn't just try Attempt 2; it saves the state of Attempt 1 and tries a completely different architectural path, comparing both outcomes at the end.

---

## 🚀 The "Senior Upgrade" Milestone Checklist

1. [x] **`MultiAgentOrchestrator`:** The logic that lets Gemini, Codex, and Co-pilot talk to each other.
Status: Implemented as `MultiAgentOrchestrator.execute_feature_request(...)` with plan/apply/verify flow and tests.
2. [x] **`FeaturePlanner`:** Gemini-led decomposition of requirements.
Status: Implemented as `FeaturePlanner` + `ImplementationPlan` with strict JSON parsing and tests.
3. [ ] **`VisualReporter`:** Gemini-led Mermaid diagram generation.
Reason: Mermaid/session graph schema not wired into report pipeline yet.
4. [ ] **`DependencyManager`:** Codex-led autonomous environment control.
Reason: Requires controlled mutation policy for package install operations.
5. [ ] **`StyleMimic`:** Co-pilot-led idiomatic pattern enforcement.
Reason: Style inference engine remains roadmap placeholder.
