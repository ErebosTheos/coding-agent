import argparse
import asyncio
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from codegen_agent.orchestrator import Orchestrator

BENCHMARK_PROMPTS = [
    {
        "tier": "small",
        "slug": "prime_checker",
        "prompt": (
            "Create a Python module with a single function is_prime(n) that returns True if n "
            "is prime and False otherwise. Handle edge cases for n <= 1. Include pytest tests."
        ),
    },
    {
        "tier": "small",
        "slug": "stack_class",
        "prompt": (
            "Create a Python module stack.py with a Stack class that implements push, pop, peek, "
            "is_empty, and size. pop/peek should raise IndexError on empty stack. Include tests."
        ),
    },
    {
        "tier": "small",
        "slug": "csv_stats",
        "prompt": (
            "Build a Python utility that reads a CSV file and returns column-wise stats for numeric "
            "fields: count, min, max, mean. Include tests with temp files."
        ),
    },
    {
        "tier": "medium",
        "slug": "todo_api",
        "prompt": (
            "Build a FastAPI TODO API with SQLite and SQLAlchemy: create/list/update/delete todos, "
            "mark complete, and filter by status. Include tests with TestClient."
        ),
    },
    {
        "tier": "medium",
        "slug": "cli_calculator",
        "prompt": (
            "Build a Python CLI calculator app with add/subtract/multiply/divide commands, proper "
            "argument parsing, and division-by-zero handling. Include tests."
        ),
    },
    {
        "tier": "medium",
        "slug": "json_kv_store",
        "prompt": (
            "Build a small Python JSON-backed key-value store module with get/set/delete/list APIs, "
            "atomic file writes, and tests."
        ),
    },
    {
        "tier": "large",
        "slug": "data_validators",
        "prompt": (
            "Build a Python data-validation package for user records, orders, and product payloads "
            "with reusable validators, clear error objects, and a CLI entrypoint. Include tests."
        ),
    },
    {
        "tier": "large",
        "slug": "task_queue",
        "prompt": (
            "Create a SaaS application that provides a REST API for managing a task queue. "
            "Use FastAPI + SQLAlchemy + SQLite. Include auth, enqueue/dequeue, retry tracking, "
            "status transitions, and tests."
        ),
    },
]


@dataclass
class BenchmarkResult:
    slug: str
    tier: str
    wall_clock: float
    heal_attempts: int
    qa_score: float
    qa_approved: bool
    success: bool
    error: Optional[str] = None


def _workspace_for(slug: str) -> str:
    return os.path.join("benchmark_output", slug)


async def run_one(entry: dict, max_heals: int = 3) -> BenchmarkResult:
    slug = entry["slug"]
    tier = entry["tier"]
    prompt = entry["prompt"]
    workspace = _workspace_for(slug)

    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(workspace)

    print(f"\n[{tier.upper()}] {slug}")
    print(f"  Prompt: {prompt[:80]}...")

    t0 = time.monotonic()
    try:
        orchestrator = Orchestrator(workspace)
        report = await orchestrator.run(prompt, max_heals=max_heals)
        elapsed = time.monotonic() - t0

        heals = len(report.healing_report.attempts) if report.healing_report else 0
        score = report.qa_report.score if report.qa_report else 0.0
        approved = report.qa_report.approved if report.qa_report else False

        print(f"  Done in {elapsed:.1f}s | heals={heals} | qa={score:.0f} | {'PASS' if approved else 'FAIL'}")
        return BenchmarkResult(
            slug=slug, tier=tier, wall_clock=elapsed,
            heal_attempts=heals, qa_score=score, qa_approved=approved,
            success=True,
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  FAILED after {elapsed:.1f}s: {exc}")
        return BenchmarkResult(
            slug=slug, tier=tier, wall_clock=elapsed,
            heal_attempts=0, qa_score=0.0, qa_approved=False,
            success=False, error=str(exc),
        )


def _print_summary(results: list[BenchmarkResult]) -> None:
    print("\n" + "=" * 72)
    print(f"{'BENCHMARK SUMMARY':^72}")
    print("=" * 72)
    header = f"{'Slug':<22} {'Tier':<8} {'Time':>7} {'Heals':>6} {'QA':>6} {'Result':>8}"
    print(header)
    print("-" * 72)
    for r in results:
        result_str = "PASS" if r.qa_approved else ("ERROR" if not r.success else "FAIL")
        print(
            f"{r.slug:<22} {r.tier:<8} {r.wall_clock:>6.1f}s "
            f"{r.heal_attempts:>6} {r.qa_score:>5.0f}% {result_str:>8}"
        )
    print("-" * 72)

    passed = sum(1 for r in results if r.qa_approved)
    first_pass = sum(1 for r in results if r.qa_approved and r.heal_attempts == 0)
    total = len(results)
    times = [r.wall_clock for r in results if r.success]

    print(f"\n  Total runs:        {total}")
    print(f"  QA passed:         {passed}/{total} ({passed/total:.0%})")
    print(f"  First-pass (0 heals): {first_pass}/{total} ({first_pass/total:.0%})")
    if times:
        times_sorted = sorted(times)
        p50 = times_sorted[len(times_sorted) // 2]
        p90 = times_sorted[int(len(times_sorted) * 0.9)]
        print(f"  P50 wall clock:    {p50:.1f}s")
        print(f"  P90 wall clock:    {p90:.1f}s")
    print("=" * 72)
    print("\nRun 'python -m codegen_agent doctor --workspace benchmark_output/<slug>'")
    print("or  'python -m codegen_agent doctor --set-baseline' to lock in this baseline.")


async def main():
    parser = argparse.ArgumentParser(description="Codegen Agent Benchmark Suite (§2.2)")
    parser.add_argument(
        "--tier",
        choices=["small", "medium", "large", "all"],
        default="all",
        help="Run only prompts of this tier (default: all)",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=None,
        help="Run only the prompt at this 0-based index in BENCHMARK_PROMPTS",
    )
    parser.add_argument(
        "--max-heals",
        type=int,
        default=3,
        help="Maximum heal iterations per run (default: 3)",
    )
    args = parser.parse_args()

    prompts = BENCHMARK_PROMPTS
    if args.index is not None:
        if args.index < 0 or args.index >= len(BENCHMARK_PROMPTS):
            raise SystemExit(
                f"--index must be between 0 and {len(BENCHMARK_PROMPTS) - 1}, got {args.index}"
            )
        prompts = [BENCHMARK_PROMPTS[args.index]]
    elif args.tier != "all":
        prompts = [p for p in BENCHMARK_PROMPTS if p["tier"] == args.tier]

    print(f"Running {len(prompts)} benchmark prompt(s) — tier={args.tier}, max_heals={args.max_heals}")

    results = []
    for entry in prompts:
        result = await run_one(entry, max_heals=args.max_heals)
        results.append(result)

    _print_summary(results)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBenchmark interrupted by user.")
        sys.exit(130)
