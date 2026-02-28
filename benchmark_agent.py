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
        "tier": "large",
        "slug": "dashboard_ui",
        "prompt": (
            "Build a stunning premium SaaS analytics dashboard as a single self-contained index.html "
            "with all CSS in a <style> block and all JS in a <script> block. "
            "CDN links allowed (Google Fonts only). Zero dependencies, zero build tools. "
            ""
            "VISUAL IDENTITY: "
            "Background: deep navy #0a0e1a. "
            "Sidebar: #0d1117 with a 1px right border in #1e2433. "
            "Cards: #111827 background, 1px border #1e2d3d, border-radius 16px. "
            "Primary accent: electric blue #3b82f6. Secondary: violet #8b5cf6. "
            "Success: #10b981. Warning: #f59e0b. Danger: #ef4444. "
            "All text on Inter (Google Fonts). Headings bold 600+, body 400. "
            "Subtle blue glow (box-shadow: 0 0 40px rgba(59,130,246,0.08)) on hover states. "
            ""
            "LAYOUT: "
            "Fixed left sidebar 260px with logo at top (a blue lightning bolt SVG icon + 'Pulse' wordmark), "
            "nav sections: MAIN (Dashboard, Analytics, Revenue), MANAGE (Users, Projects, Reports), "
            "SYSTEM (Settings, Help). Each nav item has an SVG icon, label, and active state "
            "with blue left border + blue text + faint blue background. Sidebar footer shows "
            "user avatar, name 'Alex Morgan', role 'Admin', and a logout icon. "
            "Top header: breadcrumb on left, center has a pill-shaped search bar, right has "
            "notification bell with a red dot badge, a 'Upgrade' CTA button in gradient "
            "(blue to violet), and avatar. "
            ""
            "DASHBOARD PAGE — 4 sections: "
            ""
            "1. KPI ROW — 4 gradient-bordered cards: "
            "Total Revenue $847,293 (+12.5% vs last month, green), "
            "Active Users 24,891 (+8.2%, green), "
            "Churn Rate 2.1% (-0.4%, green — lower is better), "
            "MRR $70,607 (+15.3%, green). "
            "Each card: large metric value in white 32px bold, trend pill with arrow, "
            "a small sparkline SVG (7-point line) in the bottom right corner. "
            ""
            "2. CHARTS ROW — two side-by-side panels: "
            "LEFT (60% width): 'Revenue Over Time' area chart in SVG. "
            "12 months of data, filled area with a blue-to-transparent gradient fill, "
            "stroke line in #3b82f6, gridlines in #1e2433, x/y axis labels in #6b7280. "
            "Animated: stroke-dashoffset draws the line on load over 1.2s. "
            "Tooltip on mouseover showing month + value. "
            "RIGHT (40% width): 'Traffic Sources' donut chart in SVG. "
            "5 segments: Organic 38%, Direct 24%, Social 18%, Email 12%, Paid 8%. "
            "Each segment a different color. Center shows total '100K visits'. "
            "Legend below with colored dots and percentages. "
            ""
            "3. USERS TABLE — 'Recent Signups' with columns: "
            "Avatar+Name, Email, Plan (Free/Pro/Enterprise as colored pills), "
            "Joined, MRR, Status (Active/Trial/Churned pills). "
            "10 realistic mock rows. Sortable column headers (click toggles asc/desc arrow). "
            "Row hover highlights in #1a2235. Pagination controls below (Prev / 1 2 3 / Next). "
            ""
            "4. BOTTOM ROW — two panels: "
            "LEFT: 'Top Pages' — horizontal bar chart (SVG) showing 6 pages with visit counts, "
            "bars in blue gradient, value labels on the right. "
            "RIGHT: 'Live Activity Feed' — scrollable list of 12 timestamped events like "
            "'User signed up', 'Pro plan upgraded', 'Payment failed' each with a colored "
            "icon dot and relative time ('2m ago'). New items animate in from the top. "
            ""
            "INTERACTIVITY: "
            "Sidebar nav clicks switch active state and swap main content area between "
            "Dashboard (full layout above) and placeholder screens for other pages "
            "showing a centered icon + 'Analytics coming soon' style message. "
            "Search bar filters the users table in real-time. "
            "Column header clicks sort the table. "
            "Dark/light mode toggle in header — light mode uses #f8fafc bg, white cards, "
            "dark text — persisted in localStorage. "
            "Notification bell click opens a dropdown panel with 3 mock notifications. "
            "All transitions 200ms ease. Scrollbar styled dark to match theme."
        ),
    },
    {
        "tier": "large",
        "slug": "projectflow",
        "prompt": (
            "Build a Python FastAPI project management API called Projectflow. "
            "MODELS: User (id, email, hashed_password, name, created_at, is_active), "
            "Project (id, name, description, owner_id, status active/archived, created_at), "
            "Task (id, project_id, assignee_id, title, description, status todo/in_progress/done, "
            "priority low/medium/high/critical, due_date, created_at, updated_at), "
            "Comment (id, task_id, author_id, body, created_at). "
            "ENDPOINTS: POST /auth/register, POST /auth/login returning JWT, "
            "GET/POST /projects, GET/PUT/DELETE /projects/{id}, "
            "GET/POST /projects/{id}/tasks, PUT/DELETE /tasks/{id}, "
            "GET/POST /tasks/{id}/comments. "
            "BUSINESS RULES: only project owner can archive or delete a project (403 otherwise); "
            "overdue tasks (due_date < today and status != done) include is_overdue=true in responses; "
            "deleting a project cascades to its tasks and comments. "
            "TECHNICAL: FastAPI + SQLAlchemy with SQLite, async sessions; "
            "JWT auth via python-jose, passwords hashed with passlib/bcrypt; "
            "Pydantic v2 schemas with email validation and password min 8 chars; "
            "src/ package layout with src/routers/, src/models/, src/schemas/, src/crud/; "
            "top-level run.py that starts uvicorn. "
            "TESTS in tests/ using FastAPI TestClient with fresh in-memory SQLite per session: "
            "test registration and login flow, JWT protection returning 401 on bad token, "
            "owner-vs-non-owner project delete returning 403, task creation and overdue flag, "
            "comment creation and retrieval."
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


async def run_one(entry: dict, max_heals: int = 0) -> BenchmarkResult:
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
        default=0,
        help="Maximum heal iterations per run (default: 0 for faster benchmark runs)",
    )
    args = parser.parse_args()

    prompts = BENCHMARK_PROMPTS
    if args.index is not None:
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
