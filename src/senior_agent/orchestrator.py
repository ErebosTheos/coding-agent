from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
import hashlib
import inspect
import json
import logging
import multiprocessing as mp
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4
from pathlib import Path
from typing import Any, TextIO

from senior_agent.dependency_manager import DependencyManager
from senior_agent.engine import Executor, SeniorAgent, run_shell_command
from senior_agent.llm_client import LLMClient, LLMClientError
from senior_agent.models import (
    CommandResult,
    DependencyGraph,
    ExecutionNode,
    FileRollback,
    ImplementationPlan,
    NodeExecutionRecord,
    NodeStatus,
    OrchestrationTelemetry,
    SessionReport,
)
from senior_agent.patterns import CODE_FENCE_PATTERN
from senior_agent.planner import FeaturePlanner
from senior_agent.symbol_graph import SymbolGraph
from senior_agent.style_mimic import StyleMimic
from senior_agent.test_writer import TestWriter
from senior_agent.utils import is_within_workspace
from senior_agent.visual_reporter import VisualReporter

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_BINARIES: tuple[str, ...] = (
    "python",
    "python3",
    "pytest",
    "pip",
    "pip3",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "go",
    "cargo",
    "ruff",
    "mypy",
    "black",
    "pyright",
    "tsc",
    "gofmt",
    "uv",
    "poetry",
    "node",
    "sh",
    "bash",
    "make",
    "tox",
)
_ROLLBACK_ARTIFACT_CANDIDATES: tuple[str, ...] = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.lock",
    "go.sum",
    "Cargo.lock",
)
_VALIDATION_DAEMON_MODULE = "senior_agent.validation_daemon"
_MIN_COMMAND_TIMEOUT_SECONDS = 0.1
_DEFAULT_VALIDATION_COMMAND_TIMEOUT_SECONDS = 300.0
_DAEMON_RESPONSE_PADDING_SECONDS = 5.0
_SEMANTIC_MERGE_MAX_FORMAT_ISSUES = 10


@dataclass(frozen=True)
class _NodeRunResult:
    node: ExecutionNode
    trace_id: str
    status: NodeStatus
    level1_passed: bool
    duration_seconds: float
    note: str = ""
    rollback_entries: tuple[FileRollback, ...] = ()
    commands_run: tuple[str, ...] = ()
    final_result: CommandResult | None = None


@dataclass
class _ValidationDaemonState:
    workspace_root: Path
    process: subprocess.Popen[str]
    lock: threading.Lock
    last_used_at: float


def _execute_in_subprocess(
    executor: Executor,
    command: str,
    workspace_root: str,
    queue: Any,
) -> None:
    try:
        result = executor(command, Path(workspace_root))
        queue.put(
            {
                "ok": True,
                "command": result.command,
                "return_code": int(result.return_code),
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    except Exception as exc:  # pragma: no cover - defensive guardrail
        queue.put(
            {
                "ok": False,
                "error": str(exc),
            }
        )


class MultiAgentOrchestrator:
    """Coordinate plan -> implement -> verify flow for feature requests."""

    def __init__(
        self,
        llm_client: LLMClient,
        planner: FeaturePlanner,
        executor: Executor = run_shell_command,
        visual_reporter: VisualReporter | None = None,
        test_writer: TestWriter | None = None,
        dependency_manager: DependencyManager | None = None,
        style_mimic: StyleMimic | None = None,
        symbol_graph: SymbolGraph | None = None,
        architect_llm_client: LLMClient | None = None,
        reviewer_llm_client: LLMClient | None = None,
        generation_concurrency: int = 1,
        node_concurrency: int = 4,
        enable_self_critique: bool = True,
        enable_adaptive_throttling: bool = True,
        enable_selective_testing: bool = True,
        enable_persistent_daemons: bool = True,
        daemon_cache_ttl_seconds: float = 120.0,
        enable_speculative_prefetch: bool = True,
        enable_speculative_review: bool = True,
        trust_speculative_review_if_pass: bool = True,
        node_heartbeat_interval_seconds: float = 10.0,
        node_watchdog_timeout_seconds: float = 60.0,
        validation_command_timeout_seconds: float | None = _DEFAULT_VALIDATION_COMMAND_TIMEOUT_SECONDS,
        daemon_startup_timeout_seconds: float = 5.0,
        watchdog_kill_grace_seconds: float = 1.0,
        command_allowlist: tuple[str, ...] = _DEFAULT_ALLOWED_BINARIES,
        conflict_resolution_attempts: int = 3,
        enable_hitl_conflict_pause: bool = True,
        hitl_conflict_timeout_seconds: float = 60.0,
        hitl_poll_interval_seconds: float = 2.0,
        conflict_resolution_relative_path: str = ".senior_agent/conflict_resolution.json",
        flight_recorder_relative_dir: str = ".senior_agent",
        enable_fix_cache: bool = True,
        fix_cache_relative_path: str = ".senior_agent/fix_cache.json",
        max_fix_cache_entries: int = 128,
        max_fix_cache_file_chars: int = 200_000,
        enforce_semantic_merge_gate: bool = True,
        disable_runtime_checks: bool = False,
    ) -> None:
        self.llm_client = llm_client
        self.architect_llm_client = architect_llm_client
        self.reviewer_llm_client = reviewer_llm_client
        self.planner = planner
        self.executor = executor
        self.visual_reporter = visual_reporter or VisualReporter()
        self.test_writer = test_writer or TestWriter(llm_client=llm_client)
        self.dependency_manager = dependency_manager or DependencyManager(executor=executor)
        self.style_mimic = style_mimic or StyleMimic()
        self.symbol_graph = symbol_graph or SymbolGraph()
        self._rollback_agent = SeniorAgent(max_attempts=1, executor=executor)
        self._environment_workspace = Path(".").resolve()
        self.generation_concurrency = max(1, generation_concurrency)
        self.node_concurrency = max(1, node_concurrency)
        self.enable_adaptive_throttling = enable_adaptive_throttling
        self.enable_selective_testing = enable_selective_testing
        self.enable_persistent_daemons = enable_persistent_daemons
        self.daemon_cache_ttl_seconds = max(1.0, daemon_cache_ttl_seconds)
        self.enable_speculative_prefetch = enable_speculative_prefetch
        self.enable_speculative_review = enable_speculative_review
        self.trust_speculative_review_if_pass = trust_speculative_review_if_pass
        self.node_heartbeat_interval_seconds = max(1.0, node_heartbeat_interval_seconds)
        self.node_watchdog_timeout_seconds = max(1.0, node_watchdog_timeout_seconds)
        if validation_command_timeout_seconds is None:
            self.validation_command_timeout_seconds = None
        else:
            self.validation_command_timeout_seconds = max(
                _MIN_COMMAND_TIMEOUT_SECONDS,
                float(validation_command_timeout_seconds),
            )
        self.daemon_startup_timeout_seconds = max(
            _MIN_COMMAND_TIMEOUT_SECONDS,
            daemon_startup_timeout_seconds,
        )
        self.watchdog_kill_grace_seconds = max(
            _MIN_COMMAND_TIMEOUT_SECONDS,
            watchdog_kill_grace_seconds,
        )
        self.command_allowlist = tuple(
            sorted({entry.strip() for entry in command_allowlist if entry.strip()})
        )
        self.conflict_resolution_attempts = max(1, conflict_resolution_attempts)
        self.enable_hitl_conflict_pause = enable_hitl_conflict_pause
        self.hitl_conflict_timeout_seconds = max(1.0, hitl_conflict_timeout_seconds)
        self.hitl_poll_interval_seconds = max(0.1, hitl_poll_interval_seconds)
        self.conflict_resolution_relative_path = Path(conflict_resolution_relative_path)
        self.flight_recorder_relative_dir = Path(flight_recorder_relative_dir)
        self.enable_self_critique = enable_self_critique
        self.enable_fix_cache = enable_fix_cache
        self.fix_cache_relative_path = Path(fix_cache_relative_path)
        self.max_fix_cache_entries = max(1, max_fix_cache_entries)
        self.max_fix_cache_file_chars = max(1, max_fix_cache_file_chars)
        self.enforce_semantic_merge_gate = enforce_semantic_merge_gate
        self.disable_runtime_checks = disable_runtime_checks
        self._validation_daemon_cache: dict[str, tuple[float, CommandResult]] = {}
        self._validation_daemons: dict[Path, _ValidationDaemonState] = {}
        self._last_conflict_auto_merge_applied = False

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self._shutdown_validation_daemons()
        except Exception:
            pass

    def execute_feature_request(
        self,
        requirement: str,
        codebase_summary: str,
        workspace: str | Path = ".",
        *,
        fast_mode: bool = False,
    ) -> bool:
        workspace_root = Path(workspace).resolve()
        self._environment_workspace = workspace_root
        if not workspace_root.exists() or not workspace_root.is_dir():
            logger.error("Workspace path is invalid or missing: %s", workspace_root)
            return False

        planner_attempts = 2 if fast_mode else 1
        last_planner_error: Exception | None = None
        plan: ImplementationPlan | None = None
        for attempt in range(1, planner_attempts + 1):
            try:
                plan = self.planner.plan_feature(requirement, codebase_summary)
                break
            except (LLMClientError, ValueError) as exc:
                last_planner_error = exc
                if attempt < planner_attempts:
                    logger.warning(
                        "Feature planning attempt %s/%s failed in fast mode; retrying once: %s",
                        attempt,
                        planner_attempts,
                        exc,
                    )
                    continue
                logger.error("Feature planning failed: %s", exc)
            except Exception as exc:  # pragma: no cover - defensive guardrail
                last_planner_error = exc
                if attempt < planner_attempts:
                    logger.warning(
                        "Unexpected planner failure on attempt %s/%s in fast mode; retrying: %s",
                        attempt,
                        planner_attempts,
                        exc,
                    )
                    continue
                logger.exception("Unexpected planner failure: %s", exc)

        if plan is None:
            exc = last_planner_error or RuntimeError("planner returned no plan")
            blocked_reason = (
                f"Feature planning failed: {exc}"
                if isinstance(exc, (LLMClientError, ValueError))
                else f"Unexpected planner failure: {exc}"
            )
            fallback_plan = ImplementationPlan(
                feature_name=requirement.strip() or "Unplanned Feature",
                summary="Feature planning failed.",
            )
            self._emit_visual_summary(
                plan=fallback_plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=CommandResult(
                        command="planning",
                        return_code=1,
                        stdout="",
                        stderr=blocked_reason,
                    ),
                    success=False,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        self.test_writer.workspace = workspace_root

        if fast_mode:
            style_rules = "Style: preserve existing conventions."
            generated_file_overrides: dict[str, str] = {}
            test_generation_note: str | None = None
            logger.info(
                "Fast mode enabled: skipping style inference, proactive symbol validation, and test generation."
            )
        else:
            style_rules, symbol_graph_ready = self._prefetch_workspace_context(workspace_root)

            if plan.dependency_graph is None:
                if not symbol_graph_ready:
                    try:
                        self.symbol_graph.build_graph(workspace_root)
                        symbol_graph_ready = True
                    except Exception as exc:  # pragma: no cover - defensive guardrail
                        logger.exception(
                            "Symbol graph build failed; continuing without proactive impact validation: %s",
                            exc,
                        )
                if symbol_graph_ready:
                    try:
                        plan = self._augment_plan_with_symbol_graph_validation(
                            plan=plan,
                            workspace_root=workspace_root,
                        )
                    except Exception as exc:  # pragma: no cover - defensive guardrail
                        logger.exception(
                            "Symbol graph augmentation failed; continuing without proactive impact validation: %s",
                            exc,
                        )

            if plan.dependency_graph is None:
                plan, generated_file_overrides, test_generation_note = self._augment_plan_with_generated_tests(
                    plan=plan,
                    workspace_root=workspace_root,
                )
            else:
                generated_file_overrides = {}
                test_generation_note = None
        self._log_plan(plan)
        if test_generation_note is not None:
            blocked_reason = test_generation_note
            final_result = CommandResult(
                command="test-generation",
                return_code=1,
                stdout="",
                stderr=blocked_reason,
            )
            logger.error("TDD generation failed: %s", blocked_reason)
            self._emit_visual_summary(
                plan=plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=final_result,
                    success=False,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        if plan.dependency_graph is not None:
            graph_success, graph_final_result, graph_blocked_reason, node_records, telemetry = (
                self._execute_dependency_graph(
                    requirement=requirement,
                    plan=plan,
                    dependency_graph=plan.dependency_graph,
                    workspace_root=workspace_root,
                    style_rules=style_rules,
                    fast_mode=fast_mode,
                )
            )
            if graph_success:
                self._store_successful_fix_cache_entry(
                    requirement=requirement,
                    plan=plan,
                    workspace_root=workspace_root,
                )
            self._emit_visual_summary(
                plan=plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=graph_final_result,
                    success=graph_success,
                    blocked_reason=graph_blocked_reason,
                    node_records=node_records,
                    telemetry=telemetry,
                ),
                workspace_root=workspace_root,
            )
            return graph_success

        validation_commands = tuple(command.strip() for command in plan.validation_commands if command.strip())
        if not validation_commands and not fast_mode and not self.disable_runtime_checks:
            inferred_validation_commands = tuple(
                self._autodetect_validation_commands(workspace_root)
            )
            if inferred_validation_commands:
                validation_commands = inferred_validation_commands
                plan = ImplementationPlan(
                    feature_name=plan.feature_name,
                    summary=plan.summary,
                    new_files=list(plan.new_files),
                    modified_files=list(plan.modified_files),
                    steps=list(plan.steps),
                    validation_commands=list(inferred_validation_commands),
                    design_guidance=plan.design_guidance,
                )
                logger.info(
                    "No validation commands in plan '%s'; using autodetected defaults: %s",
                    plan.feature_name,
                    ", ".join(validation_commands),
                )
            else:
                blocked_reason = (
                    "No validation commands were provided in the plan and no "
                    "safe defaults could be detected."
                )
                final_result = CommandResult(
                    command="validation-autodetect",
                    return_code=1,
                    stdout="",
                    stderr=blocked_reason,
                )
                logger.error(blocked_reason)
                self._emit_visual_summary(
                    plan=plan,
                    report=self._build_session_report(
                        command=requirement,
                        final_result=final_result,
                        success=False,
                        blocked_reason=blocked_reason,
                    ),
                    workspace_root=workspace_root,
                )
                return False
        elif fast_mode and not validation_commands:
            logger.info(
                "Fast mode enabled with no plan validation commands; deferring strict validation to caller."
            )
        elif self.disable_runtime_checks and not validation_commands:
            logger.info(
                "Runtime checks disabled with no validation commands; continuing without strict validation."
            )

        if validation_commands and not fast_mode and not self.disable_runtime_checks:
            validation_commands = tuple(
                self._apply_selective_testing_to_commands(
                    plan=plan,
                    workspace_root=workspace_root,
                    commands=list(validation_commands),
                )
            )

        cache_overrides = self._load_cached_fix_outputs(
            requirement=requirement,
            plan=plan,
            workspace_root=workspace_root,
        )
        plan_file_overrides = dict(cache_overrides)
        plan_file_overrides.update(generated_file_overrides)

        success = False
        blocked_reason: str | None = None
        final_result = CommandResult(
            command=requirement,
            return_code=0,
            stdout="Feature plan generated.",
            stderr="",
        )
        speculative_review_executor: ThreadPoolExecutor | None = None
        speculative_review_future = None

        if (
            validation_commands
            and not fast_mode
            and not self.disable_runtime_checks
            and not self._check_environment(list(validation_commands))
        ):
            blocked_reason = (
                "Environment check failed for planned validation commands. "
                "Aborting before file generation."
            )
            final_result = CommandResult(
                command="environment-check",
                return_code=1,
                stdout="",
                stderr=blocked_reason,
            )
            logger.critical(
                "Environment check failed for planned validation commands. "
                "Aborting before file generation."
            )
            self._emit_visual_summary(
                plan=plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=final_result,
                    success=success,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        rollback_map: dict[Path, FileRollback] = {}
        applied_ok, failure_note = self._apply_plan(
            plan=plan,
            workspace_root=workspace_root,
            rollback_map=rollback_map,
            file_overrides=plan_file_overrides,
            style_rules=style_rules,
        )
        if not applied_ok:
            blocked_reason = failure_note or "Feature implementation failed."
            final_result = CommandResult(
                command="plan-apply",
                return_code=1,
                stdout="",
                stderr=blocked_reason,
            )
            self._critical_failure_and_rollback(
                reason=blocked_reason,
                workspace_root=workspace_root,
                rollback_entries=tuple(rollback_map.values()),
            )
            self._emit_visual_summary(
                plan=plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=final_result,
                    success=success,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        if (
            self.reviewer_llm_client is not None
            and not fast_mode
            and not self.disable_runtime_checks
            and self.enable_speculative_review
        ):
            speculative_review_executor = ThreadPoolExecutor(max_workers=1)
            speculative_review_future = speculative_review_executor.submit(
                self._run_gatekeeper_review,
                plan=plan,
                requirement=requirement,
                workspace_root=workspace_root,
                validation_commands=validation_commands,
                final_result=CommandResult(
                    command="speculative-prevalidation",
                    return_code=0,
                    stdout="Speculative pre-validation context.",
                    stderr="",
                ),
            )

        if validation_commands and not fast_mode and not self.disable_runtime_checks:
            validation_ok, validation_result = self._run_validation(validation_commands, workspace_root)
            if validation_result is not None:
                final_result = validation_result
            if not validation_ok:
                blocked_reason = (
                    "Validation command execution failed after applying planned "
                    "file changes."
                )
                self._critical_failure_and_rollback(
                    reason=blocked_reason,
                    workspace_root=workspace_root,
                    rollback_entries=tuple(rollback_map.values()),
                )
                self._emit_visual_summary(
                    plan=plan,
                    report=self._build_session_report(
                        command=requirement,
                        final_result=final_result,
                        success=success,
                        blocked_reason=blocked_reason,
                    ),
                    workspace_root=workspace_root,
                )
                if speculative_review_executor is not None:
                    speculative_review_executor.shutdown(wait=False, cancel_futures=True)
                return False

        if self.reviewer_llm_client is not None and not fast_mode and not self.disable_runtime_checks:
            review_passed = True
            review_note = ""
            speculative_accepted = False
            if speculative_review_future is not None and speculative_review_future.done():
                try:
                    speculative_passed, speculative_note = speculative_review_future.result()
                    if speculative_note:
                        logger.info("Speculative review note: %s", speculative_note)
                    if not speculative_passed:
                        review_passed = False
                        review_note = f"Speculative review failed: {speculative_note}"
                    elif self.trust_speculative_review_if_pass:
                        review_passed = True
                        review_note = speculative_note or "Speculative review passed."
                        speculative_accepted = True
                except Exception as exc:  # pragma: no cover - defensive guardrail
                    logger.exception("Speculative review task failed: %s", exc)

            if review_passed and not speculative_accepted:
                review_passed, review_note = self._run_gatekeeper_review(
                    plan=plan,
                    requirement=requirement,
                    workspace_root=workspace_root,
                    validation_commands=validation_commands,
                    final_result=final_result,
                )
            if review_note:
                logger.info("Gatekeeper review note: %s", review_note)
            if not review_passed:
                blocked_reason = f"Gatekeeper review rejected changes: {review_note}"
                final_result = CommandResult(
                    command="gatekeeper-review",
                    return_code=1,
                    stdout="",
                    stderr=blocked_reason,
                )
                self._critical_failure_and_rollback(
                    reason=blocked_reason,
                    workspace_root=workspace_root,
                    rollback_entries=tuple(rollback_map.values()),
                )
                self._emit_visual_summary(
                    plan=plan,
                    report=self._build_session_report(
                        command=requirement,
                        final_result=final_result,
                        success=success,
                        blocked_reason=blocked_reason,
                    ),
                    workspace_root=workspace_root,
                )
                if speculative_review_executor is not None:
                    speculative_review_executor.shutdown(wait=False, cancel_futures=True)
                return False

        success = True
        self._store_successful_fix_cache_entry(
            requirement=requirement,
            plan=plan,
            workspace_root=workspace_root,
        )
        self._emit_visual_summary(
            plan=plan,
            report=self._build_session_report(
                command=requirement,
                final_result=final_result,
                success=success,
                blocked_reason=blocked_reason,
            ),
            workspace_root=workspace_root,
        )
        if speculative_review_executor is not None:
            speculative_review_executor.shutdown(wait=False, cancel_futures=True)
        return True

    def _log_plan(self, plan: ImplementationPlan) -> None:
        logger.info(
            "ImplementationPlan: feature=%s summary=%s new_files=%s modified_files=%s steps=%s validations=%s",
            plan.feature_name,
            plan.summary,
            len(plan.new_files),
            len(plan.modified_files),
            len(plan.steps),
            len(plan.validation_commands),
        )
        if plan.dependency_graph is not None:
            logger.info(
                "DependencyGraph: nodes=%s global_validations=%s",
                len(plan.dependency_graph.nodes),
                len(plan.dependency_graph.global_validation_commands),
            )

    def _prefetch_workspace_context(self, workspace_root: Path) -> tuple[str, bool]:
        default_style = "Style: preserve existing conventions."
        if not self.enable_speculative_prefetch:
            try:
                style_rules = self.style_mimic.infer_project_style(workspace_root)
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception(
                    "Style inference failed; falling back to default style guidance: %s",
                    exc,
                )
                style_rules = default_style
            try:
                self.symbol_graph.build_graph(workspace_root)
                symbol_ready = True
            except Exception:  # pragma: no cover - defensive guardrail
                symbol_ready = False
            return style_rules, symbol_ready

        with ThreadPoolExecutor(max_workers=2) as executor:
            style_future = executor.submit(self.style_mimic.infer_project_style, workspace_root)
            symbol_future = executor.submit(self.symbol_graph.build_graph, workspace_root)

            style_rules = default_style
            symbol_ready = False

            try:
                style_rules = style_future.result()
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception(
                    "Speculative style inference failed; falling back to defaults: %s",
                    exc,
                )
                style_rules = default_style

            try:
                symbol_future.result()
                symbol_ready = True
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception(
                    "Speculative symbol graph prefetch failed: %s",
                    exc,
                )
                symbol_ready = False

        return style_rules, symbol_ready

    def _apply_selective_testing_to_commands(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        commands: list[str],
    ) -> list[str]:
        if not self.enable_selective_testing:
            return commands
        if not plan.modified_files:
            return commands

        impacted_tests = self._discover_impacted_test_files(
            modified_files=list(plan.modified_files),
            workspace_root=workspace_root,
        )
        if not impacted_tests:
            return commands
        selective_commands = self.test_writer.build_validation_commands(impacted_tests)
        if not selective_commands:
            return commands

        replaced = False
        output_commands: list[str] = []
        for command in commands:
            cleaned = command.strip()
            if not cleaned:
                continue
            if self._is_test_like_command(cleaned):
                replaced = True
                continue
            output_commands.append(cleaned)

        if replaced:
            output_commands.extend(selective_commands)
            logger.info(
                "Selective testing replaced broad test commands with impacted tests: %s",
                ", ".join(selective_commands),
            )
            return self._unique_values(output_commands)

        output_commands.extend(selective_commands)
        return self._unique_values(output_commands)

    @staticmethod
    def _is_test_like_command(command: str) -> bool:
        normalized = command.lower()
        test_hints = (
            "pytest",
            "unittest",
            "go test",
            "cargo test",
            "npm test",
            "pnpm test",
            "yarn test",
            "vitest",
            "jest",
        )
        return any(hint in normalized for hint in test_hints)

    @staticmethod
    def _is_type_like_command(command: str) -> bool:
        normalized = command.lower()
        type_hints = (
            "mypy",
            "pyright",
            "typecheck",
            "tsc",
            "py_compile",
            "ruff check",
            "go vet",
            "cargo check",
        )
        return any(hint in normalized for hint in type_hints)

    @staticmethod
    def _is_format_like_command(command: str) -> bool:
        normalized = command.lower()
        format_hints = (
            "ruff format",
            "black",
            "prettier",
            "gofmt",
            "cargo fmt",
            "rustfmt",
            "yapf",
            "autopep8",
        )
        return any(hint in normalized for hint in format_hints)

    def _execute_dependency_graph(
        self,
        *,
        requirement: str,
        plan: ImplementationPlan,
        dependency_graph: DependencyGraph,
        workspace_root: Path,
        style_rules: str,
        fast_mode: bool,
    ) -> tuple[bool, CommandResult, str | None, list[NodeExecutionRecord], OrchestrationTelemetry]:
        resolved_graph, conflict_note = self._resolve_dependency_graph_conflicts(
            dependency_graph=dependency_graph,
            workspace_root=workspace_root,
        )
        semantic_merge_gate_required = (
            self.enforce_semantic_merge_gate and self._last_conflict_auto_merge_applied
        )
        if resolved_graph is None:
            final_result = CommandResult(
                command="graph-conflict-resolution",
                return_code=1,
                stdout="",
                stderr=conflict_note or "Dependency graph conflict resolution failed.",
            )
            telemetry = OrchestrationTelemetry(
                initial_concurrency=self.node_concurrency,
                final_concurrency=self.node_concurrency,
                level1_failed_nodes=1,
            )
            return False, final_result, final_result.stderr, [], telemetry

        if not self.disable_runtime_checks:
            graph_validation_commands = self._collect_graph_validation_commands(resolved_graph)
            command_allowlist_ok, command_allowlist_note = self._check_commands_allowlisted(
                commands=graph_validation_commands,
            )
            if not command_allowlist_ok and fast_mode:
                sanitized_graph, dropped_count = self._filter_dependency_graph_allowlisted_commands(
                    resolved_graph
                )
                if dropped_count > 0:
                    logger.warning(
                        "Fast mode removed %s non-allowlisted graph validation command(s).",
                        dropped_count,
                    )
                resolved_graph = sanitized_graph
                graph_validation_commands = self._collect_graph_validation_commands(resolved_graph)
                command_allowlist_ok, command_allowlist_note = self._check_commands_allowlisted(
                    commands=graph_validation_commands,
                )
            if not command_allowlist_ok:
                final_result = CommandResult(
                    command="command-allowlist",
                    return_code=1,
                    stdout="",
                    stderr=command_allowlist_note or "Validation command not allowlisted.",
                )
                telemetry = OrchestrationTelemetry(
                    initial_concurrency=self.node_concurrency,
                    final_concurrency=self.node_concurrency,
                    level1_failed_nodes=1,
                )
                return False, final_result, final_result.stderr, [], telemetry

        if not fast_mode and not self.disable_runtime_checks:
            environment_commands = list(resolved_graph.global_validation_commands)
            if not environment_commands:
                environment_commands = list(plan.validation_commands)
            if environment_commands and not self._check_environment(environment_commands):
                final_result = CommandResult(
                    command="environment-check",
                    return_code=1,
                    stdout="",
                    stderr=(
                        "Environment check failed for graph-level validation commands. "
                        "Aborting before node execution."
                    ),
                )
                telemetry = OrchestrationTelemetry(
                    initial_concurrency=self.node_concurrency,
                    final_concurrency=self.node_concurrency,
                    level1_failed_nodes=1,
                )
                return False, final_result, final_result.stderr, [], telemetry

        node_map: dict[str, ExecutionNode] = {
            node.node_id: node for node in resolved_graph.nodes
        }
        children: dict[str, set[str]] = {node_id: set() for node_id in node_map}
        for node in resolved_graph.nodes:
            for dependency in node.depends_on:
                children.setdefault(dependency, set()).add(node.node_id)

        pending: set[str] = set(node_map)
        completed: set[str] = set()
        failed: set[str] = set()
        evicted: set[str] = set()
        node_records: list[NodeExecutionRecord] = []
        aggregate_rollbacks: dict[Path, FileRollback] = {}

        active_concurrency = max(1, self.node_concurrency)
        initial_concurrency = active_concurrency
        adaptive_throttle_events = 0
        total_node_seconds = 0.0
        level1_pass_nodes = 0
        level1_failed_nodes = 0
        level2_failures = 0
        blocked_reason: str | None = None
        final_result = CommandResult(
            command="graph-dispatch",
            return_code=0,
            stdout="Graph execution completed.",
            stderr="",
        )
        wall_clock_started = time.monotonic()
        flight_recorder_dir = self._resolve_flight_recorder_dir(workspace_root)
        speculative_review_executor: ThreadPoolExecutor | None = None
        speculative_review_future = None
        self._emit_live_dashboard_snapshot(
            plan=plan,
            workspace_root=workspace_root,
            node_records=node_records,
            final_result=final_result,
            blocked_reason=blocked_reason,
            telemetry=OrchestrationTelemetry(
                total_node_seconds=0.0,
                wall_clock_seconds=max(time.monotonic() - wall_clock_started, 1e-9),
                parallel_gain=0.0,
                initial_concurrency=initial_concurrency,
                final_concurrency=active_concurrency,
                adaptive_throttle_events=adaptive_throttle_events,
                level1_pass_nodes=level1_pass_nodes,
                level1_failed_nodes=level1_failed_nodes,
                level2_failures=level2_failures,
            ),
        )

        while pending:
            ready = [
                node_map[node_id]
                for node_id in sorted(pending)
                if all(dependency in completed for dependency in node_map[node_id].depends_on)
            ]
            if not ready:
                blocked_reason = (
                    "Dependency graph stalled: no schedulable nodes remain while pending nodes exist."
                )
                final_result = CommandResult(
                    command="graph-dispatch",
                    return_code=1,
                    stdout="",
                    stderr=blocked_reason,
                )
                break

            wave_nodes = self._select_nodes_for_wave(
                ready_nodes=ready,
                max_nodes=active_concurrency,
            )
            wave_started = time.monotonic()
            wave_results = self._run_node_wave(
                nodes=wave_nodes,
                plan=plan,
                workspace_root=workspace_root,
                style_rules=style_rules,
                flight_recorder_dir=flight_recorder_dir,
            )
            wave_wall_clock = max(time.monotonic() - wave_started, 1e-9)
            wave_node_seconds = sum(result.duration_seconds for result in wave_results)
            total_node_seconds += wave_node_seconds

            if self.enable_adaptive_throttling and active_concurrency > 1:
                wave_parallel_gain = wave_node_seconds / wave_wall_clock
                if wave_parallel_gain < 1.0:
                    active_concurrency -= 1
                    adaptive_throttle_events += 1
                    logger.info(
                        "Adaptive throttling reduced node concurrency to %s after low gain %.3f.",
                        active_concurrency,
                        wave_parallel_gain,
                    )

            for result in wave_results:
                node_id = result.node.node_id
                pending.discard(node_id)
                final_result = result.final_result or final_result
                node_records.append(
                    NodeExecutionRecord(
                        node_id=node_id,
                        trace_id=result.trace_id,
                        status=result.status,
                        level1_passed=result.level1_passed,
                        duration_seconds=result.duration_seconds,
                        note=result.note,
                        commands_run=result.commands_run,
                    )
                )

                if result.status == NodeStatus.SUCCESS:
                    completed.add(node_id)
                    level1_pass_nodes += 1
                    for rollback_entry in result.rollback_entries:
                        aggregate_rollbacks.setdefault(rollback_entry.path, rollback_entry)
                    continue

                level1_failed_nodes += 1
                if result.status == NodeStatus.EVICTED:
                    evicted.add(node_id)
                    continue

                failed.add(node_id)
                if blocked_reason is None:
                    blocked_reason = result.note or f"Node {node_id} failed."

                evicted_children = self._evict_downstream_nodes(
                    parent_node_id=node_id,
                    children=children,
                    pending=pending,
                )
                level1_failed_nodes += len(evicted_children)
                for evicted_node_id in sorted(evicted_children):
                    evicted.add(evicted_node_id)
                    node_records.append(
                        NodeExecutionRecord(
                            node_id=evicted_node_id,
                            trace_id=uuid4().hex[:12],
                            status=NodeStatus.EVICTED,
                            level1_passed=False,
                            duration_seconds=0.0,
                            note=f"Evicted because upstream node '{node_id}' failed.",
                            commands_run=(),
                        )
                    )

                if result.rollback_entries:
                    self._critical_failure_and_rollback(
                        reason=f"Rolling back failed node {node_id}: {result.note}",
                        workspace_root=workspace_root,
                        rollback_entries=result.rollback_entries,
                    )

            partial_wall_clock = max(time.monotonic() - wall_clock_started, 1e-9)
            partial_telemetry = OrchestrationTelemetry(
                total_node_seconds=total_node_seconds,
                wall_clock_seconds=partial_wall_clock,
                parallel_gain=total_node_seconds / partial_wall_clock,
                initial_concurrency=initial_concurrency,
                final_concurrency=active_concurrency,
                adaptive_throttle_events=adaptive_throttle_events,
                level1_pass_nodes=level1_pass_nodes,
                level1_failed_nodes=level1_failed_nodes,
                level2_failures=level2_failures,
            )
            self._emit_live_dashboard_snapshot(
                plan=plan,
                workspace_root=workspace_root,
                node_records=node_records,
                final_result=final_result,
                blocked_reason=blocked_reason,
                telemetry=partial_telemetry,
            )

        all_level1_passed = not failed and not evicted and not pending and blocked_reason is None
        if all_level1_passed:
            global_validation_commands = tuple(
                command.strip()
                for command in (
                    resolved_graph.global_validation_commands or plan.validation_commands
                )
                if command.strip()
            )
            if global_validation_commands:
                global_validation_commands = tuple(
                    self._apply_selective_testing_to_commands(
                        plan=plan,
                        workspace_root=workspace_root,
                        commands=list(global_validation_commands),
                    )
                )
            if (
                self.reviewer_llm_client is not None
                and self.enable_speculative_review
                and not fast_mode
                and not self.disable_runtime_checks
            ):
                speculative_review_executor = ThreadPoolExecutor(max_workers=1)
                speculative_review_future = speculative_review_executor.submit(
                    self._run_gatekeeper_review,
                    plan=plan,
                    requirement=requirement,
                    workspace_root=workspace_root,
                    validation_commands=global_validation_commands,
                    final_result=CommandResult(
                        command="speculative-prevalidation",
                        return_code=0,
                        stdout="Speculative pre-validation context.",
                        stderr="",
                    ),
                )
            if not fast_mode and not self.disable_runtime_checks and not global_validation_commands:
                global_validation_commands = tuple(
                    self._autodetect_validation_commands(workspace_root)
                )
            if not fast_mode and not self.disable_runtime_checks and not global_validation_commands:
                blocked_reason = (
                    "Level 2 validation commands are missing for dependency-graph execution."
                )
                final_result = CommandResult(
                    command="level2-validation",
                    return_code=1,
                    stdout="",
                    stderr=blocked_reason,
                )
                level2_failures += 1
            elif not fast_mode and not self.disable_runtime_checks:
                validation_ok, validation_result = self._run_validation(
                    global_validation_commands,
                    workspace_root,
                )
                if validation_result is not None:
                    final_result = validation_result
                if not validation_ok:
                    blocked_reason = "Level 2 validation failed after node-level success."
                    level2_failures += 1
                else:
                    integrity_ok, integrity_result = self._run_semantic_integrity_check(
                        plan=plan,
                        workspace_root=workspace_root,
                    )
                    final_result = integrity_result
                    if not integrity_ok:
                        blocked_reason = "Semantic integrity check failed."
                        level2_failures += 1
                    elif semantic_merge_gate_required:
                        merge_ok, merge_result = self._run_semantic_merge_gate(
                            plan=plan,
                            workspace_root=workspace_root,
                            validation_commands=global_validation_commands,
                            semantic_integrity_result=integrity_result,
                        )
                        final_result = merge_result
                        if not merge_ok:
                            blocked_reason = "Semantic merge gate failed."
                            level2_failures += 1
                    if blocked_reason is None and self.reviewer_llm_client is not None:
                        review_passed = True
                        review_note = ""
                        speculative_accepted = False
                        if (
                            speculative_review_future is not None
                            and speculative_review_future.done()
                        ):
                            try:
                                speculative_passed, speculative_note = speculative_review_future.result()
                                if speculative_note:
                                    logger.info("Speculative review note: %s", speculative_note)
                                if not speculative_passed:
                                    review_passed = False
                                    review_note = f"Speculative review failed: {speculative_note}"
                                elif self.trust_speculative_review_if_pass:
                                    review_passed = True
                                    review_note = speculative_note or "Speculative review passed."
                                    speculative_accepted = True
                            except Exception as exc:  # pragma: no cover - defensive guardrail
                                logger.exception("Speculative review task failed: %s", exc)

                        if review_passed and not speculative_accepted:
                            review_passed, review_note = self._run_gatekeeper_review(
                                plan=plan,
                                requirement=requirement,
                                workspace_root=workspace_root,
                                validation_commands=global_validation_commands,
                                final_result=final_result,
                            )
                        if review_note:
                            logger.info("Gatekeeper review note: %s", review_note)
                        if not review_passed:
                            blocked_reason = f"Gatekeeper review rejected changes: {review_note}"
                            final_result = CommandResult(
                                command="gatekeeper-review",
                                return_code=1,
                                stdout="",
                                stderr=blocked_reason,
                            )
                            level2_failures += 1
        else:
            if blocked_reason is None:
                blocked_reason = "One or more graph nodes failed Level 1 DoD."
            final_result = CommandResult(
                command="level1-node-validation",
                return_code=1,
                stdout="",
                stderr=blocked_reason,
            )

        success = blocked_reason is None
        if not success:
            self._critical_failure_and_rollback(
                reason=blocked_reason or "Graph execution failed. Rolling back transaction.",
                workspace_root=workspace_root,
                rollback_entries=tuple(aggregate_rollbacks.values()),
            )

        wall_clock_seconds = max(time.monotonic() - wall_clock_started, 1e-9)
        telemetry = OrchestrationTelemetry(
            total_node_seconds=total_node_seconds,
            wall_clock_seconds=wall_clock_seconds,
            parallel_gain=total_node_seconds / wall_clock_seconds,
            initial_concurrency=initial_concurrency,
            final_concurrency=active_concurrency,
            adaptive_throttle_events=adaptive_throttle_events,
            level1_pass_nodes=level1_pass_nodes,
            level1_failed_nodes=level1_failed_nodes,
            level2_failures=level2_failures,
        )
        if speculative_review_executor is not None:
            speculative_review_executor.shutdown(wait=False, cancel_futures=True)
        return success, final_result, blocked_reason, node_records, telemetry

    def _resolve_dependency_graph_conflicts(
        self,
        *,
        dependency_graph: DependencyGraph,
        workspace_root: Path,
    ) -> tuple[DependencyGraph | None, str | None]:
        current_graph = dependency_graph
        self._last_conflict_auto_merge_applied = False
        last_error: str | None = None
        for attempt in range(1, self.conflict_resolution_attempts + 1):
            conflicts = self._collect_file_ownership_conflicts(current_graph)
            if not conflicts:
                return current_graph, None
            try:
                current_graph = self._merge_conflicting_graph_nodes(
                    dependency_graph=current_graph,
                    conflicts=conflicts,
                )
                self._last_conflict_auto_merge_applied = True
            except ValueError as exc:
                last_error = str(exc)
                logger.warning(
                    "Graph conflict merge attempt %s failed: %s",
                    attempt,
                    exc,
                )

        unresolved = self._collect_file_ownership_conflicts(current_graph)
        conflict_path = self._write_conflict_graph(
            workspace_root=workspace_root,
            dependency_graph=current_graph,
            conflicts=unresolved,
        )
        message = (
            "Unable to resolve graph file-ownership conflicts after "
            f"{self.conflict_resolution_attempts} attempts. "
            f"Conflict graph written to {conflict_path}."
        )
        if last_error:
            message = f"{message} Last merge error: {last_error}"
        if self.enable_hitl_conflict_pause:
            steered_graph, steer_note = self._await_human_conflict_steering(
                workspace_root=workspace_root,
                conflict_graph_path=conflict_path,
                fallback_graph=current_graph,
            )
            if steered_graph is not None:
                # Manual steering supersedes auto-merge guarantees.
                self._last_conflict_auto_merge_applied = False
                return steered_graph, None
            if steer_note:
                message = f"{message} {steer_note}"
        return None, message

    @staticmethod
    def _collect_file_ownership_conflicts(
        dependency_graph: DependencyGraph,
    ) -> dict[str, list[str]]:
        owners: dict[str, list[str]] = {}
        for node in dependency_graph.nodes:
            for file_path in [*node.new_files, *node.modified_files]:
                key = file_path.strip()
                if not key:
                    continue
                owners.setdefault(key, []).append(node.node_id)
        return {
            path: sorted(list(dict.fromkeys(node_ids)))
            for path, node_ids in owners.items()
            if len(set(node_ids)) > 1
        }

    def _merge_conflicting_graph_nodes(
        self,
        *,
        dependency_graph: DependencyGraph,
        conflicts: dict[str, list[str]],
    ) -> DependencyGraph:
        node_map: dict[str, ExecutionNode] = {
            node.node_id: node for node in dependency_graph.nodes
        }
        redirect: dict[str, str] = {}

        for owners in conflicts.values():
            active_owners = [owner for owner in owners if owner in node_map]
            if len(active_owners) < 2:
                continue
            primary_id = self._pick_primary_owner(node_map, active_owners)
            primary_node = node_map[primary_id]
            for owner_id in active_owners:
                if owner_id == primary_id:
                    continue
                if owner_id not in node_map:
                    continue
                other = node_map.pop(owner_id)
                redirect[owner_id] = primary_id
                primary_node = ExecutionNode(
                    node_id=primary_node.node_id,
                    title=primary_node.title,
                    summary=(
                        f"{primary_node.summary} | merged {other.node_id}: {other.summary}"
                    ),
                    new_files=self._unique_values([*primary_node.new_files, *other.new_files]),
                    modified_files=self._unique_values(
                        [*primary_node.modified_files, *other.modified_files]
                    ),
                    steps=self._unique_values([*primary_node.steps, *other.steps]),
                    validation_commands=self._unique_values(
                        [*primary_node.validation_commands, *other.validation_commands]
                    ),
                    depends_on=self._unique_values(
                        [
                            dependency
                            for dependency in [*primary_node.depends_on, *other.depends_on]
                            if dependency not in {primary_id, owner_id}
                        ]
                    ),
                    contract_node=primary_node.contract_node or other.contract_node,
                    shared_resources=self._unique_values(
                        [*primary_node.shared_resources, *other.shared_resources]
                    ),
                )
            node_map[primary_id] = primary_node

        rebuilt_nodes: list[ExecutionNode] = []
        for original_node in dependency_graph.nodes:
            target_id = redirect.get(original_node.node_id, original_node.node_id)
            if target_id != original_node.node_id:
                continue
            node = node_map.get(target_id)
            if node is None:
                continue
            rewritten_dependencies = self._unique_values(
                [
                    redirect.get(dependency, dependency)
                    for dependency in node.depends_on
                    if redirect.get(dependency, dependency) in node_map
                    and redirect.get(dependency, dependency) != node.node_id
                ]
            )
            rebuilt_nodes.append(
                ExecutionNode(
                    node_id=node.node_id,
                    title=node.title,
                    summary=node.summary,
                    new_files=list(node.new_files),
                    modified_files=list(node.modified_files),
                    steps=list(node.steps),
                    validation_commands=list(node.validation_commands),
                    depends_on=rewritten_dependencies,
                    contract_node=node.contract_node,
                    shared_resources=list(node.shared_resources),
                )
            )

        merged_graph = DependencyGraph(
            feature_name=dependency_graph.feature_name,
            summary=dependency_graph.summary,
            nodes=rebuilt_nodes,
            global_validation_commands=list(dependency_graph.global_validation_commands),
        )
        merged_graph.validate()
        return merged_graph

    @staticmethod
    def _pick_primary_owner(
        node_map: dict[str, ExecutionNode],
        owners: list[str],
    ) -> str:
        contract_owners = [
            owner
            for owner in owners
            if node_map.get(owner) is not None and node_map[owner].contract_node
        ]
        if contract_owners:
            return sorted(contract_owners)[0]
        return sorted(owners)[0]

    @staticmethod
    def _unique_values(values: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            cleaned = value.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            deduped.append(cleaned)
        return deduped

    def _write_conflict_graph(
        self,
        *,
        workspace_root: Path,
        dependency_graph: DependencyGraph,
        conflicts: dict[str, list[str]],
    ) -> Path:
        recorder_dir = self._resolve_flight_recorder_dir(workspace_root)
        path = (recorder_dir / "conflict_graph.json").resolve()
        payload = {
            "feature_name": dependency_graph.feature_name,
            "summary": dependency_graph.summary,
            "conflicts": conflicts,
            "nodes": [node.to_dict() for node in dependency_graph.nodes],
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def _await_human_conflict_steering(
        self,
        *,
        workspace_root: Path,
        conflict_graph_path: Path,
        fallback_graph: DependencyGraph,
    ) -> tuple[DependencyGraph | None, str | None]:
        steering_path = self._resolve_conflict_resolution_path(workspace_root)
        if steering_path is None:
            return None, "Manual steering file path is invalid."

        started = time.monotonic()
        logger.critical(
            "HITL required: waiting for conflict steering file at %s (timeout %.1fs). Conflict graph: %s",
            steering_path,
            self.hitl_conflict_timeout_seconds,
            conflict_graph_path,
        )
        while (time.monotonic() - started) <= self.hitl_conflict_timeout_seconds:
            if steering_path.exists() and steering_path.is_file():
                try:
                    payload = json.loads(steering_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    time.sleep(self.hitl_poll_interval_seconds)
                    continue
                action = str(payload.get("action", "")).strip().lower()
                if action in {"abort", "stop"}:
                    return None, "Manual steering requested abort."
                if action in {"retry", "resolve"}:
                    graph_payload = payload.get("dependency_graph")
                    if isinstance(graph_payload, dict):
                        try:
                            return DependencyGraph.from_dict(graph_payload), None
                        except ValueError as exc:
                            logger.error("Manual steering graph was invalid: %s", exc)
                            return None, f"Manual steering graph invalid: {exc}"
                    if action == "retry":
                        return fallback_graph, "Manual steering requested retry with fallback graph."
            time.sleep(self.hitl_poll_interval_seconds)

        return None, (
            "Manual steering timeout reached without a valid conflict resolution file."
        )

    def _resolve_conflict_resolution_path(self, workspace_root: Path) -> Path | None:
        candidate = (workspace_root / self.conflict_resolution_relative_path).resolve()
        if not is_within_workspace(workspace_root, candidate):
            return None
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def _check_commands_allowlisted(
        self,
        *,
        commands: list[str],
    ) -> tuple[bool, str | None]:
        for command in commands:
            candidate = command.strip()
            if not candidate:
                continue
            allowed, note = self._is_command_allowlisted(candidate)
            if not allowed:
                return False, note
        return True, None

    @staticmethod
    def _collect_graph_validation_commands(
        dependency_graph: DependencyGraph,
    ) -> list[str]:
        return [
            *dependency_graph.global_validation_commands,
            *[
                command
                for node in dependency_graph.nodes
                for command in node.validation_commands
            ],
        ]

    def _filter_dependency_graph_allowlisted_commands(
        self,
        dependency_graph: DependencyGraph,
    ) -> tuple[DependencyGraph, int]:
        dropped = 0
        global_commands: list[str] = []
        for command in dependency_graph.global_validation_commands:
            cleaned = command.strip()
            if not cleaned:
                continue
            allowlisted, _ = self._is_command_allowlisted(cleaned)
            if allowlisted:
                global_commands.append(cleaned)
                continue
            dropped += 1

        sanitized_nodes: list[ExecutionNode] = []
        for node in dependency_graph.nodes:
            node_commands: list[str] = []
            for command in node.validation_commands:
                cleaned = command.strip()
                if not cleaned:
                    continue
                allowlisted, _ = self._is_command_allowlisted(cleaned)
                if allowlisted:
                    node_commands.append(cleaned)
                    continue
                dropped += 1
            sanitized_nodes.append(
                ExecutionNode(
                    node_id=node.node_id,
                    title=node.title,
                    summary=node.summary,
                    new_files=list(node.new_files),
                    modified_files=list(node.modified_files),
                    steps=list(node.steps),
                    validation_commands=self._unique_values(node_commands),
                    depends_on=list(node.depends_on),
                    contract_node=node.contract_node,
                    shared_resources=list(node.shared_resources),
                )
            )

        return (
            DependencyGraph(
                feature_name=dependency_graph.feature_name,
                summary=dependency_graph.summary,
                nodes=sanitized_nodes,
                global_validation_commands=self._unique_values(global_commands),
            ),
            dropped,
        )

    def _is_command_allowlisted(self, command: str) -> tuple[bool, str | None]:
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            return False, f"Unable to parse command for allowlist check: {exc}"
        if not tokens:
            return False, "Validation command is empty after parsing."
        binary = os.path.basename(tokens[0])
        if binary in self.command_allowlist:
            return True, None
        return (
            False,
            "Validation command binary is not allowlisted: "
            f"{binary}. Allowed: {', '.join(self.command_allowlist)}",
        )

    def _select_nodes_for_wave(
        self,
        *,
        ready_nodes: list[ExecutionNode],
        max_nodes: int,
    ) -> list[ExecutionNode]:
        ordered_nodes = sorted(
            ready_nodes,
            key=lambda node: (not node.contract_node, node.node_id),
        )
        selected: list[ExecutionNode] = []
        occupied_resources: set[str] = set()

        for node in ordered_nodes:
            if len(selected) >= max_nodes:
                break
            node_resources = {resource.strip() for resource in node.shared_resources if resource.strip()}
            if node_resources and node_resources.intersection(occupied_resources):
                continue
            selected.append(node)
            occupied_resources.update(node_resources)

        if not selected and ordered_nodes:
            selected.append(ordered_nodes[0])
        return selected

    def _evict_downstream_nodes(
        self,
        *,
        parent_node_id: str,
        children: dict[str, set[str]],
        pending: set[str],
    ) -> set[str]:
        evicted: set[str] = set()
        stack = list(children.get(parent_node_id, set()))
        while stack:
            candidate = stack.pop()
            if candidate in evicted:
                continue
            if candidate in pending:
                pending.discard(candidate)
                evicted.add(candidate)
            stack.extend(children.get(candidate, set()))
        return evicted

    def _run_node_wave(
        self,
        *,
        nodes: list[ExecutionNode],
        plan: ImplementationPlan,
        workspace_root: Path,
        style_rules: str,
        flight_recorder_dir: Path,
    ) -> list[_NodeRunResult]:
        if len(nodes) <= 1:
            return [
                self._execute_dependency_node(
                    node=nodes[0],
                    plan=plan,
                    workspace_root=workspace_root,
                    style_rules=style_rules,
                    flight_recorder_dir=flight_recorder_dir,
                )
            ]

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.warning(
                "Event loop already running; falling back to sequential node wave execution."
            )
            return [
                self._execute_dependency_node(
                    node=node,
                    plan=plan,
                    workspace_root=workspace_root,
                    style_rules=style_rules,
                    flight_recorder_dir=flight_recorder_dir,
                )
                for node in nodes
            ]

        return asyncio.run(
            self._run_node_wave_async(
                nodes=nodes,
                plan=plan,
                workspace_root=workspace_root,
                style_rules=style_rules,
                flight_recorder_dir=flight_recorder_dir,
            )
        )

    async def _run_node_wave_async(
        self,
        *,
        nodes: list[ExecutionNode],
        plan: ImplementationPlan,
        workspace_root: Path,
        style_rules: str,
        flight_recorder_dir: Path,
    ) -> list[_NodeRunResult]:
        semaphore = asyncio.Semaphore(max(1, min(self.node_concurrency, len(nodes))))

        async def execute_one(node: ExecutionNode) -> _NodeRunResult:
            async with semaphore:
                return await asyncio.to_thread(
                    self._execute_dependency_node,
                    node=node,
                    plan=plan,
                    workspace_root=workspace_root,
                    style_rules=style_rules,
                    flight_recorder_dir=flight_recorder_dir,
                )

        tasks = [execute_one(node) for node in nodes]
        return list(await asyncio.gather(*tasks))

    def _execute_dependency_node(
        self,
        *,
        node: ExecutionNode,
        plan: ImplementationPlan,
        workspace_root: Path,
        style_rules: str,
        flight_recorder_dir: Path,
    ) -> _NodeRunResult:
        trace_id = uuid4().hex[:12]
        trace_path = self._resolve_node_log_path(
            flight_recorder_dir=flight_recorder_dir,
            node_id=node.node_id,
        )
        started = time.monotonic()
        timeout_hit = {"value": False}
        last_activity = {"at": started}

        def mark_activity(message: str) -> None:
            last_activity["at"] = time.monotonic()
            self._append_node_trace(
                trace_path=trace_path,
                trace_id=trace_id,
                node_id=node.node_id,
                message=message,
            )

        def heartbeat(stop_event: "threading.Event") -> None:
            while not stop_event.wait(self.node_heartbeat_interval_seconds):
                elapsed = time.monotonic() - started
                self._append_node_trace(
                    trace_path=trace_path,
                    trace_id=trace_id,
                    node_id=node.node_id,
                    message=f"heartbeat elapsed={elapsed:.2f}s",
                )
                silence_seconds = time.monotonic() - last_activity["at"]
                if silence_seconds >= self.node_watchdog_timeout_seconds:
                    timeout_hit["value"] = True
                    self._append_node_trace(
                        trace_path=trace_path,
                        trace_id=trace_id,
                        node_id=node.node_id,
                        message=(
                            f"watchdog timeout triggered after {silence_seconds:.2f}s without progress"
                        ),
                    )
                    break

        stop_event = threading.Event()
        heartbeat_thread = threading.Thread(
            target=heartbeat,
            args=(stop_event,),
            daemon=True,
        )
        heartbeat_thread.start()
        rollback_map: dict[Path, FileRollback] = {}
        commands_run: list[str] = []

        try:
            mark_activity("node started")
            node_plan = self._build_node_plan(plan=plan, node=node)
            self._capture_artifact_rollbacks(
                workspace_root=workspace_root,
                rollback_map=rollback_map,
            )
            mark_activity("captured rollback snapshots")
            apply_ok, apply_note = self._apply_plan(
                plan=node_plan,
                workspace_root=workspace_root,
                rollback_map=rollback_map,
                file_overrides={},
                style_rules=style_rules,
            )
            if not apply_ok:
                duration = max(time.monotonic() - started, 0.0)
                note = apply_note or f"Node {node.node_id} failed while applying file changes."
                mark_activity(f"apply failed: {note}")
                return _NodeRunResult(
                    node=node,
                    trace_id=trace_id,
                    status=NodeStatus.FAILED,
                    level1_passed=False,
                    duration_seconds=duration,
                    note=note,
                    rollback_entries=tuple(rollback_map.values()),
                    commands_run=tuple(commands_run),
                    final_result=CommandResult(
                        command=f"node:{node.node_id}:apply",
                        return_code=1,
                        stdout="",
                        stderr=note,
                    ),
                )

            if timeout_hit["value"]:
                duration = max(time.monotonic() - started, 0.0)
                timeout_note = (
                    f"Node {node.node_id} exceeded watchdog timeout "
                    f"({self.node_watchdog_timeout_seconds:.0f}s)."
                )
                mark_activity(timeout_note)
                return _NodeRunResult(
                    node=node,
                    trace_id=trace_id,
                    status=NodeStatus.FAILED,
                    level1_passed=False,
                    duration_seconds=duration,
                    note=timeout_note,
                    rollback_entries=tuple(rollback_map.values()),
                    commands_run=tuple(commands_run),
                    final_result=CommandResult(
                        command=f"node:{node.node_id}:watchdog",
                        return_code=1,
                        stdout="",
                        stderr=timeout_note,
                    ),
                )

            validation_commands = self._derive_node_validation_commands(
                node=node,
                workspace_root=workspace_root,
            )
            mark_activity(
                f"derived {len(validation_commands)} node validation command(s)"
            )
            commands_run.extend(validation_commands)
            final_result = CommandResult(
                command=f"node:{node.node_id}",
                return_code=0,
                stdout="Node Level 1 DoD passed.",
                stderr="",
            )
            if validation_commands:
                remaining_timeout = max(
                    0.1,
                    self.node_watchdog_timeout_seconds - (time.monotonic() - started),
                )
                mark_activity(
                    f"running node validations with timeout {remaining_timeout:.2f}s"
                )
                ok, validation_result = self._run_validation(
                    tuple(validation_commands),
                    workspace_root,
                    command_timeout_seconds=remaining_timeout,
                )
                if validation_result is not None:
                    final_result = validation_result
                if not ok:
                    duration = max(time.monotonic() - started, 0.0)
                    note = (
                        f"Node {node.node_id} failed Level 1 validation."
                    )
                    mark_activity(note)
                    return _NodeRunResult(
                        node=node,
                        trace_id=trace_id,
                        status=NodeStatus.FAILED,
                        level1_passed=False,
                        duration_seconds=duration,
                        note=note,
                        rollback_entries=tuple(rollback_map.values()),
                        commands_run=tuple(commands_run),
                        final_result=final_result,
                    )
                mark_activity("node validation commands passed")

            duration = max(time.monotonic() - started, 0.0)
            mark_activity(f"node completed in {duration:.2f}s")
            return _NodeRunResult(
                node=node,
                trace_id=trace_id,
                status=NodeStatus.SUCCESS,
                level1_passed=True,
                duration_seconds=duration,
                note="Node Level 1 DoD passed.",
                rollback_entries=tuple(rollback_map.values()),
                commands_run=tuple(commands_run),
                final_result=final_result,
            )
        finally:
            stop_event.set()
            heartbeat_thread.join(timeout=0.2)

    @staticmethod
    def _build_node_plan(plan: ImplementationPlan, node: ExecutionNode) -> ImplementationPlan:
        return ImplementationPlan(
            feature_name=f"{plan.feature_name}:{node.node_id}",
            summary=node.summary,
            new_files=list(node.new_files),
            modified_files=list(node.modified_files),
            steps=list(node.steps),
            validation_commands=list(node.validation_commands),
            design_guidance=plan.design_guidance,
            dependency_graph=None,
        )

    def _derive_node_validation_commands(
        self,
        *,
        node: ExecutionNode,
        workspace_root: Path,
    ) -> list[str]:
        if self.disable_runtime_checks:
            logger.info(
                "Runtime checks disabled; skipping node-level validation command derivation for %s.",
                node.node_id,
            )
            return []

        commands: list[str] = []
        for command in node.validation_commands:
            cleaned = command.strip()
            if cleaned:
                commands.append(cleaned)

        impacted_tests = self._discover_impacted_test_files(
            modified_files=list(node.modified_files),
            workspace_root=workspace_root,
        )
        commands.extend(self.test_writer.build_validation_commands(impacted_tests))
        commands.extend(
            self._derive_node_static_analysis_commands(
                node=node,
                workspace_root=workspace_root,
            )
        )

        isolated_port = self._allocate_isolated_port()
        injected: list[str] = []
        for command in commands:
            normalized = command.strip()
            if not normalized:
                continue
            if not self.disable_runtime_checks:
                allowlisted, allowlist_note = self._is_command_allowlisted(normalized)
                if not allowlisted:
                    logger.warning(
                        "Skipping non-allowlisted node command for %s: %s",
                        node.node_id,
                        allowlist_note,
                    )
                    continue
            injected.append(
                self._inject_node_runtime_env(
                    command=normalized,
                    node_id=node.node_id,
                    isolated_port=isolated_port,
                )
            )
        return self._unique_values(injected)

    def _derive_node_static_analysis_commands(
        self,
        *,
        node: ExecutionNode,
        workspace_root: Path,
    ) -> list[str]:
        candidates = [*node.new_files, *node.modified_files]
        python_paths: list[str] = []
        for raw_path in candidates:
            resolved = self._resolve_target_path(workspace_root, raw_path)
            if resolved is None:
                continue
            if resolved.suffix.lower() != ".py":
                continue
            try:
                relative = resolved.relative_to(workspace_root).as_posix()
            except ValueError:
                continue
            python_paths.append(relative)

        if not python_paths:
            return []
        quoted_paths = " ".join(shlex.quote(path) for path in sorted(set(python_paths)))
        return [f"python -m py_compile {quoted_paths}"]

    def _capture_artifact_rollbacks(
        self,
        *,
        workspace_root: Path,
        rollback_map: dict[Path, FileRollback],
    ) -> None:
        for artifact in _ROLLBACK_ARTIFACT_CANDIDATES:
            resolved = self._resolve_target_path(workspace_root, artifact)
            if resolved is None:
                continue
            self._ensure_rollback_entry(
                resolved_target=resolved,
                rollback_map=rollback_map,
            )

    @staticmethod
    def _inject_node_runtime_env(
        *,
        command: str,
        node_id: str,
        isolated_port: int,
    ) -> str:
        return (
            f"SENIOR_AGENT_NODE_ID={shlex.quote(node_id)} "
            f"SENIOR_AGENT_TEST_PORT={isolated_port} "
            f"{command}"
        )

    @staticmethod
    def _allocate_isolated_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            return int(sock.getsockname()[1])

    def _resolve_flight_recorder_dir(self, workspace_root: Path) -> Path:
        recorder = (workspace_root / self.flight_recorder_relative_dir).resolve()
        if not is_within_workspace(workspace_root, recorder):
            recorder = (workspace_root / ".senior_agent").resolve()
        recorder.mkdir(parents=True, exist_ok=True)
        return recorder

    @staticmethod
    def _resolve_node_log_path(*, flight_recorder_dir: Path, node_id: str) -> Path:
        safe_node_id = re.sub(r"[^A-Za-z0-9_-]+", "_", node_id).strip("_") or "node"
        return (flight_recorder_dir / f"node_{safe_node_id}.log").resolve()

    @staticmethod
    def _append_node_trace(
        *,
        trace_path: Path,
        trace_id: str,
        node_id: str,
        message: str,
    ) -> None:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"{timestamp} trace={trace_id} node={node_id} {message}\n"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _run_semantic_integrity_check(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> tuple[bool, CommandResult]:
        # Lightweight semantic check: verify changed Python files still parse.
        checked_files: list[str] = []
        for raw_path in [*plan.new_files, *plan.modified_files]:
            resolved = self._resolve_target_path(workspace_root, raw_path)
            if resolved is None:
                return False, CommandResult(
                    command="semantic-integrity",
                    return_code=1,
                    stdout="",
                    stderr=f"Invalid changed file path during semantic check: {raw_path}",
                )
            if not resolved.exists():
                return False, CommandResult(
                    command="semantic-integrity",
                    return_code=1,
                    stdout="",
                    stderr=f"Changed file missing after execution: {resolved}",
                )
            if resolved.suffix.lower() == ".py":
                checked_files.append(resolved.relative_to(workspace_root).as_posix())

        if not checked_files:
            return True, CommandResult(
                command="semantic-integrity",
                return_code=0,
                stdout="No Python files required syntax verification.",
                stderr="",
            )

        command = "python -m py_compile " + " ".join(
            shlex.quote(path) for path in checked_files
        )
        result = self.executor(command, workspace_root)
        return (
            result.return_code == 0,
            CommandResult(
                command="semantic-integrity",
                return_code=result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
            ),
        )

    def _run_semantic_merge_gate(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        validation_commands: tuple[str, ...],
        semantic_integrity_result: CommandResult,
    ) -> tuple[bool, CommandResult]:
        test_commands = tuple(
            command for command in validation_commands if self._is_test_like_command(command)
        )
        if not test_commands:
            return False, CommandResult(
                command="semantic-merge-gate",
                return_code=1,
                stdout="",
                stderr=(
                    "Semantic merge gate requires at least one test command "
                    "(tests + types + formatting)."
                ),
            )

        semantic_integrity_verified_types = (
            semantic_integrity_result.return_code == 0
            and "No Python files required syntax verification."
            not in semantic_integrity_result.stdout
        )
        has_type_signal = any(
            self._is_type_like_command(command) for command in validation_commands
        ) or semantic_integrity_verified_types
        if not has_type_signal:
            return False, CommandResult(
                command="semantic-merge-gate",
                return_code=1,
                stdout="",
                stderr=(
                    "Semantic merge gate requires a type/static-analysis signal "
                    "(tests + types + formatting)."
                ),
            )

        format_commands = tuple(
            self._unique_values(
                [command for command in validation_commands if self._is_format_like_command(command)]
            )
        )
        if format_commands:
            format_ok, format_result = self._run_validation(
                format_commands,
                workspace_root,
            )
            if format_result is None:
                return False, CommandResult(
                    command="semantic-merge-gate",
                    return_code=1,
                    stdout="",
                    stderr="Formatting stage produced no result.",
                )
            if not format_ok:
                return False, CommandResult(
                    command="semantic-merge-gate",
                    return_code=1,
                    stdout=format_result.stdout,
                    stderr=f"Formatting stage failed: {format_result.stderr}",
                )

        inline_format_ok, inline_format_note = self._run_inline_formatting_gate(
            plan=plan,
            workspace_root=workspace_root,
        )
        if not inline_format_ok:
            return False, CommandResult(
                command="semantic-merge-gate",
                return_code=1,
                stdout="",
                stderr=f"Formatting gate failed: {inline_format_note}",
            )

        return True, CommandResult(
            command="semantic-merge-gate",
            return_code=0,
            stdout=(
                "Semantic merge gate passed: tests + types + formatting "
                "requirements satisfied."
            ),
            stderr="",
        )

    def _run_inline_formatting_gate(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> tuple[bool, str]:
        issues: list[str] = []
        seen_paths: set[str] = set()
        changed_files = [*plan.new_files, *plan.modified_files]

        for raw_path in changed_files:
            normalized = raw_path.strip()
            if not normalized or normalized in seen_paths:
                continue
            seen_paths.add(normalized)

            resolved = self._resolve_target_path(workspace_root, normalized)
            if resolved is None:
                return False, f"Invalid changed file path during formatting gate: {normalized}"
            if not resolved.exists() or not resolved.is_file():
                return False, f"Changed file missing during formatting gate: {resolved}"

            if not self._is_inline_format_target(resolved):
                continue

            try:
                raw_bytes = resolved.read_bytes()
            except OSError as exc:
                return False, f"Unable to read file during formatting gate: {resolved} ({exc})"

            if b"\x00" in raw_bytes:
                continue
            try:
                content = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue

            if content and not content.endswith("\n"):
                issues.append(f"{resolved.relative_to(workspace_root)}: missing trailing newline")

            for line_number, line in enumerate(content.splitlines(), start=1):
                if line.endswith((" ", "\t")):
                    issues.append(
                        f"{resolved.relative_to(workspace_root)}:{line_number}: trailing whitespace"
                    )
                    break

            if len(issues) >= _SEMANTIC_MERGE_MAX_FORMAT_ISSUES:
                break

        if issues:
            preview = "; ".join(issues[:_SEMANTIC_MERGE_MAX_FORMAT_ISSUES])
            return False, preview
        return True, "Inline formatting gate passed."

    @staticmethod
    def _is_inline_format_target(path: Path) -> bool:
        suffix = path.suffix.lower()
        return suffix in {
            ".py",
            ".pyi",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".json",
            ".md",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".sh",
            ".css",
            ".scss",
            ".html",
        }

    def _apply_plan(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        rollback_map: dict[Path, FileRollback],
        file_overrides: dict[str, str],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        if self.generation_concurrency <= 1:
            return self._apply_plan_sequential(
                plan=plan,
                workspace_root=workspace_root,
                rollback_map=rollback_map,
                file_overrides=file_overrides,
                style_rules=style_rules,
            )

        return self._apply_plan_parallel(
            plan=plan,
            workspace_root=workspace_root,
            rollback_map=rollback_map,
            file_overrides=file_overrides,
            style_rules=style_rules,
        )

    def _apply_plan_sequential(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        rollback_map: dict[Path, FileRollback],
        file_overrides: dict[str, str],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        for file_path in plan.new_files:
            created_ok, create_note = self._create_new_file(
                plan=plan,
                workspace_root=workspace_root,
                file_path=file_path,
                rollback_map=rollback_map,
                file_overrides=file_overrides,
                style_rules=style_rules,
            )
            if not created_ok:
                return False, create_note

        for file_path in plan.modified_files:
            modified_ok, modify_note = self._modify_existing_file(
                plan=plan,
                workspace_root=workspace_root,
                file_path=file_path,
                rollback_map=rollback_map,
                file_overrides=file_overrides,
                style_rules=style_rules,
            )
            if not modified_ok:
                return False, modify_note

        return True, None

    def _apply_plan_parallel(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        rollback_map: dict[Path, FileRollback],
        file_overrides: dict[str, str],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        # Build all generation requests first, then write to disk only after every
        # generation succeeds to preserve atomic rollback semantics.
        generation_specs: list[tuple[Path, Path, str]] = []
        planned_writes: list[tuple[Path, Path, str]] = []

        for file_path in plan.new_files:
            resolved_target = self._resolve_target_path(workspace_root, file_path)
            if resolved_target is None:
                return False, f"Invalid new-file path in plan: {file_path!r}"

            snap_ok, snap_note = self._ensure_rollback_entry(
                resolved_target=resolved_target,
                rollback_map=rollback_map,
            )
            if not snap_ok:
                return False, snap_note

            snapshot = rollback_map[resolved_target]
            if snapshot.existed_before:
                if snapshot.content is None:
                    return False, f"Rollback snapshot for {resolved_target} is missing file content."
                relative_target = resolved_target.relative_to(workspace_root)
                logger.warning(
                    "Plan declared existing file as new; treating as modified: %s",
                    relative_target,
                )
                override = file_overrides.get(relative_target.as_posix())
                if override is not None:
                    planned_writes.append((resolved_target, relative_target, override))
                    continue
                prompt = self._build_modify_file_prompt(
                    plan=plan,
                    relative_target=relative_target,
                    current_content=snapshot.content,
                    style_rules=style_rules,
                )
                generation_specs.append((resolved_target, relative_target, prompt))
                continue

            relative_target = resolved_target.relative_to(workspace_root)
            override = file_overrides.get(relative_target.as_posix())
            if override is not None:
                planned_writes.append((resolved_target, relative_target, override))
                continue

            prompt = self._build_new_file_prompt(plan, relative_target, style_rules=style_rules)
            generation_specs.append((resolved_target, relative_target, prompt))

        for file_path in plan.modified_files:
            resolved_target = self._resolve_target_path(workspace_root, file_path)
            if resolved_target is None:
                return False, f"Invalid modified-file path in plan: {file_path!r}"

            snap_ok, snap_note = self._ensure_rollback_entry(
                resolved_target=resolved_target,
                rollback_map=rollback_map,
            )
            if not snap_ok:
                return False, snap_note

            snapshot = rollback_map[resolved_target]
            if not snapshot.existed_before:
                return False, f"Planned modified file does not exist: {resolved_target}"
            if snapshot.content is None:
                return False, f"Rollback snapshot for {resolved_target} is missing file content."

            relative_target = resolved_target.relative_to(workspace_root)
            override = file_overrides.get(relative_target.as_posix())
            if override is not None:
                planned_writes.append((resolved_target, relative_target, override))
                continue
            prompt = self._build_modify_file_prompt(
                plan=plan,
                relative_target=relative_target,
                current_content=snapshot.content,
                style_rules=style_rules,
            )
            generation_specs.append((resolved_target, relative_target, prompt))

        generated_writes, generation_error = self._generate_contents_parallel(generation_specs)
        if generation_error is not None:
            return False, generation_error
        planned_writes.extend(generated_writes)

        for resolved_target, relative_target, generated in planned_writes:
            try:
                resolved_target.parent.mkdir(parents=True, exist_ok=True)
                resolved_target.write_text(generated, encoding="utf-8")
            except (OSError, UnicodeEncodeError) as exc:
                return False, f"Failed to write file {resolved_target}: {exc}"
            logger.info("Updated file: %s", relative_target)

        return True, None

    def _generate_contents_parallel(
        self,
        generation_specs: list[tuple[Path, Path, str]],
    ) -> tuple[list[tuple[Path, Path, str]], str | None]:
        if not generation_specs:
            return [], None

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.warning(
                "Event loop already running; falling back to sequential generation for safety."
            )
            return self._generate_contents_sequential(generation_specs)

        return asyncio.run(self._generate_contents_parallel_async(generation_specs))

    async def _generate_contents_parallel_async(
        self,
        generation_specs: list[tuple[Path, Path, str]],
    ) -> tuple[list[tuple[Path, Path, str]], str | None]:
        semaphore = asyncio.Semaphore(self.generation_concurrency)

        async def generate_one(
            index: int,
            resolved_target: Path,
            relative_target: Path,
            prompt: str,
        ) -> tuple[int, Path, Path, str | None]:
            async with semaphore:
                generated = await asyncio.to_thread(
                    self._generate_code,
                    prompt,
                    relative_target,
                )
                if generated is not None and self.enable_self_critique:
                    generated = await asyncio.to_thread(
                        self._self_critique_generated_code,
                        relative_target,
                        generated,
                    )
            return index, resolved_target, relative_target, generated

        tasks = [
            generate_one(index, resolved_target, relative_target, prompt)
            for index, (resolved_target, relative_target, prompt) in enumerate(generation_specs)
        ]
        generated_results = await asyncio.gather(*tasks)
        generated_results.sort(key=lambda item: item[0])

        writes: list[tuple[Path, Path, str]] = []
        for _, resolved_target, relative_target, generated in generated_results:
            if generated is None:
                return (
                    [],
                    f"LLM generation returned no usable code for {relative_target}.",
                )
            writes.append((resolved_target, relative_target, generated))
        return writes, None

    def _generate_contents_sequential(
        self,
        generation_specs: list[tuple[Path, Path, str]],
    ) -> tuple[list[tuple[Path, Path, str]], str | None]:
        writes: list[tuple[Path, Path, str]] = []
        for resolved_target, relative_target, prompt in generation_specs:
            generated = self._generate_code(prompt, relative_target)
            if generated is None:
                return [], f"LLM generation returned no usable code for {relative_target}."
            writes.append((resolved_target, relative_target, generated))
        return writes, None

    def _create_new_file(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        file_path: str,
        rollback_map: dict[Path, FileRollback],
        file_overrides: dict[str, str],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        resolved_target = self._resolve_target_path(workspace_root, file_path)
        if resolved_target is None:
            return False, f"Invalid new-file path in plan: {file_path!r}"

        snap_ok, snap_note = self._ensure_rollback_entry(
            resolved_target=resolved_target,
            rollback_map=rollback_map,
        )
        if not snap_ok:
            return False, snap_note

        snapshot = rollback_map[resolved_target]
        if snapshot.existed_before:
            relative_target = resolved_target.relative_to(workspace_root)
            logger.warning(
                "Plan declared existing file as new; treating as modified: %s",
                relative_target,
            )
            return self._modify_existing_file(
                plan=plan,
                workspace_root=workspace_root,
                file_path=file_path,
                rollback_map=rollback_map,
                file_overrides=file_overrides,
                style_rules=style_rules,
            )

        relative_target = resolved_target.relative_to(workspace_root)
        generated = file_overrides.get(relative_target.as_posix())
        if generated is None:
            prompt = self._build_new_file_prompt(plan, relative_target, style_rules=style_rules)
            generated = self._generate_code(prompt, relative_target)
            if generated is None:
                return False, f"LLM generation returned no usable code for new file {relative_target}."

        try:
            resolved_target.parent.mkdir(parents=True, exist_ok=True)
            resolved_target.write_text(generated, encoding="utf-8")
        except (OSError, UnicodeEncodeError) as exc:
            return False, f"Failed to write new file {resolved_target}: {exc}"

        logger.info("Created new file: %s", relative_target)
        return True, None

    def _augment_plan_with_symbol_graph_validation(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> ImplementationPlan:
        if not plan.modified_files:
            return plan

        impacted_test_files = self._discover_impacted_test_files(
            modified_files=plan.modified_files,
            workspace_root=workspace_root,
        )
        if not impacted_test_files:
            return plan

        proactive_commands = self.test_writer.build_validation_commands(impacted_test_files)
        if not proactive_commands:
            return plan

        merged_validation_commands = list(plan.validation_commands)
        added_commands = 0
        for command in proactive_commands:
            cleaned = command.strip()
            if cleaned and cleaned not in merged_validation_commands:
                merged_validation_commands.append(cleaned)
                added_commands += 1

        if added_commands == 0:
            return plan

        logger.info(
            "Symbol graph added proactive validation commands: impacted_tests=%s added_commands=%s",
            len(impacted_test_files),
            added_commands,
        )
        return ImplementationPlan(
            feature_name=plan.feature_name,
            summary=plan.summary,
            new_files=list(plan.new_files),
            modified_files=list(plan.modified_files),
            steps=list(plan.steps),
            validation_commands=merged_validation_commands,
            design_guidance=plan.design_guidance,
        )

    def _discover_impacted_test_files(
        self,
        *,
        modified_files: list[str],
        workspace_root: Path,
    ) -> list[str]:
        impacted_tests: list[str] = []
        seen_tests: set[str] = set()

        for raw_modified_file in modified_files:
            resolved_modified = self._resolve_target_path(workspace_root, raw_modified_file)
            if resolved_modified is None:
                continue

            symbols = self.symbol_graph.get_defined_symbols(resolved_modified)
            if not symbols:
                continue

            for symbol_name in symbols:
                dependent_files = self.symbol_graph.get_dependents(
                    resolved_modified,
                    symbol_name,
                )
                for dependent_file in dependent_files:
                    candidate_paths = self._candidate_test_paths_for_source(
                        source_file=dependent_file,
                        workspace_root=workspace_root,
                    )
                    for candidate in candidate_paths:
                        if candidate in seen_tests:
                            continue
                        seen_tests.add(candidate)
                        impacted_tests.append(candidate)

        return impacted_tests

    def _candidate_test_paths_for_source(
        self,
        *,
        source_file: Path,
        workspace_root: Path,
    ) -> list[str]:
        if not is_within_workspace(workspace_root, source_file):
            return []
        if not source_file.exists() or not source_file.is_file():
            return []

        suffix = source_file.suffix.lower()
        stem = source_file.stem
        candidates: list[Path] = []

        if suffix == ".py":
            candidates.extend(
                [
                    workspace_root / "tests" / f"test_{stem}.py",
                    source_file.parent / f"test_{stem}.py",
                ]
            )
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            candidates.extend(
                [
                    workspace_root / "tests" / f"{stem}.test{suffix}",
                    source_file.parent / f"{stem}.test{suffix}",
                ]
            )
        elif suffix == ".go":
            candidates.extend(
                [
                    source_file.parent / f"{stem}_test.go",
                    workspace_root / "tests" / f"{stem}_test.go",
                ]
            )
        else:
            return []

        resolved_candidates: list[str] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if not is_within_workspace(workspace_root, resolved):
                continue
            if not resolved.exists() or not resolved.is_file():
                continue
            relative = resolved.relative_to(workspace_root).as_posix()
            resolved_candidates.append(relative)
        return resolved_candidates

    def _augment_plan_with_generated_tests(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> tuple[ImplementationPlan, dict[str, str], str | None]:
        if not plan.new_files:
            return plan, {}, None

        files_content, context_note = self._collect_files_content_for_test_generation(
            plan=plan,
            workspace_root=workspace_root,
        )
        if context_note is not None:
            return plan, {}, context_note

        try:
            generated_tests = self.test_writer.generate_test_suite(plan, files_content)
            validation_additions = self.test_writer.build_validation_commands(generated_tests.keys())
        except (LLMClientError, ValueError) as exc:
            return plan, {}, f"Test suite generation failed: {exc}"
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Unexpected test-writer failure: %s", exc)
            return plan, {}, f"Unexpected test suite generation failure: {exc}"

        if not generated_tests:
            return plan, {}, None

        normalized_tests: dict[str, str] = {}
        for raw_path, raw_content in generated_tests.items():
            candidate = str(raw_path).strip()
            if not candidate:
                continue
            resolved = self._resolve_target_path(workspace_root, candidate)
            if resolved is None:
                return plan, {}, f"Generated test file path is invalid: {candidate!r}"

            relative = resolved.relative_to(workspace_root).as_posix()
            normalized_content = self._normalize_generated_content(str(raw_content))
            if not normalized_content.strip():
                return plan, {}, f"Generated test file is empty: {relative}"
            if not normalized_content.endswith("\n"):
                normalized_content = f"{normalized_content}\n"
            normalized_tests[relative] = normalized_content

        if not normalized_tests:
            return plan, {}, None

        new_files = list(plan.new_files)
        for generated_test_path in normalized_tests:
            if generated_test_path not in new_files:
                new_files.append(generated_test_path)

        validation_commands = list(plan.validation_commands)
        for validation_command in validation_additions:
            cleaned = validation_command.strip()
            if cleaned and cleaned not in validation_commands:
                validation_commands.append(cleaned)

        updated_plan = ImplementationPlan(
            feature_name=plan.feature_name,
            summary=plan.summary,
            new_files=new_files,
            modified_files=list(plan.modified_files),
            steps=list(plan.steps),
            validation_commands=validation_commands,
            design_guidance=plan.design_guidance,
        )
        logger.info(
            "TDD generated test files: files=%s commands=%s",
            len(normalized_tests),
            len(validation_additions),
        )
        return updated_plan, normalized_tests, None

    def _collect_files_content_for_test_generation(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> tuple[dict[str, str], str | None]:
        collected: dict[str, str] = {}
        candidates = list(dict.fromkeys((*plan.new_files, *plan.modified_files)))

        for raw_path in candidates:
            candidate = str(raw_path).strip()
            if not candidate:
                continue

            resolved = self._resolve_target_path(workspace_root, candidate)
            if resolved is None:
                return {}, f"Invalid file path in plan while preparing tests: {candidate!r}"

            content = ""
            if resolved.exists():
                if not resolved.is_file():
                    return {}, f"Test context path is not a regular file: {resolved}"
                try:
                    content = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return {}, f"Failed to read file for test context {resolved}: {exc}"

            collected[candidate] = content
            collected[resolved.relative_to(workspace_root).as_posix()] = content

        return collected, None

    def _modify_existing_file(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        file_path: str,
        rollback_map: dict[Path, FileRollback],
        file_overrides: dict[str, str],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        resolved_target = self._resolve_target_path(workspace_root, file_path)
        if resolved_target is None:
            return False, f"Invalid modified-file path in plan: {file_path!r}"

        snap_ok, snap_note = self._ensure_rollback_entry(
            resolved_target=resolved_target,
            rollback_map=rollback_map,
        )
        if not snap_ok:
            return False, snap_note

        snapshot = rollback_map[resolved_target]
        if not snapshot.existed_before:
            return False, f"Planned modified file does not exist: {resolved_target}"
        if snapshot.content is None:
            return False, f"Rollback snapshot for {resolved_target} is missing file content."

        relative_target = resolved_target.relative_to(workspace_root)
        generated = file_overrides.get(relative_target.as_posix())
        if generated is None:
            prompt = self._build_modify_file_prompt(
                plan=plan,
                relative_target=relative_target,
                current_content=snapshot.content,
                style_rules=style_rules,
            )
            generated = self._generate_code(prompt, relative_target)
            if generated is None:
                return False, f"LLM generation returned no usable code for modified file {relative_target}."

        try:
            resolved_target.write_text(generated, encoding="utf-8")
        except (OSError, UnicodeEncodeError) as exc:
            return False, f"Failed to write modified file {resolved_target}: {exc}"

        logger.info("Updated file: %s", relative_target)
        return True, None

    def _self_critique_generated_code(self, relative_target: Path, generated: str) -> str:
        prompt = (
            "Role: Senior Code Critic.\n"
            f"Task: Review this code for {relative_target} for syntax errors, logical flaws, "
            "and missing imports.\n"
            "Return ONLY the final corrected code. No markdown fences.\n\n"
            f"Code under review ({relative_target}):\n"
            "--- BEGIN CODE ---\n"
            f"{generated}\n"
            "--- END CODE ---\n"
        )
        try:
            raw_output = self.llm_client.generate_fix(prompt)
        except LLMClientError as exc:
            logger.warning(
                "Self-critique skipped due to LLM error for %s: %s",
                relative_target,
                exc,
            )
            return generated
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception(
                "Unexpected self-critique failure for %s: %s",
                relative_target,
                exc,
            )
            return generated

        normalized = self._normalize_generated_content(raw_output)
        if not normalized.strip():
            logger.warning("Self-critique returned empty output for %s; using original generation.", relative_target)
            return generated
        if not normalized.endswith("\n"):
            normalized = f"{normalized}\n"
        return normalized

    def _ensure_rollback_entry(
        self,
        *,
        resolved_target: Path,
        rollback_map: dict[Path, FileRollback],
    ) -> tuple[bool, str | None]:
        if resolved_target in rollback_map:
            return True, None

        if resolved_target.exists():
            if not resolved_target.is_file():
                return False, f"Rollback snapshot target is not a regular file: {resolved_target}"
            try:
                content = resolved_target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return False, f"Failed to capture rollback snapshot for {resolved_target}: {exc}"
            rollback_map[resolved_target] = FileRollback(
                path=resolved_target,
                existed_before=True,
                content=content,
            )
        else:
            rollback_map[resolved_target] = FileRollback(
                path=resolved_target,
                existed_before=False,
                content=None,
            )

        return True, None

    def _critical_failure_and_rollback(
        self,
        *,
        reason: str,
        workspace_root: Path,
        rollback_entries: tuple[FileRollback, ...],
    ) -> None:
        logger.critical(reason)
        if not rollback_entries:
            logger.critical("No rollback snapshots captured; no atomic recovery could be performed.")
            return

        rollback_success, rollback_note = self._rollback_agent._rollback_changes(
            workspace=workspace_root,
            rollback_entries=rollback_entries,
        )
        if rollback_success:
            logger.critical("Atomic rollback succeeded: %s", rollback_note)
            return
        logger.critical("Atomic rollback failed: %s", rollback_note)

    @staticmethod
    def _build_requirement_hash(requirement: str) -> str:
        normalized = requirement.strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _resolve_fix_cache_path(self, workspace_root: Path) -> Path | None:
        cache_path = (workspace_root / self.fix_cache_relative_path).resolve()
        if not is_within_workspace(workspace_root, cache_path):
            logger.error(
                "Blocked out-of-workspace fix-cache path: workspace=%s target=%s",
                workspace_root,
                cache_path,
            )
            return None
        return cache_path

    def _load_fix_cache_entries(self, workspace_root: Path) -> dict[str, dict[str, object]]:
        cache_path = self._resolve_fix_cache_path(workspace_root)
        if cache_path is None or not cache_path.exists():
            return {}
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, dict):
            return {}

        entries: dict[str, dict[str, object]] = {}
        for key, value in raw_entries.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            entries[key] = value
        return entries

    def _write_fix_cache_entries(
        self,
        workspace_root: Path,
        entries: dict[str, dict[str, object]],
    ) -> None:
        cache_path = self._resolve_fix_cache_path(workspace_root)
        if cache_path is None:
            return
        payload = {"version": 1, "entries": entries}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Unable to persist fix cache at %s: %s", cache_path, exc)

    def _load_cached_fix_outputs(
        self,
        *,
        requirement: str,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> dict[str, str]:
        if not self.enable_fix_cache:
            return {}

        entries = self._load_fix_cache_entries(workspace_root)
        key = self._build_requirement_hash(requirement)
        entry = entries.get(key)
        if not isinstance(entry, dict):
            return {}

        files = entry.get("files")
        if not isinstance(files, dict):
            return {}

        eligible_paths = set(plan.new_files) | set(plan.modified_files)
        overrides: dict[str, str] = {}
        for raw_path, raw_content in files.items():
            if not isinstance(raw_path, str) or raw_path not in eligible_paths:
                continue
            if not isinstance(raw_content, str):
                continue
            normalized = raw_content if raw_content.endswith("\n") else f"{raw_content}\n"
            overrides[raw_path] = normalized

        if overrides:
            logger.info(
                "Fix cache hit for requirement hash %s: %s file(s) reused.",
                key[:12],
                len(overrides),
            )
        return overrides

    def _store_successful_fix_cache_entry(
        self,
        *,
        requirement: str,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> None:
        if not self.enable_fix_cache:
            return

        changed_paths = list(dict.fromkeys([*plan.new_files, *plan.modified_files]))
        if not changed_paths:
            return

        cached_files: dict[str, str] = {}
        for raw_path in changed_paths:
            resolved = self._resolve_target_path(workspace_root, raw_path)
            if resolved is None or not resolved.exists() or not resolved.is_file():
                continue
            try:
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if len(content) > self.max_fix_cache_file_chars:
                continue
            relative = resolved.relative_to(workspace_root).as_posix()
            cached_files[relative] = content

        if not cached_files:
            return

        entries = self._load_fix_cache_entries(workspace_root)
        key = self._build_requirement_hash(requirement)
        entries[key] = {
            "feature_name": plan.feature_name,
            "summary": plan.summary,
            "files": cached_files,
        }
        while len(entries) > self.max_fix_cache_entries:
            oldest_key = next(iter(entries))
            del entries[oldest_key]
        self._write_fix_cache_entries(workspace_root, entries)
        logger.info(
            "Persisted fix cache entry for requirement hash %s with %s file(s).",
            key[:12],
            len(cached_files),
        )

    def _resolve_target_path(self, workspace_root: Path, raw_path: str) -> Path | None:
        candidate_raw = str(raw_path).strip()
        if not candidate_raw:
            logger.error("Encountered empty file path in implementation plan.")
            return None

        candidate = Path(candidate_raw)
        resolved = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
        if not is_within_workspace(workspace_root, resolved):
            logger.error(
                "Blocked out-of-workspace file operation: workspace=%s target=%s",
                workspace_root,
                resolved,
            )
            return None
        return resolved

    def _generate_code(self, prompt: str, relative_target: Path) -> str | None:
        try:
            raw_output = self.llm_client.generate_fix(prompt)
        except LLMClientError as exc:
            logger.error("LLM generation failed for %s: %s", relative_target, exc)
            return None
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Unexpected LLM failure for %s: %s", relative_target, exc)
            return None

        normalized = self._normalize_generated_content(raw_output)
        if not normalized.strip():
            logger.error("LLM returned empty code for %s", relative_target)
            return None
        if not normalized.endswith("\n"):
            normalized = f"{normalized}\n"
        return normalized

    @staticmethod
    def _normalize_generated_content(raw_output: str) -> str:
        stripped = raw_output.strip()
        match = CODE_FENCE_PATTERN.search(stripped)
        if match:
            return match.group("code").strip()
        return stripped

    def _check_environment(self, commands: list[str]) -> bool:
        seen_binaries: set[str] = set()
        for command in commands:
            command_text = command.strip()
            if not command_text:
                continue

            allowlisted, allowlist_note = self._is_command_allowlisted(command_text)
            if not allowlisted:
                logger.critical("Validation command rejected by allowlist: %s", allowlist_note)
                return False

            try:
                tokens = shlex.split(command_text)
            except ValueError as exc:
                logger.critical(
                    "Could not parse validation command for environment check: command=%s error=%s",
                    command_text,
                    exc,
                )
                return False
            if not tokens:
                continue

            binary = tokens[0]
            if binary in seen_binaries:
                continue
            seen_binaries.add(binary)

            if shutil.which(binary) is None:
                logger.critical(
                    "Missing validation binary required for orchestrator execution: binary=%s command=%s",
                    binary,
                    command_text,
                )
                return False

        return True

    def _run_validation(
        self,
        commands: tuple[str, ...],
        workspace_root: Path,
        command_timeout_seconds: float | None = None,
    ) -> tuple[bool, CommandResult | None]:
        effective_timeout = self._resolve_validation_timeout(command_timeout_seconds)
        daemon_enabled = (
            self.enable_persistent_daemons and self._supports_validation_daemon()
        )
        cache_enabled = self.enable_persistent_daemons and not daemon_enabled
        last_result: CommandResult | None = None
        for command in commands:
            cache_key = command.strip()
            if cache_enabled:
                cached_entry = self._validation_daemon_cache.get(cache_key)
                if cached_entry is not None:
                    cached_at, cached_result = cached_entry
                    if (
                        time.monotonic() - cached_at <= self.daemon_cache_ttl_seconds
                        and cached_result.return_code == 0
                    ):
                        logger.info(
                            "Persistent daemon cache hit for validation command: %s",
                            command,
                        )
                        last_result = cached_result
                        continue
                    self._validation_daemon_cache.pop(cache_key, None)

            logger.info("Running orchestrator validation command: %s (cwd=%s)", command, workspace_root)
            result: CommandResult | None = None
            if daemon_enabled:
                result = self._execute_validation_command_via_daemon(
                    command=command,
                    workspace_root=workspace_root,
                    command_timeout_seconds=effective_timeout,
                )
            if result is None:
                result = self._execute_validation_command(
                    command=command,
                    workspace_root=workspace_root,
                    command_timeout_seconds=effective_timeout,
                )
            if result is None:
                return False, last_result

            last_result = result

            if result.return_code != 0:
                dependency_fixed = self.dependency_manager.check_and_fix_dependencies(
                    result=result,
                    workspace=workspace_root,
                )
                if dependency_fixed:
                    logger.info(
                        "Retrying validation command after dependency auto-install: %s",
                        command,
                    )
                    retry_result: CommandResult | None = None
                    if daemon_enabled:
                        retry_result = self._execute_validation_command_via_daemon(
                            command=command,
                            workspace_root=workspace_root,
                            command_timeout_seconds=effective_timeout,
                        )
                    if retry_result is None:
                        retry_result = self._execute_validation_command(
                            command=command,
                            workspace_root=workspace_root,
                            command_timeout_seconds=effective_timeout,
                        )
                    if retry_result is None:
                        return False, result
                    last_result = retry_result
                    if retry_result.return_code == 0:
                        if cache_enabled:
                            self._validation_daemon_cache[cache_key] = (
                                time.monotonic(),
                                retry_result,
                            )
                        continue
                    result = retry_result

                logger.error(
                    "Validation command failed: command=%s return_code=%s stderr=%s",
                    command,
                    result.return_code,
                    result.stderr.strip(),
                )
                return False, result
            if cache_enabled:
                self._validation_daemon_cache[cache_key] = (
                    time.monotonic(),
                    result,
                )

        logger.info("All orchestrator validation commands passed (%s).", len(commands))
        return True, last_result

    def _supports_validation_daemon(self) -> bool:
        return self.executor is run_shell_command

    def _resolve_validation_timeout(
        self,
        command_timeout_seconds: float | None,
    ) -> float | None:
        if command_timeout_seconds is not None:
            return max(_MIN_COMMAND_TIMEOUT_SECONDS, float(command_timeout_seconds))
        if self.validation_command_timeout_seconds is not None:
            return max(
                _MIN_COMMAND_TIMEOUT_SECONDS,
                float(self.validation_command_timeout_seconds),
            )
        return None

    def _shutdown_validation_daemons(self) -> None:
        for workspace_key, state in list(self._validation_daemons.items()):
            self._stop_validation_daemon(
                workspace_key=workspace_key,
                state=state,
                reason="shutdown",
            )
        self._validation_daemons.clear()

    def _get_or_start_validation_daemon(self, workspace_root: Path) -> _ValidationDaemonState | None:
        if not self._supports_validation_daemon():
            return None

        workspace_key = workspace_root.resolve()
        state = self._validation_daemons.get(workspace_key)
        now = time.monotonic()

        if state is not None:
            idle_seconds = now - state.last_used_at
            if state.process.poll() is not None or idle_seconds > self.daemon_cache_ttl_seconds:
                self._stop_validation_daemon(
                    workspace_key=workspace_key,
                    state=state,
                    reason="stale-or-dead",
                )
                state = None
                self._validation_daemons.pop(workspace_key, None)

        if state is None:
            state = self._start_validation_daemon(workspace_key)
            if state is not None:
                self._validation_daemons[workspace_key] = state
        return state

    def _start_validation_daemon(self, workspace_root: Path) -> _ValidationDaemonState | None:
        env = os.environ.copy()
        src_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = env.get("PYTHONPATH", "")
        if src_root not in existing_pythonpath.split(os.pathsep):
            env["PYTHONPATH"] = (
                f"{src_root}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else src_root
            )

        try:
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", _VALIDATION_DAEMON_MODULE],
                cwd=str(workspace_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=os.name != "nt",
                env=env,
            )
        except OSError as exc:
            logger.warning("Unable to start validation daemon: %s", exc)
            return None

        if process.stdin is None or process.stdout is None:
            self._terminate_process_tree(process, grace_seconds=self.watchdog_kill_grace_seconds)
            return None

        state = _ValidationDaemonState(
            workspace_root=workspace_root,
            process=process,
            lock=threading.Lock(),
            last_used_at=time.monotonic(),
        )
        ping_response = self._send_validation_daemon_request(
            state=state,
            payload={"action": "ping"},
            timeout_seconds=self.daemon_startup_timeout_seconds,
        )
        if not isinstance(ping_response, dict) or ping_response.get("status") != "ok":
            self._stop_validation_daemon(
                workspace_key=workspace_root,
                state=state,
                reason="startup-ping-failed",
            )
            return None
        return state

    def _stop_validation_daemon(
        self,
        *,
        workspace_key: Path,
        state: _ValidationDaemonState,
        reason: str,
    ) -> None:
        try:
            if state.process.poll() is None:
                self._send_validation_daemon_request(
                    state=state,
                    payload={"action": "shutdown"},
                    timeout_seconds=min(1.0, self.watchdog_kill_grace_seconds),
                )
        except Exception:
            pass
        self._terminate_process_tree(
            state.process,
            grace_seconds=self.watchdog_kill_grace_seconds,
        )
        self._validation_daemons.pop(workspace_key, None)
        logger.info("Stopped validation daemon: workspace=%s reason=%s", workspace_key, reason)

    def _send_validation_daemon_request(
        self,
        *,
        state: _ValidationDaemonState,
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, object] | None:
        process = state.process
        if process.stdin is None or process.stdout is None:
            return None

        request_id = uuid4().hex
        envelope = dict(payload)
        envelope["request_id"] = request_id

        with state.lock:
            if process.poll() is not None:
                return None
            try:
                process.stdin.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except OSError:
                return None

            raw_line = self._read_line_with_timeout(
                stream=process.stdout,
                timeout_seconds=max(_MIN_COMMAND_TIMEOUT_SECONDS, timeout_seconds),
            )
            if raw_line is None:
                return None
            try:
                response = json.loads(raw_line.strip())
            except json.JSONDecodeError:
                return None
            if not isinstance(response, dict):
                return None
            if str(response.get("request_id", "")).strip() != request_id:
                return None

        state.last_used_at = time.monotonic()
        return response

    def _execute_validation_command_via_daemon(
        self,
        *,
        command: str,
        workspace_root: Path,
        command_timeout_seconds: float | None,
    ) -> CommandResult | None:
        state = self._get_or_start_validation_daemon(workspace_root)
        if state is None:
            return None

        effective_timeout = command_timeout_seconds
        response_timeout = (
            (effective_timeout or self.node_watchdog_timeout_seconds)
            + self.watchdog_kill_grace_seconds
            + _DAEMON_RESPONSE_PADDING_SECONDS
        )
        response = self._send_validation_daemon_request(
            state=state,
            payload={
                "action": "run",
                "command": command,
                "cwd": str(workspace_root),
                "timeout_seconds": effective_timeout,
            },
            timeout_seconds=response_timeout,
        )
        if response is None:
            workspace_key = workspace_root.resolve()
            self._stop_validation_daemon(
                workspace_key=workspace_key,
                state=state,
                reason="unresponsive",
            )
            timeout_hint = (
                f"Validation daemon became unresponsive after {response_timeout:.1f}s "
                "(watchdog reaper hard-kill)."
            )
            return CommandResult(
                command=command,
                return_code=124,
                stdout="",
                stderr=timeout_hint,
            )

        return CommandResult(
            command=command,
            return_code=int(response.get("return_code", 1)),
            stdout=str(response.get("stdout", "")),
            stderr=str(response.get("stderr", "")),
        )

    def _execute_validation_command(
        self,
        *,
        command: str,
        workspace_root: Path,
        command_timeout_seconds: float | None,
    ) -> CommandResult | None:
        timeout_value = self._resolve_validation_timeout(command_timeout_seconds)

        if timeout_value is None:
            try:
                return self.executor(command, workspace_root)
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception("Validation command raised error: command=%s error=%s", command, exc)
                return None

        if self.executor is run_shell_command:
            return self._run_shell_command_with_reaper(
                command=command,
                workspace_root=workspace_root,
                timeout_seconds=timeout_value,
            )

        # Preserve executor-side stateful behavior for callable instances used in tests
        # and embedded integrations; hard-kill subprocess wrapping is reserved for
        # plain function executors.
        if inspect.isfunction(self.executor):
            subprocess_result = self._execute_validation_command_in_subprocess(
                command=command,
                workspace_root=workspace_root,
                timeout_seconds=timeout_value,
            )
            if subprocess_result is not None:
                return subprocess_result

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.executor, command, workspace_root)
            try:
                return future.result(timeout=timeout_value)
            except FuturesTimeoutError:
                future.cancel()
                return CommandResult(
                    command=command,
                    return_code=124,
                    stdout="",
                    stderr=(
                        f"Validation command timed out after {timeout_value:.1f}s "
                        "(watchdog reaper; hard-kill unavailable for current executor)."
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception("Validation command raised error: command=%s error=%s", command, exc)
                return None

    def _run_shell_command_with_reaper(
        self,
        *,
        command: str,
        workspace_root: Path,
        timeout_seconds: float,
    ) -> CommandResult:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(workspace_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return CommandResult(
                command=command,
                return_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_tree(
                process,
                grace_seconds=self.watchdog_kill_grace_seconds,
            )
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            timeout_note = (
                f"Validation command timed out after {timeout_seconds:.1f}s "
                "(watchdog reaper hard-killed process tree)."
            )
            merged_stderr = timeout_note if not stderr else f"{stderr.rstrip()}\n{timeout_note}"
            return CommandResult(
                command=command,
                return_code=124,
                stdout=stdout,
                stderr=merged_stderr,
            )

    def _execute_validation_command_in_subprocess(
        self,
        *,
        command: str,
        workspace_root: Path,
        timeout_seconds: float,
    ) -> CommandResult | None:
        if os.name == "nt":
            return None
        try:
            context = mp.get_context("fork")
        except ValueError:
            return None

        queue = context.Queue(maxsize=1)
        process = context.Process(
            target=_execute_in_subprocess,
            args=(self.executor, command, str(workspace_root), queue),
            daemon=True,
        )
        process.start()
        process.join(timeout=timeout_seconds)

        if process.is_alive():
            process.terminate()
            process.join(timeout=self.watchdog_kill_grace_seconds)
            if process.is_alive():
                process.kill()
                process.join(timeout=self.watchdog_kill_grace_seconds)
            try:
                queue.close()
                queue.join_thread()
            except Exception:
                pass
            return CommandResult(
                command=command,
                return_code=124,
                stdout="",
                stderr=(
                    f"Validation command timed out after {timeout_seconds:.1f}s "
                    "(watchdog reaper hard-killed subprocess executor)."
                ),
            )

        payload: Any = None
        try:
            if not queue.empty():
                payload = queue.get_nowait()
        except Exception:
            payload = None
        finally:
            try:
                queue.close()
                queue.join_thread()
            except Exception:
                pass

        if isinstance(payload, dict) and payload.get("ok") is True:
            return CommandResult(
                command=str(payload.get("command", command)),
                return_code=int(payload.get("return_code", 1)),
                stdout=str(payload.get("stdout", "")),
                stderr=str(payload.get("stderr", "")),
            )
        if isinstance(payload, dict):
            return CommandResult(
                command=command,
                return_code=1,
                stdout="",
                stderr=(
                    "Validation executor subprocess failed: "
                    f"{str(payload.get('error', 'unknown error'))}"
                ),
            )
        if process.exitcode not in (None, 0):
            return CommandResult(
                command=command,
                return_code=1,
                stdout="",
                stderr=f"Validation executor subprocess exited with code {process.exitcode}.",
            )
        return CommandResult(
            command=command,
            return_code=1,
            stdout="",
            stderr="Validation executor subprocess returned no payload.",
        )

    @staticmethod
    def _read_line_with_timeout(
        *,
        stream: TextIO,
        timeout_seconds: float,
    ) -> str | None:
        holder: dict[str, str | None] = {"line": None}
        completed = threading.Event()

        def reader() -> None:
            try:
                holder["line"] = stream.readline()
            except Exception:  # pragma: no cover - defensive guardrail
                holder["line"] = None
            finally:
                completed.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        if not completed.wait(timeout_seconds):
            return None
        line = holder["line"]
        if not line:
            return None
        return line

    @staticmethod
    def _terminate_process_tree(
        process: subprocess.Popen[str],
        *,
        grace_seconds: float,
    ) -> None:
        if process.poll() is not None:
            return

        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

        try:
            process.wait(timeout=max(_MIN_COMMAND_TIMEOUT_SECONDS, grace_seconds))
            return
        except subprocess.TimeoutExpired:
            pass

        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=max(_MIN_COMMAND_TIMEOUT_SECONDS, grace_seconds))
        except subprocess.TimeoutExpired:
            pass

    def _autodetect_validation_commands(self, workspace_root: Path) -> list[str]:
        commands: list[str] = []
        package_json = workspace_root / "package.json"
        if package_json.exists() and package_json.is_file():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if isinstance(scripts, dict):
                if isinstance(scripts.get("test"), str):
                    commands.append("npm test")
                if isinstance(scripts.get("lint"), str):
                    commands.append("npm run lint")

        if (workspace_root / "go.mod").exists():
            commands.append("go test ./...")
        if (workspace_root / "Cargo.toml").exists():
            commands.append("cargo test")
        if self._has_pytest_config(workspace_root):
            commands.append("pytest")
        if (workspace_root / "tests").exists():
            commands.append("python -m unittest discover -s tests -v")

        unique_commands: list[str] = []
        seen: set[str] = set()
        for command in commands:
            normalized = command.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_commands.append(normalized)
        return unique_commands

    @staticmethod
    def _has_pytest_config(workspace_root: Path) -> bool:
        if (workspace_root / "pytest.ini").exists():
            return True

        tox_ini = workspace_root / "tox.ini"
        if tox_ini.exists():
            try:
                if "[pytest]" in tox_ini.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                return False

        pyproject = workspace_root / "pyproject.toml"
        if pyproject.exists():
            try:
                if "[tool.pytest.ini_options]" in pyproject.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                return False

        return False

    def _build_session_report(
        self,
        *,
        command: str,
        final_result: CommandResult,
        success: bool,
        blocked_reason: str | None,
        node_records: list[NodeExecutionRecord] | None = None,
        telemetry: OrchestrationTelemetry | None = None,
    ) -> SessionReport:
        initial_result = CommandResult(
            command=command,
            return_code=0,
            stdout="Feature request accepted.",
            stderr="",
        )
        return SessionReport(
            command=command,
            initial_result=initial_result,
            final_result=final_result,
            attempts=[],
            node_records=list(node_records or []),
            telemetry=telemetry,
            success=success,
            blocked_reason=blocked_reason,
        )

    def _emit_visual_summary(
        self,
        *,
        plan: ImplementationPlan,
        report: SessionReport,
        workspace_root: Path,
    ) -> None:
        stage = "succeeded" if report.success else "failed"
        safe_stem = self._safe_feature_stem(plan.feature_name)
        mermaid: str | None = None
        try:
            mermaid = self.visual_reporter.generate_mermaid_summary(plan, report)
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Failed to generate Mermaid summary: %s", exc)
        else:
            logger.info("Mermaid execution summary:\n%s", mermaid)
            self._write_mermaid_file(
                workspace_root=workspace_root,
                safe_stem=safe_stem,
                mermaid=mermaid,
            )

        try:
            dashboard_payload = self.visual_reporter.generate_dashboard_payload(
                plan,
                report,
                workspace_root=workspace_root,
                stage=stage,
            )
            dashboard_json_relative = f"{safe_stem}.dashboard.json"
            dashboard_html = self.visual_reporter.generate_dashboard_html(
                initial_payload=dashboard_payload,
                dashboard_json_relative_path=dashboard_json_relative,
            )
            self._write_dashboard_files(
                workspace_root=workspace_root,
                safe_stem=safe_stem,
                dashboard_payload=dashboard_payload,
                dashboard_html=dashboard_html,
            )
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Failed to generate dashboard artifacts: %s", exc)

    def _write_mermaid_file(
        self,
        *,
        workspace_root: Path,
        safe_stem: str,
        mermaid: str,
    ) -> None:
        output_path = (workspace_root / f"{safe_stem}.mermaid").resolve()
        if not is_within_workspace(workspace_root, output_path):
            logger.error(
                "Blocked writing Mermaid output outside workspace: workspace=%s target=%s",
                workspace_root,
                output_path,
            )
            return

        try:
            output_path.write_text(f"{mermaid}\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Unable to persist Mermaid summary to %s: %s", output_path, exc)
            return
        logger.info("Saved Mermaid summary: %s", output_path)

    def _write_dashboard_files(
        self,
        *,
        workspace_root: Path,
        safe_stem: str,
        dashboard_payload: dict[str, object],
        dashboard_html: str,
    ) -> None:
        json_path = (workspace_root / f"{safe_stem}.dashboard.json").resolve()
        html_path = (workspace_root / f"{safe_stem}.dashboard.html").resolve()
        for path in (json_path, html_path):
            if not is_within_workspace(workspace_root, path):
                logger.error(
                    "Blocked writing dashboard output outside workspace: workspace=%s target=%s",
                    workspace_root,
                    path,
                )
                return

        try:
            json_path.write_text(
                json.dumps(dashboard_payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            html_path.write_text(dashboard_html, encoding="utf-8")
        except OSError as exc:
            logger.warning("Unable to persist dashboard artifacts (%s, %s): %s", json_path, html_path, exc)
            return
        logger.info("Saved dashboard artifacts: %s and %s", json_path, html_path)

    def _emit_live_dashboard_snapshot(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        node_records: list[NodeExecutionRecord],
        final_result: CommandResult,
        blocked_reason: str | None,
        telemetry: OrchestrationTelemetry,
    ) -> None:
        safe_stem = self._safe_feature_stem(plan.feature_name)
        snapshot_report = SessionReport(
            command="live-dashboard",
            initial_result=CommandResult(command="live-dashboard", return_code=0),
            final_result=final_result,
            attempts=[],
            node_records=list(node_records),
            telemetry=telemetry,
            success=False,
            blocked_reason=blocked_reason,
        )
        try:
            payload = self.visual_reporter.generate_dashboard_payload(
                plan,
                snapshot_report,
                workspace_root=workspace_root,
                stage="running",
            )
            html = self.visual_reporter.generate_dashboard_html(
                initial_payload=payload,
                dashboard_json_relative_path=f"{safe_stem}.dashboard.json",
            )
            self._write_dashboard_files(
                workspace_root=workspace_root,
                safe_stem=safe_stem,
                dashboard_payload=payload,
                dashboard_html=html,
            )
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.debug("Live dashboard snapshot skipped due to error: %s", exc)

    @staticmethod
    def _safe_feature_stem(feature_name: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", feature_name).strip("_").lower()
        if not sanitized:
            return "feature"
        # Keep artifact filenames bounded even when planners return very long feature names.
        bounded = sanitized[:96].strip("_")
        return bounded or "feature"

    def _run_gatekeeper_review(
        self,
        *,
        plan: ImplementationPlan,
        requirement: str,
        workspace_root: Path,
        validation_commands: tuple[str, ...],
        final_result: CommandResult,
    ) -> tuple[bool, str]:
        if self.reviewer_llm_client is None:
            return True, "Gatekeeper review skipped (no reviewer configured)."

        prompt = self._build_gatekeeper_review_prompt(
            plan=plan,
            requirement=requirement,
            workspace_root=workspace_root,
            validation_commands=validation_commands,
            final_result=final_result,
        )
        try:
            raw_review = self.reviewer_llm_client.generate_fix(prompt)
        except LLMClientError as exc:
            return True, f"Gatekeeper review unavailable due to LLM error: {exc}"
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Unexpected gatekeeper review failure: %s", exc)
            return True, f"Gatekeeper review unavailable due to unexpected error: {exc}"

        normalized = self._normalize_generated_content(raw_review)
        if not normalized.strip():
            return True, "Gatekeeper returned empty review output."

        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            compact = " ".join(normalized.split())
            return True, compact[:600]

        if not isinstance(payload, dict):
            return True, "Gatekeeper response was not a JSON object."

        status_raw = str(payload.get("status", "pass")).strip().lower()
        summary = str(payload.get("summary", "")).strip()
        findings_raw = payload.get("findings")
        findings: list[str] = []
        if isinstance(findings_raw, list):
            findings = [
                str(item).strip()
                for item in findings_raw
                if isinstance(item, str) and item.strip()
            ]

        note = summary or "No summary provided."
        if findings:
            note = f"{note} Findings: {' | '.join(findings[:5])}"

        if status_raw in {"fail", "failed", "block", "blocked", "reject", "rejected"}:
            return False, note
        return True, note

    @staticmethod
    def _build_new_file_prompt(
        plan: ImplementationPlan,
        relative_target: Path,
        *,
        style_rules: str,
    ) -> str:
        steps = "\n".join(f"- {step}" for step in plan.steps) or "- No explicit steps provided."
        return (
            "Role: Lead Developer.\n"
            "Task: Generate production-ready source code for a NEW file.\n"
            "Output constraints: Return ONLY raw file contents. No markdown fences.\n\n"
            f"Feature: {plan.feature_name}\n"
            f"Summary: {plan.summary}\n"
            f"Design Guidance: {plan.design_guidance}\n"
            f"Inferred Project Style: {style_rules}\n"
            f"Target File: {relative_target}\n"
            "Plan Steps:\n"
            f"{steps}\n"
        )

    @staticmethod
    def _build_modify_file_prompt(
        *,
        plan: ImplementationPlan,
        relative_target: Path,
        current_content: str,
        style_rules: str,
    ) -> str:
        steps = "\n".join(f"- {step}" for step in plan.steps) or "- No explicit steps provided."
        return (
            "Role: Lead Developer.\n"
            "Task: Perform a surgical update of an EXISTING file.\n"
            "Output constraints: Return ONLY the FULL updated file contents. No markdown fences.\n"
            "Do not change unrelated behavior.\n\n"
            f"Feature: {plan.feature_name}\n"
            f"Summary: {plan.summary}\n"
            f"Design Guidance: {plan.design_guidance}\n"
            f"Inferred Project Style: {style_rules}\n"
            f"Target File: {relative_target}\n"
            "Plan Steps:\n"
            f"{steps}\n\n"
            "Current File Content:\n"
            "--- BEGIN CURRENT FILE ---\n"
            f"{current_content}\n"
            "--- END CURRENT FILE ---\n"
        )

    @staticmethod
    def _build_gatekeeper_review_prompt(
        *,
        plan: ImplementationPlan,
        requirement: str,
        workspace_root: Path,
        validation_commands: tuple[str, ...],
        final_result: CommandResult,
    ) -> str:
        validations = "\n".join(f"- {command}" for command in validation_commands)
        if not validations:
            validations = "- No validation commands were provided."
        changed_files = "\n".join(
            f"- {path}" for path in [*plan.new_files, *plan.modified_files]
        ) or "- No file changes were listed in the plan."

        combined_output = final_result.combined_output
        if len(combined_output) > 3000:
            combined_output = f"{combined_output[:3000]}\n...[truncated]"

        return (
            "Role: Chief Architect & Senior Reviewer (Gatekeeper).\n"
            "Task: Audit this implementation result for security, performance, reliability, "
            "and idiomatic consistency.\n"
            "Return ONLY one JSON object with this schema:\n"
            "{\n"
            '  "status": "pass|fail",\n'
            '  "summary": "string",\n'
            '  "findings": ["string"]\n'
            "}\n\n"
            f"Requirement: {requirement}\n"
            f"Workspace: {workspace_root}\n"
            f"Feature: {plan.feature_name}\n"
            f"Summary: {plan.summary}\n\n"
            "Planned changed files:\n"
            f"{changed_files}\n\n"
            "Validation commands:\n"
            f"{validations}\n\n"
            "Final validation result:\n"
            f"- command: {final_result.command}\n"
            f"- return_code: {final_result.return_code}\n"
            f"- output:\n{combined_output}\n"
        )
