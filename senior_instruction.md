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

## 🚀 ACTIVE TASK: Phase 6 - The Style Mimic
**Objective:** Ensure the agent writes idiomatic code that matches the project's existing style, patterns, and conventions.

### 📋 Prompt for ChatGPT Codex (Lead Developer)
*Copy this into Codex to build the style-inference logic:*

> **Role:** Senior Lead Developer.
> **Context:** We are building the `StyleMimic` module to ensure all AI-generated code matches the repository's local conventions.
> **Task:** Implement the `StyleMimic` class in `src/senior_agent/style_mimic/__init__.py`.
> 
> **1. Logic Requirements:**
> - Implement a method `infer_project_style(workspace: Path) -> str`.
> - **Process:**
>   - Scan the workspace for up to 5 source files (focusing on the primary language of the project).
>   - **Analyze Patterns:**
>     - Indentation (Spaces vs Tabs, and the count).
>     - Quote Style (Single vs Double).
>     - Naming Conventions (camelCase, snake_case, PascalCase).
>     - Framework specific patterns (e.g., Arrow functions vs Function keywords in JS).
>   - **Framework Detection:** Identify if the project is using React, Vue, FastAPI, Django, etc.
> - **Output:** A concise, declarative string summarizing the style rules (e.g., "Style: 4-space indent, snake_case names, double quotes, FastAPI patterns").
> 
> **2. Integration Hook:**
> - Update `src/senior_agent/orchestrator.py` to:
>   - Initialize `StyleMimic` in the constructor.
>   - In `execute_feature_request()`, *before* file generation:
>     - Call `self.style_mimic.infer_project_style()`.
>     - **Prompt Injection:** Inject the inferred style rules into the prompts for `_build_new_file_prompt` and `_build_modify_file_prompt`.
> 
> **Constraint:** Return ONLY the code for `style_mimic/__init__.py` and the updated `orchestrator.py`.
