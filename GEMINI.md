# Codegen Agent Project Mandates

## Core Principles
- **Robustness**: Always use atomic writes for state persistence.
- **Traceability**: Every stage must produce a clear log or artifact.
- **Parallelism**: Maximize concurrency in file generation where dependencies allow.
- **Self-Healing**: Prioritize fixing broken code over failing the pipeline.

## Documentation
- All significant architectural changes must be reflected in `docs/ARCHITECTURE.md`.
- Implementation progress must be tracked in `docs/DEVELOPMENT_LOG.md`.

## Tech Stack
- **Primary Language**: Python 3.8+
- **LLM Transport**: CLI binaries (Gemini, Claude) and direct HTTP (Anthropic).
- **Automation**: Playwright for visual validation, standard test frameworks (pytest, unittest) for healing.

## Directory Structure
- `src/codegen_agent/`: Main package.
- `src/codegen_agent/llm/`: LLM provider implementations.
- `docs/`: System documentation.
- `tests/`: Unit and integration tests.
