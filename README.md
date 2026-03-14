# coding-agent

An autonomous code generation agent that takes a plain-English prompt and produces a working FastAPI + React application — complete with tests, self-healing on failures, and a full QA pass.

Built in Python with a 7-stage orchestration pipeline, adaptive concurrency, and 73+ passing tests.

---

## How it works

Given a prompt like *"Build a task management API with React frontend"*, the agent:

```
1. Plan       — breaks the prompt into a structured spec
2. Architect  — designs file structure and module dependencies  
3. Execute    — generates all files in parallel, wave-by-wave
4. Test       — writes and runs a full test suite automatically
5. Self-Heal  — classifies failures, patches code, re-runs tests (up to N attempts)
6. QA         — reviews generated code for quality and consistency
7. Deploy     — assembles the final workspace
```

The agent never stops at the first error. It classifies each failure (lint / logic / import / type), applies a targeted fix, and re-runs — autonomously — until tests pass or the retry budget is exhausted.

---

## Performance

| Scenario | Before | After | Speedup |
|----------|--------|-------|---------|
| Small project (10 files) | ~120s | ~75s | **+37%** |
| Medium project (50 files) | ~380s | ~220s | **+42%** |
| Heal loop (3 attempts) | ~25s | ~18s | **+28%** |
| Test generation (20 files) | ~40s | ~28s | **+33%** |

Key optimizations:
- **Adaptive concurrency** — auto-scales to `max(2, cpu_count - 1)` instead of a fixed pool (+40% on 8-core machines)
- **Smart batch sizing** — 30–40% fewer LLM API round-trips by tuning bulk thresholds per CPU count  
- **Pre-compiled regex** — failure classifier runs 40% faster with module-load pattern compilation

---

## Tech stack

- **Python** 85% · **JavaScript/React** 7% · HTML/CSS 7%
- FastAPI · asyncio · pytest · LLM API (pluggable)
- GitHub Actions CI · pre-commit hooks

---

## Project structure

```
src/
  codegen_agent/       # Core agent (v1)
    orchestrator.py    # 7-stage pipeline coordinator
    executor.py        # Parallel file generation with adaptive concurrency
    healer.py          # Self-healing: classify → patch → re-run
    test_writer.py     # Automatic test generation
    classifier.py      # Failure classification (lint/logic/import/type)
  codegen_agent_v2/    # Improved agent (active development)
  core/                # Shared utilities and LLM client
static_v2/             # React web UI
tests/                 # 73+ passing tests
server_v2.py           # FastAPI server exposing agent as REST API
benchmark_agent.py     # Performance benchmarking harness
```

---

## Quick start

```bash
git clone https://github.com/ErebosTheos/coding-agent
cd coding-agent
cp .env.example .env        # Add your LLM API key
pip install -e .
python server_v2.py         # Starts FastAPI server on :8000
```

Then open the React UI or call the API directly:

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Build a REST API for a task manager with pytest tests"}'
```

---

## Configuration

```bash
# Tune concurrency (default: auto-detect from CPU count)
export CODEGEN_CONCURRENCY=8

# Increase bulk threshold for large projects
export CODEGEN_MAX_BULK_FILES=50

# LLM timeout per request
export CODEGEN_LLM_TIMEOUT_SECONDS=90
```

---

## Testing

```bash
pytest tests/ -v          # Run full suite (73+ tests)
python benchmark_agent.py  # Run performance benchmarks
```

---

Built by [Aditya Nepal](https://linkedin.com/in/adityanepal) · [Portfolio](https://personal-portfolio-gamma-swart-27.vercel.app)
