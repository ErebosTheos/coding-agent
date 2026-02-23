from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Literal

from senior_agent.llm_client import CodexCLIClient, GeminiCLIClient, LocalOffloadClient, LLMClient
from senior_agent.planner import FeaturePlanner
from senior_agent_v2.orchestrator import MultiAgentOrchestratorV2


ProviderName = Literal["gemini", "codex", "local"]


def _build_client(
    *,
    provider: ProviderName,
    workspace: Path,
    model: str | None,
    timeout_seconds: int,
) -> LLMClient:
    model_clean = model.strip() if isinstance(model, str) else ""
    selected_model = model_clean or None
    if provider == "gemini":
        return GeminiCLIClient(
            model=selected_model,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
    if provider == "codex":
        return CodexCLIClient(
            model=selected_model,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
    local_model = selected_model or "deepseek-coder:latest"
    return LocalOffloadClient(
        model=local_model,
        workspace=workspace,
        timeout_seconds=timeout_seconds,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Senior Agent V2 runner (Gemini architect/reviewer + Codex developer)."
    )
    parser.add_argument(
        "requirement",
        help="Feature requirement text for V2 execution.",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace directory to apply changes (default: current directory).",
    )
    parser.add_argument(
        "--architect-provider",
        choices=["gemini", "codex", "local"],
        default="gemini",
        help="Provider for planning + review roles.",
    )
    parser.add_argument(
        "--developer-provider",
        choices=["gemini", "codex", "local"],
        default="codex",
        help="Provider for execution role.",
    )
    parser.add_argument(
        "--architect-model",
        default="",
        help="Optional model name override for architect/reviewer provider.",
    )
    parser.add_argument(
        "--developer-model",
        default="",
        help="Optional model name override for developer provider.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=180,
        help="Per-LLM request timeout in seconds.",
    )
    parser.add_argument(
        "--node-concurrency",
        type=int,
        default=8,
        help="Parallel node concurrency for Phase 2.",
    )
    parser.add_argument(
        "--disable-persistent-daemons",
        action="store_true",
        help="Disable validation daemon usage.",
    )
    parser.add_argument(
        "--disable-visual-linter",
        action="store_true",
        help="Disable Phase 6b visual validation.",
    )
    parser.add_argument(
        "--disable-visual-auto-heal",
        action="store_true",
        help="Disable visual auto-heal follow-up nodes.",
    )
    parser.add_argument(
        "--max-visual-auto-heal-attempts",
        type=int,
        default=1,
        help="Maximum number of visual auto-heal follow-up attempts.",
    )
    parser.add_argument(
        "--visual-target-url",
        default="http://127.0.0.1:8080",
        help="Visual linter target URL (default: http://127.0.0.1:8080).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print final summary as JSON.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    architect_client = _build_client(
        provider=args.architect_provider,
        workspace=workspace,
        model=args.architect_model,
        timeout_seconds=max(1, int(args.timeout_seconds)),
    )
    developer_client = _build_client(
        provider=args.developer_provider,
        workspace=workspace,
        model=args.developer_model,
        timeout_seconds=max(1, int(args.timeout_seconds)),
    )
    planner = FeaturePlanner(llm_client=architect_client)

    orchestrator = MultiAgentOrchestratorV2(
        llm_client=developer_client,
        reviewer_llm_client=architect_client,
        planner=planner,
        node_concurrency=max(1, int(args.node_concurrency)),
        enable_persistent_daemons=not bool(args.disable_persistent_daemons),
        enable_visual_linter=not bool(args.disable_visual_linter),
        visual_linter_target_url=str(args.visual_target_url).strip(),
        enable_visual_auto_heal=not bool(args.disable_visual_auto_heal),
        max_visual_auto_heal_attempts=max(0, int(args.max_visual_auto_heal_attempts)),
    )

    ok = await orchestrator.execute_feature_request(
        requirement=str(args.requirement).strip(),
        workspace=workspace,
    )
    report_path = workspace / ".senior_agent" / "v2_session_report.json"
    blocked_reason: str | None = None
    if report_path.exists():
        try:
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            blocked_reason = (
                str(report_payload.get("blocked_reason"))
                if report_payload.get("blocked_reason") is not None
                else None
            )
        except json.JSONDecodeError:
            blocked_reason = "Unable to parse v2_session_report.json"

    summary = {
        "success": bool(ok),
        "workspace": str(workspace),
        "report_path": str(report_path),
        "blocked_reason": blocked_reason,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print()
        print("V2 Run Summary")
        print(f"- success: {summary['success']}")
        print(f"- workspace: {summary['workspace']}")
        print(f"- report: {summary['report_path']}")
        if summary["blocked_reason"]:
            print(f"- blocked_reason: {summary['blocked_reason']}")

    return 0 if ok else 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    _configure_logging(bool(args.verbose))
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except Exception as exc:
        if args.json:
            print(json.dumps({"success": False, "error": str(exc)}))
        else:
            print(f"V2 run failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
