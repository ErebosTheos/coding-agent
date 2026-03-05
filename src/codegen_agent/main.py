import argparse
import asyncio
import os
import sys
from pathlib import Path
from .orchestrator import Orchestrator
from .metrics import RollingMetrics, save_baseline, load_baseline, compare


def _run_health_check() -> int:
    """Print pass/fail for each environment check. Returns exit code (0=all pass)."""
    import shutil
    issues = []

    # 1. CLI binaries
    for binary in ("claude", "gemini", "codex"):
        found = shutil.which(binary) is not None
        print(f"  {'OK' if found else 'MISSING':8} binary: {binary}")
        if not found:
            issues.append(f"binary '{binary}' not found in PATH")

    # 2. .env file present
    env_path = Path(".env")
    env_present = env_path.exists()
    print(f"  {'OK' if env_present else 'MISSING':8} .env file")
    if not env_present:
        issues.append(".env not found (copy from .env.example)")

    # 3. Provider env vars set
    from .llm.router import LLMRouter, _ALL_ROLES
    router = LLMRouter()
    for role in _ALL_ROLES:
        cfg = router.config.get("roles", {}).get(role, {})
        provider = cfg.get("provider", "")
        status = "OK" if provider else "MISSING"
        print(f"  {status:8} role '{role}' -> provider: {provider or 'unset'}")
        if not provider:
            issues.append(f"role '{role}' has no provider configured")

    # 4. Output workspace writable
    ws = Path("./output")
    ws.mkdir(parents=True, exist_ok=True)
    try:
        test_file = ws / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()
        print(f"  {'OK':8} workspace writable: {ws}")
    except OSError as e:
        print(f"  {'FAIL':8} workspace not writable: {e}")
        issues.append(f"workspace not writable: {e}")

    if issues:
        print(f"\n{len(issues)} issue(s) found:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print("\nAll checks passed.")
    return 0


def _run_doctor_check(workspace: str = "./output", set_baseline: bool = False) -> int:
    """Print rolling-window metrics from the run log. Returns 0 always (read-only)."""
    report_dir = os.path.join(os.path.abspath(workspace), ".codegen_agent")
    runs_path = os.path.join(report_dir, "runs.jsonl")
    baseline_path = os.path.join(report_dir, "baseline.json")

    metrics = RollingMetrics(runs_path).compute(window=20)

    if metrics is None:
        print("No run data found. Complete at least one pipeline run first.")
        print(f"  Expected log at: {runs_path}")
        return 0

    if set_baseline:
        save_baseline(baseline_path, metrics)
        print(f"Baseline saved ({metrics.run_count} runs) → {baseline_path}")
        return 0

    print(f"Rolling metrics (last {metrics.run_count} runs):")
    print(f"  p50 wall_clock      {metrics.p50_wall_clock:.1f}s")
    print(f"  p90 wall_clock      {metrics.p90_wall_clock:.1f}s")
    print(f"  first_pass_rate     {metrics.first_pass_rate:.0%}")
    print(f"  avg_heal_attempts   {metrics.avg_heal_attempts:.2f}")
    print(f"  qa_approval_rate    {metrics.qa_approval_rate:.0%}")

    baseline = load_baseline(baseline_path)
    if baseline is None:
        print("\nBaseline: not established.")
        print("  Run 'codegen doctor --set-baseline' after a benchmark to lock in baseline.")
    else:
        verdicts = compare(metrics, baseline)
        print(f"\nVs baseline ({baseline.run_count} runs):")
        for metric, verdict in verdicts.items():
            print(f"  {metric:<20} {verdict}")

    return 0


def _run_status_check(workspace: str = "./output") -> int:
    """Print the checkpoint state for a workspace. Returns 0 always (read-only)."""
    from .checkpoint import CheckpointManager

    ws = os.path.abspath(workspace)
    report = CheckpointManager(ws).load()

    if report is None:
        print(f"No checkpoint found in: {ws}")
        print(f"  Expected: {os.path.join(ws, '.codegen_agent', 'checkpoint.json')}")
        return 0

    prompt_preview = report.prompt[:80] + ("..." if len(report.prompt) > 80 else "")
    print(f"Workspace:  {ws}")
    print(f"Prompt:     {prompt_preview}")
    print(f"Wall clock: {report.wall_clock_seconds:.1f}s")
    print()

    stages = [
        ("PLAN",    report.plan is not None),
        ("ARCH",    report.architecture is not None),
        ("EXEC",    report.execution_result is not None),
        ("DEPS",    report.dependency_resolution is not None),
        ("TESTS",   report.test_suite is not None),
        ("HEAL",    report.healing_report is not None),
        ("QA",      report.qa_report is not None),
        ("VISUAL",  report.visual_audit is not None),
    ]
    for name, done in stages:
        print(f"  {'✓' if done else '○'} {name}")

    if report.qa_report:
        print(f"\nQA score:   {report.qa_report.score:.0f}/100  "
              f"({'approved' if report.qa_report.approved else 'not approved'})")

    if report.execution_result and report.execution_result.generated_files:
        files = report.execution_result.generated_files
        print(f"\nGenerated {len(files)} file(s):")
        for f in files:
            print(f"  {f.file_path}")

    if report.healing_report:
        h = report.healing_report
        heals = len(h.attempts)
        print(f"\nHeal attempts: {heals}  ({'success' if h.success else 'failed'})")
        if h.blocked_reason:
            print(f"  Blocked: {h.blocked_reason}")

    return 0


async def main_async():
    parser = argparse.ArgumentParser(description="Autonomous Codegen Agent")
    subparsers = parser.add_subparsers(dest="command")

    # --- 'run' subcommand (existing behavior) ---
    run_parser = subparsers.add_parser("run", help="Run the codegen pipeline")
    run_parser.add_argument("--prompt", type=str)
    run_parser.add_argument("--workspace", type=str, default="./output")
    run_parser.add_argument("--config", type=str)
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--verbose", action="store_true")
    run_parser.add_argument(
        "--max-heals",
        type=int,
        default=3,
        dest="max_heals",
        help="Maximum heal iterations (0 = skip healing, default: 3)",
    )

    # --- 'serve' subcommand ---
    serve_parser = subparsers.add_parser("serve", help="Start the web dashboard")
    serve_parser.add_argument("--host", type=str, default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=7070)
    serve_parser.add_argument("--output-dir", type=str, default="./output", dest="output_dir")
    serve_parser.add_argument("--inbox-dir", type=str, default="./inbox", dest="inbox_dir")
    serve_parser.add_argument("--config", type=str)

    # --- 'health' subcommand ---
    subparsers.add_parser("health", help="Check environment and provider readiness")
    doctor_parser = subparsers.add_parser("doctor", help="Show rolling-window pipeline metrics")
    doctor_parser.add_argument("--workspace", type=str, default="./output")
    doctor_parser.add_argument(
        "--set-baseline",
        action="store_true",
        help="Save the current rolling window as the comparison baseline",
    )
    status_parser = subparsers.add_parser("status", help="Show checkpoint state for a workspace")
    status_parser.add_argument("--workspace", type=str, default="./output")

    args = parser.parse_args()

    if args.command == "serve":
        from .dashboard.server import start_server
        await start_server(
            host=args.host,
            port=args.port,
            output_dir=os.path.abspath(args.output_dir),
            inbox_dir=os.path.abspath(args.inbox_dir),
            config_path=args.config,
        )
        return

    if args.command == "health":
        sys.exit(_run_health_check())

    if args.command == "doctor":
        sys.exit(_run_doctor_check(args.workspace, set_baseline=args.set_baseline))

    if args.command == "status":
        sys.exit(_run_status_check(args.workspace))

    if args.command == "run":
        if not args.prompt and not args.resume:
            run_parser.print_help()
            return
        workspace = os.path.abspath(args.workspace)
        os.makedirs(workspace, exist_ok=True)

        orchestrator = Orchestrator(workspace, args.config)

        try:
            print(f"Starting codegen agent in {workspace}...")
            report = await orchestrator.run(args.prompt, resume=args.resume, max_heals=args.max_heals)
            print("\n--- PIPELINE COMPLETE ---")
            print(f"Project: {report.plan.project_name if report.plan else 'Unknown'}")
            print(f"QA Score: {report.qa_report.score if report.qa_report else 'N/A'}/100")
            print(f"Report saved to: {os.path.join(workspace, '.codegen_agent', 'report_summary.md')}")
        except Exception as e:
            print(f"\nPIPELINE FAILED: {str(e)}")
            if args.verbose:
                import traceback
                traceback.print_exc()
            sys.exit(1)
        return

    # No subcommand
    parser.print_help()

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)

if __name__ == "__main__":
    main()
