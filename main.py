#!/usr/bin/env python3
"""
CLI Entry point for the Senior Autonomous Developer Agent.
Usage: python main.py "npm test" --provider gemini --validate "npm run lint"
"""

import argparse
import logging
import re
import sys
from pathlib import Path

# Ensure the 'src' directory is in the python path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from senior_agent import create_default_senior_agent

_DANGEROUS_COMMAND_PATTERNS = (
    re.compile(r"(^|[;&|])\s*rm\s+-rf\s+/(?:\s|$)"),
    re.compile(r"(^|[;&|])\s*rm\s+-rf\s+--no-preserve-root\b"),
    re.compile(r"(^|[;&|])\s*mkfs(?:\.[a-zA-Z0-9_+-]+)?\b"),
    re.compile(r"(^|[;&|])\s*dd\s+if="),
    re.compile(r"(^|[;&|])\s*(?:shutdown|reboot|halt)\b"),
)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _validate_command_input(command: str) -> str:
    normalized = command.strip()
    if not normalized:
        raise argparse.ArgumentTypeError("command must not be empty.")
    if any(char in normalized for char in ("\n", "\r", "\x00")):
        raise argparse.ArgumentTypeError(
            "command contains disallowed control characters."
        )
    for pattern in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(normalized):
            raise argparse.ArgumentTypeError(
                "command appears destructive and is blocked by CLI safety validation."
            )
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="Senior Autonomous Developer Agent")
    parser.add_argument(
        "command",
        nargs="?",
        type=_validate_command_input,
        help="The primary command to run and heal (e.g., 'pytest')",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Launch the FastAPI control center service instead of running one CLI healing session.",
    )
    parser.add_argument(
        "--provider",
        choices=["gemini", "codex"],
        default="gemini",
        help=(
            "Compatibility flag for non-server flows. In --serve mode, dual-role routing "
            "is enforced (Gemini architect/reviewer + Codex developer)."
        ),
    )
    parser.add_argument(
        "--validate",
        action="append",
        help="Additional validation commands to run after a fix (e.g., 'mypy .')",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum number of healing attempts (default: 3)",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=".",
        help="The directory to operate in (default: current directory)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable verbose debug logging"
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        help="Path to a session checkpoint file to enable persistence/resume",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the FastAPI control center (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the FastAPI control center (default: 8000).",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help=(
            "API key required for mutating REST endpoints when serving. "
            "If omitted, SENIOR_AGENT_API_KEY env var is used if present."
        ),
    )
    parser.add_argument(
        "--unsecure",
        action="store_true",
        help=(
            "Allow /api/heal even when server is bound to a non-localhost address. "
            "Use only in trusted networks."
        ),
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.serve:
        if args.port < 1 or args.port > 65535:
            parser.error("--port must be between 1 and 65535.")
        try:
            from senior_agent.web_api import run_server
        except ModuleNotFoundError as exc:
            missing = exc.name or "required dependency"
            print()
            print(
                "CRITICAL ERROR: --serve mode requires FastAPI dependencies. "
                f"Missing module: {missing}. Install with: pip install fastapi uvicorn"
            )
            sys.exit(2)

        run_server(
            host=args.host,
            port=args.port,
            provider=args.provider,
            workspace=args.workspace,
            verbose=args.verbose,
            api_key=args.api_key,
            allow_unsecure=args.unsecure,
        )
        return

    if args.command is None:
        parser.error("command is required unless --serve is used.")

    # Initialize the Senior Engineering Agent
    agent = create_default_senior_agent(
        provider=args.provider,
        workspace=args.workspace,
        max_attempts=args.max_attempts,
        validation_commands=args.validate,
    )

    print()
    print("Starting Senior Agent Session")
    print(f"   Target Command: {args.command}")
    print(f"   Provider:       {args.provider.upper()}")
    if args.validate:
        print(f"   Validations:    {', '.join(args.validate)}")
    print("-" * 50)

    try:
        # Check if we should resume or start fresh
        if args.checkpoint and Path(args.checkpoint).exists():
            print(f"Resuming from checkpoint: {args.checkpoint}")
            report = agent.resume(
                checkpoint_path=args.checkpoint,
                workspace=args.workspace,
                validation_commands=args.validate,
            )
        else:
            report = agent.heal(
                command=args.command,
                workspace=args.workspace,
                checkpoint_path=args.checkpoint,
            )

        # Final Reporting (As per AGENT_INSTRUCTIONS.md Section 7)
        print()
        print("=" * 50)
        print("SESSION SUMMARY")
        print("=" * 50)
        print(f"Status:      {'SUCCESS' if report.success else 'FAILED'}")
        print(f"Final Cmd:   {report.final_result.command}")
        print(f"Attempts:    {len(report.attempts)}")

        if report.attempts:
            print()
            print("MODIFICATIONS MADE:")
            for i, attempt in enumerate(report.attempts, 1):
                status = "Applied" if attempt.applied else "Skipped/Failed"
                print(f"  {i}. {attempt.strategy_name} ({status})")
                if attempt.diff_summary:
                    for diff in attempt.diff_summary:
                        print(f"     - {diff}")
                print(f"     Note: {attempt.note}")

        if not report.success and report.blocked_reason:
            print()
            print(f"REASON FOR FAILURE: {report.blocked_reason}")

        print("=" * 50)
        sys.exit(0 if report.success else 1)

    except Exception as exc:
        print()
        print(f"CRITICAL ERROR: {exc}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
