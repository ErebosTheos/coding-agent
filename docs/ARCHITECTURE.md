# Architecture: Autonomous Codegen Agent

## Overview
The Autonomous Codegen Agent is a multi-stage pipeline designed to transform high-level user prompts into fully functional, tested, and audited software applications. It leverages various LLM providers (Gemini, Claude, Anthropic) to fulfill specialized roles within the development lifecycle.

## Core Components

### 1. Orchestrator (`orchestrator.py`)
The central coordinator that manages the execution of the 8-stage pipeline. It handles state transitions, checkpointing, and error propagation.

### 2. LLM Router (`llm/router.py`)
Maps specific pipeline roles to configured LLM providers. Supports:
- **Gemini CLI**: Local subprocess calls to the Gemini binary.
- **Claude CLI**: Local subprocess calls to the Claude binary.
- **Anthropic API**: Direct HTTP requests to Anthropic's message API.

### 3. Pipeline Stages
1.  **PLAN (`planner.py`)**: Generates a high-level project plan (features, tech stack, entry point).
2.  **ARCHITECT (`architect.py`)**: Produces a detailed architecture (file tree, dependency graph, contracts).
3.  **EXECUTE (`executor.py`)**: Implementation of files in topological waves using `asyncio`.
4.  **DEPS (`dependency_manager.py`)**: Identifies and installs missing runtime dependencies.
5.  **TESTS (`test_writer.py`)**: Generates a comprehensive test suite based on the plan and implementation.
6.  **HEAL (`healer.py`)**: A bounded loop that runs tests, classifies failures, and asks the LLM for fixes.
    - **Extension Filtering**: Only considers text-based source files (`.py`, `.js`, `.ts`, etc.) to prevent `UnicodeDecodeError` with binary files.
    - **Robust Extraction**: Uses negative lookahead regex to accurately identify target files from error output (e.g., distinguishing `.py` from `.pyc`).
7.  **QA (`qa_auditor.py`)**: Performs a final audit and assigns a quality score.
8.  **VISUAL (`visual_validator.py`)**: Optional visual audit using Playwright for web projects.

## Data Models (`models.py`)
Uses `@dataclass(frozen=True)` for all core entities to ensure immutability and reliable state persistence.

## State Management (`checkpoint.py`)
Atomic JSON-based checkpointing allows the agent to resume from the last successful stage, providing robustness against network or execution failures.

## Reporting (`reporter.py`)
Generates:
- **JSON Report**: Full machine-readable state.
- **Mermaid Diagram**: Visual representation of the project architecture.
- **Markdown Summary**: Human-readable status report.
