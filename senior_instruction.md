# 🧠 Senior Developer Agent Instructions (The Dual-Agent Workflow)

## 0) The Dual-Agent Roles
1.  **Chief Architect & Senior Reviewer (Gemini):** You. You plan the systems, design the visuals (CSS/SVG), write the high-precision prompts for the Lead Developer, and perform the final code audit (Security, Performance, Style).
2.  **Lead Developer / Coder (ChatGPT Codex):** The engine. Receives architectural prompts and outputs production-grade code (Logic, Backend, APIs).

## 1) The Dual-Agent Workflow
1.  **PLAN (Architect):** Gemini decomposes a requirement into a structured implementation plan.
2.  **CODE (Developer):** Gemini sends a "Sharp Prompt" to Codex. Codex generates the files/logic.
3.  **REVIEW (Reviewer):** Gemini performs a final audit of Codex's code for security, performance, and logic.
4.  **EXECUTE (Agent):** Once Gemini approves, the agent surgically applies the changes and runs the "Definition of Done."

## 2) Prompt Styling Guide
- **To Codex (Developer):** "Role: Lead Developer. Mandate: Execution Focus. Code only. Return ONLY the code block. No chatter."
- **Internal Mandate (Gemini Review):** "Identify O(N^2) loops, edge cases, and style drifts. Ensure the code is production-ready."

---

## 🚀 BACKLOG: Phase 1 - FeaturePlanner (COMPLETED)
**Objective:** Decompose requirements into actionable implementation plans.

---

## 🚀 BACKLOG: Phase 2 - MultiAgentOrchestrator (COMPLETED)
**Objective:** Execute implementation plans with atomicity and environment-aware validation.

---

## 🚀 BACKLOG: Phase 3 - The Visual Reporter (COMPLETED)
**Objective:** Provide high-signal transparency by generating Mermaid.js diagrams of autonomous changes.

---

## 🚀 BACKLOG: Phase 5 - The Test Generator (COMPLETED)
**Objective:** Complete the Senior Engineer profile by ensuring 100% test coverage for all new features.

---

## 🚀 BACKLOG: Phase 4 - The Dependency Manager (COMPLETED)
**Objective:** Enable the agent to autonomously manage its runtime environment by installing missing libraries.

---

## 🚀 BACKLOG: Phase 6 - The Style Mimic (COMPLETED)
**Objective:** Ensure the agent writes idiomatic code that matches the project's existing style, patterns, and conventions.

---

## 🚀 BACKLOG: Legacy Modernization (Migration) (COMPLETED)
**Objective:** Fully migrate the legacy `self_healing_agent` logic into the new `senior_agent` architecture. Deprecate the old package while ensuring all existing features are preserved and improved.

---

## 🚀 ACTIVE TASK: Phase 3 - The Symbol Graph (Contextual Awareness)
**Objective:** Give the agent "X-Ray Vision" to understand project dependencies and call graphs, preventing regressions.

### 📋 Prompt for ChatGPT Codex (Lead Developer)
*Copy this into Codex to build the static analysis logic:*

> **Role:** Senior Lead Developer.
> **Context:** We are building the `SymbolGraph` module to provide global codebase awareness.
> **Task:** Implement the `SymbolGraph` class in `src/senior_agent/symbol_graph/__init__.py`.
> 
> **1. Logic Requirements:**
> - Implement a method `build_graph(workspace: Path) -> None`.
> - **Process:**
>   - Scan the workspace for source files (`.py`, `.ts`, etc.).
>   - Parse files to extract symbols (classes, functions) and their references (who calls what).
>   - **Initial Implementation:** Start with a robust Python parser (using the `ast` module) to map function definitions and calls.
> - **Query Method:** `get_dependents(file_path: Path, symbol_name: str) -> list[Path]`.
>   - Returns a list of files that depend on the given symbol.
> 
> **2. Integration Hook:**
> - Update `src/senior_agent/orchestrator.py` to:
>   - Initialize `SymbolGraph` in the constructor.
>   - In `execute_feature_request()`, if a file is modified:
>     - Query `SymbolGraph` for impacted files.
>     - **Proactive Validation:** Automatically add tests for the impacted files to the `plan.validation_commands`.
> 
> **Constraint:** Return ONLY the code for `symbol_graph/__init__.py` and the updated `orchestrator.py`. Ensure it handles large repos gracefully.
