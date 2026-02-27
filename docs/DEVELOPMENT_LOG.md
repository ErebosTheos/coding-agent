# Development Log: Autonomous Codegen Agent

## 2026-02-26: Initial Core Implementation

### Implementation Steps:
1.  **Project Structure**: Created `src/codegen_agent` and `tests` directories.
2.  **Data Models**: Implemented `models.py` with frozen dataclasses for all pipeline entities.
3.  **Utility Functions**: Implemented `utils.py` for shell execution, markdown parsing, and hashing.
4.  **Failure Classification**: Migrated and adapted `classifier.py` from Legacy Reference.
5.  **LLM Infrastructure**:
    - Defined `LLMClient` protocol in `llm/protocol.py`.
    - Implemented `GeminiCLIClient` and `ClaudeCLIClient`.
    - Implemented `AnthropicAPIClient` (urllib-based for zero dependencies).
    - Implemented `LLMRouter` with JSON/YAML config support.
6.  **State Management**: Implemented `checkpoint.py` for atomic state persistence.
7.  **Pipeline Stages Implementation**:
    - **Stage 1 (Plan)**: `planner.py`.
    - **Stage 2 (Architect)**: `architect.py`.
    - **Stage 3 (Execute)**: `executor.py` (topological wave scheduling).
    - **Stage 4 (Deps)**: `dependency_manager.py` (migrated from Legacy).
    - **Stage 5 (Tests)**: `test_writer.py` (migrated from Legacy).
    - **Stage 6 (Heal)**: `healer.py` (Self-healing loop).
    - **Stage 7 (QA)**: `qa_auditor.py`.
    - **Stage 8 (Visual)**: `visual_validator.py` (Playwright-based).
8.  **Orchestration**: Implemented `orchestrator.py` to tie all 8 stages together.
9.  **Reporting**: Implemented `reporter.py` for Markdown, Mermaid, and JSON summaries.
10. **CLI Entry Point**: Implemented `main.py` with argument parsing and resume logic.
11. **Installation**: Created `pyproject.toml`.

### Key Decisions:
- **Zero-Dependency Anthropic Client**: Used `urllib` to avoid requiring the `anthropic` library, making the agent more portable.
- **Topological Sorting**: Implemented wave-based execution in `executor.py` to allow parallel generation of independent files.
- **Surgical Legacy Reuse**: Lifted high-value logic (classifier, dependency manager) while modernizing the interface to fit the new architecture.

### Next Steps:
- Comprehensive integration testing.
- Refinement of LLM prompts for better JSON reliability.
- Support for more complex tech stacks (e.g., Docker, Database migrations).

## 2026-02-26: Hello.py Cleanup and Executor Debugging

### Observations:
- **Corrupted Source**: `test_output/hello.py` contained raw LLM thoughts preceding the Python code, causing syntax errors.
- **Extraction Limitation**: The `Executor` in `src/codegen_agent/executor.py` relies on `extract_code_from_markdown` which only looks for triple backtick blocks. If the LLM returns plain text mixed with code without blocks, the entire content is written to disk.

### Actions:
- **Surgical Cleanup**: Overwrote `test_output/hello.py` with the clean implementation of `main()`.
- **Validation**: Ran `python3 test_output/tests/test_hello.py` and confirmed all 4 tests pass (Happy Path and Edge Cases).
- **Code Audit**: Investigated `src/codegen_agent/executor.py` and `src/codegen_agent/utils.py` to understand why the thought process was leaked into the file.

### Findings:
- The system prompt for the Executor instructs the LLM to return *ONLY* source code, but LLMs sometimes ignore this or include thoughts.
- `Executor._execute_node` falls back to the full content if no markdown blocks are found.
- If the LLM provides thoughts *and then* the code without blocks, the thoughts become part of the source file.

## 2026-02-26: Enhanced Logging and Reporting

### Improvements:
- **Real-time Console Logs**: Added explicit `print` statements to `Executor` and `TestWriter` to log every file creation event.
- **Audit Trail in Reports**: Updated `Reporter` to include a dedicated "Generated Source Files" and "Generated Test Files" section in the Markdown summary (`report_summary.md`).
- **Orchestrator Visibility**: The system now provides clear feedback during the topological execution wave, indicating exactly which files are being written.

### Actions:
- Modified `src/codegen_agent/executor.py` to log source file creation.
- Modified `src/codegen_agent/test_writer.py` to log test file creation.
- Modified `src/codegen_agent/reporter.py` to include file lists in the summary report.

## 2026-02-26: Healer Robustness Improvements

### Observations:
- **Binary File Corruption**: The `Healer` would sometimes attempt to read binary files (like `.pyc`) when identifying the most recently modified file, leading to `UnicodeDecodeError`.
- **Regex Ambiguity**: The regex used to extract file paths from error output was too broad, sometimes matching a prefix of a file path (e.g., matching `.py` in a `.pyc` file path).

### Actions:
- **Extension Filtering**: Implemented `ALLOWED_EXTENSIONS` in `src/codegen_agent/healer.py` to restrict the healer to text-based source files.
- **Robust Regex**: Updated `_extract_target_file` with a negative lookahead regex to ensure exact extension matching.
- **Unit Testing**: Created `tests/test_healer.py` to verify the filtering and extraction logic in isolation.

### Results:
- Confirmed that `.pyc` and other binary files are ignored by `_get_most_recent_file`.
- Verified that the healer correctly identifies `.py` files even when `.pyc` files are mentioned in the output, provided the `.py` file is the one intended.
