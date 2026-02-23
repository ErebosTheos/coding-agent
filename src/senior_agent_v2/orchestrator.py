from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from senior_agent_v2.handoff import HandoffManager, HandoffVerificationError
from senior_agent_v2.models import (
    CommandResult,
    Contract,
    DependencyGraph,
    ExecutionNode,
    FileRollback,
    HandoffArtifact,
    NodeExecutionRecord,
    NodeStatus,
    OrchestrationTelemetry,
    SessionReport,
)
from senior_agent_v2.visual_linter import VisualAuditResult, VisualLinter

# Reuse stable utilities from V1 where appropriate.
from senior_agent.dependency_manager import DependencyManager
from senior_agent.engine import Executor, run_shell_command
from senior_agent.llm_client import LLMClient
from senior_agent.models import (
    CommandResult as LegacyCommandResult,
    ImplementationPlan as LegacyImplementationPlan,
    NodeExecutionRecord as LegacyNodeExecutionRecord,
    NodeStatus as LegacyNodeStatus,
    OrchestrationTelemetry as LegacyOrchestrationTelemetry,
    SessionReport as LegacySessionReport,
)
from senior_agent.planner import FeaturePlanner
from senior_agent.style_mimic import StyleMimic
from senior_agent.symbol_graph import SymbolGraph
from senior_agent.test_writer import TestWriter
from senior_agent.visual_reporter import VisualReporter

logger = logging.getLogger(__name__)

_VALIDATION_DAEMON_MODULE = "senior_agent.validation_daemon"
_DEFAULT_VALIDATION_COMMAND_TIMEOUT_SECONDS = 300.0
_MIN_VALIDATION_TIMEOUT_SECONDS = 0.1
_DAEMON_RESPONSE_PADDING_SECONDS = 5.0


@dataclass
class _ValidationDaemonState:
    workspace_root: Path
    process: subprocess.Popen[str]
    lock: threading.Lock
    last_used_at: float


@dataclass(frozen=True)
class _NodeRunResult:
    node_id: str
    trace_id: str
    success: bool
    status: NodeStatus
    level1_passed: bool
    duration_seconds: float
    note: str
    commands_run: tuple[str, ...]


@dataclass(frozen=True)
class _Phase2GridResult:
    success: bool
    records: tuple[NodeExecutionRecord, ...]
    total_node_seconds: float
    blocked_reason: str | None
    audited_node_ids: tuple[str, ...]


@dataclass
class _NodeRuntimeState:
    node_id: str
    trace_id: str
    log_path: Path
    log_buffer: "_AsyncLogBuffer"
    rollback_snapshots: list[FileRollback]
    started_at: float
    status: NodeStatus = NodeStatus.PENDING
    last_status_at: float = 0.0
    last_log_at: float = 0.0
    evicted: bool = False
    eviction_reason: str | None = None
    rollback_applied: bool = False
    active_process: asyncio.subprocess.Process | None = None


class _AsyncLogBuffer:
    """Async line-buffered file writer used by node-level execution logs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def write(self, line: str) -> None:
        await self._queue.put(line.rstrip("\n") + "\n")

    async def close(self) -> None:
        await self._queue.put(None)
        if self._writer_task is not None:
            await self._writer_task
            self._writer_task = None

    async def _writer_loop(self) -> None:
        while True:
            line = await self._queue.get()
            if line is None:
                return
            await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)


class MultiAgentOrchestratorV2:
    """V2 Parallel Grid Orchestrator with contract-first hard gates."""

    def __init__(
        self,
        llm_client: LLMClient,
        planner: FeaturePlanner,
        *,
        reviewer_llm_client: LLMClient | None = None,
        executor: Executor = run_shell_command,
        node_concurrency: int = 8,
        enable_persistent_daemons: bool = True,
        handoff_dir: str = ".senior_agent",
        validation_command_timeout_seconds: float | None = _DEFAULT_VALIDATION_COMMAND_TIMEOUT_SECONDS,
        daemon_cache_ttl_seconds: float = 120.0,
        daemon_startup_timeout_seconds: float = 5.0,
        watchdog_kill_grace_seconds: float = 1.0,
        watchdog_timeout_seconds: float = 60.0,
        watchdog_poll_interval_seconds: float = 5.0,
        enable_visual_linter: bool = True,
        visual_linter: VisualLinter | None = None,
        visual_linter_target_url: str = "http://127.0.0.1:8080",
        visual_linter_fail_open: bool = True,
        enable_visual_auto_heal: bool = True,
        max_visual_auto_heal_attempts: int = 1,
        visual_reporter: VisualReporter | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.reviewer_llm_client = reviewer_llm_client or llm_client
        self.planner = planner
        self.executor = executor
        self.node_concurrency = max(1, node_concurrency)
        self.handoff_dir = handoff_dir
        self.enable_persistent_daemons = enable_persistent_daemons
        self.daemon_cache_ttl_seconds = max(1.0, daemon_cache_ttl_seconds)
        self.daemon_startup_timeout_seconds = max(
            _MIN_VALIDATION_TIMEOUT_SECONDS,
            daemon_startup_timeout_seconds,
        )
        self.watchdog_kill_grace_seconds = max(
            _MIN_VALIDATION_TIMEOUT_SECONDS,
            watchdog_kill_grace_seconds,
        )
        self.watchdog_timeout_seconds = max(1.0, watchdog_timeout_seconds)
        self.watchdog_poll_interval_seconds = max(
            _MIN_VALIDATION_TIMEOUT_SECONDS,
            watchdog_poll_interval_seconds,
        )
        self.enable_visual_linter = enable_visual_linter
        self.visual_linter_target_url = visual_linter_target_url
        self.visual_linter_fail_open = visual_linter_fail_open
        self.enable_visual_auto_heal = enable_visual_auto_heal
        self.max_visual_auto_heal_attempts = max(0, int(max_visual_auto_heal_attempts))
        if validation_command_timeout_seconds is None:
            self.validation_command_timeout_seconds = None
        else:
            self.validation_command_timeout_seconds = max(
                _MIN_VALIDATION_TIMEOUT_SECONDS,
                float(validation_command_timeout_seconds),
            )

        # Tools.
        self.symbol_graph = SymbolGraph()
        self.style_mimic = StyleMimic()
        self.test_writer = TestWriter(llm_client=llm_client)
        self.dependency_manager = DependencyManager(executor=executor)
        self.visual_reporter = visual_reporter or VisualReporter()
        self.visual_linter = visual_linter or VisualLinter(
            reviewer_llm_client=self.reviewer_llm_client,
            handoff_dir=self.handoff_dir,
        )

        self.workspace_root = Path(".").resolve()
        self._validation_daemon_state: _ValidationDaemonState | None = None
        self._node_runtime_states: dict[str, _NodeRuntimeState] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._watchdog_stop_event: asyncio.Event | None = None

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            self._stop_validation_daemon_blocking("shutdown")
        except Exception:
            pass
        task = self._watchdog_task
        if task is not None and not task.done():
            task.cancel()

    async def execute_feature_request(self, requirement: str, workspace: str | Path = ".") -> bool:
        """Core entry point: orchestrates V2 lifecycle."""
        self.workspace_root = Path(workspace).resolve()
        start_time = time.monotonic()

        records: list[NodeExecutionRecord] = []
        total_node_seconds = 0.0
        phase6_failed = False
        success = False
        blocked_reason: str | None = None
        handoff: HandoffArtifact | None = None
        final_result = CommandResult(command="execute_feature_request", return_code=1)

        try:
            logger.info("V2: Starting Phase 1 (Analysis & Contract Freeze)")
            graph = await self._phase1_analysis(requirement)
            if not graph:
                blocked_reason = "Phase 1 planning failed."
                final_result = CommandResult(
                    command="phase1_analysis",
                    return_code=1,
                    stderr=blocked_reason,
                )
                return False

            try:
                handoff = self._phase1_export_handoff(graph)
            except HandoffVerificationError as exc:
                blocked_reason = f"Phase 1 handoff verification failed: {exc}"
                final_result = CommandResult(
                    command="phase1_export_handoff",
                    return_code=1,
                    stderr=blocked_reason,
                )
                logger.error("V2: %s", blocked_reason)
                return False
            logger.info("V2: Handoff verified. Checksum: %s", handoff.checksum[:12])

            logger.info("V2: Starting Phase 2 (Parallel Implementation Grid)")
            phase2_result = await self._phase2_implementation_grid(handoff)
            records = list(phase2_result.records)
            total_node_seconds = phase2_result.total_node_seconds
            if not phase2_result.success:
                blocked_reason = phase2_result.blocked_reason or "Phase 2 failed."
                final_result = CommandResult(
                    command="phase2_grid",
                    return_code=1,
                    stderr=blocked_reason,
                )
                logger.error("V2: %s", blocked_reason)
                return False

            logger.info("V2: Starting Phase 5 (Atomic Merge)")
            phase5_ok, phase5_note = await self._phase5_atomic_merge(
                handoff=handoff,
                audited_node_ids=set(phase2_result.audited_node_ids),
            )
            if not phase5_ok:
                blocked_reason = phase5_note
                final_result = CommandResult(
                    command="phase5_atomic_merge",
                    return_code=1,
                    stderr=blocked_reason,
                )
                logger.error("V2: %s", blocked_reason)
                return False

            logger.info("V2: Starting Phase 6 (Global Validation)")
            phase6_ok = await self._phase6_global_validation(handoff)
            if not phase6_ok:
                phase6_failed = True
                blocked_reason = "Phase 6 global validation failed."
                final_result = CommandResult(
                    command="phase6_global_validation",
                    return_code=1,
                    stderr=blocked_reason,
                )
                logger.error("V2: %s", blocked_reason)
                return False

            logger.info("V2: Starting Phase 6b (Visual UI Validation)")
            phase6b_ok, phase6b_note = await self._phase6b_visual_validation(
                handoff=handoff,
                requirement=requirement,
            )
            if not phase6b_ok:
                blocked_reason = phase6b_note
                final_result = CommandResult(
                    command="phase6b_visual_validation",
                    return_code=1,
                    stderr=blocked_reason,
                )
                logger.error("V2: %s", blocked_reason)
                return False

            success = True
            blocked_reason = None
            final_result = CommandResult(
                command="phase6_global_validation",
                return_code=0,
                stdout="All phases completed successfully.",
            )
            return True
        finally:
            wall_clock_seconds = time.monotonic() - start_time
            telemetry = self._build_telemetry(
                total_node_seconds=total_node_seconds,
                wall_clock_seconds=wall_clock_seconds,
                node_records=records,
                level2_failed=phase6_failed,
            )
            report = SessionReport(
                command=requirement,
                initial_result=CommandResult(command="execute_feature_request", return_code=0),
                final_result=final_result,
                node_records=list(records),
                telemetry=telemetry,
                success=success,
                blocked_reason=blocked_reason,
            )
            self._persist_v2_session_report(report)
            if handoff is not None:
                self._emit_visual_dashboard(handoff=handoff, report=report)
            await self._shutdown_validation_daemon()

            logger.info(
                "V2: Telemetry summary: wall=%.2fs total_node=%.2fs gain=%.2fx",
                telemetry.wall_clock_seconds,
                telemetry.total_node_seconds,
                telemetry.parallel_gain,
            )

    async def _phase1_analysis(self, requirement: str) -> DependencyGraph | None:
        """Gemini architect maps requirement to a frozen DAG."""
        self.symbol_graph.build_graph(self.workspace_root)
        summary = f"Symbol-aware analysis of {self.workspace_root.name}"

        try:
            plan = self.planner.plan_feature(requirement, summary)
            if not plan.dependency_graph:
                logger.error("V2: Planner failed to generate a dependency graph.")
                return None
            return self._coerce_dependency_graph(plan.dependency_graph)
        except Exception as exc:
            logger.error("V2: Phase 1 planning failure: %s", exc)
            return None

    def _phase1_export_handoff(self, graph: DependencyGraph) -> HandoffArtifact:
        handoff_manager = self._handoff_manager()
        artifact = handoff_manager.export(graph)
        handoff_manager.verify(expected_checksum=artifact.checksum)
        return artifact

    async def _phase2_implementation_grid(self, handoff: HandoffArtifact) -> _Phase2GridResult:
        """Phase 2 execution grid with RED/GREEN + audit hard gates."""
        try:
            self._handoff_manager().verify(expected_checksum=handoff.checksum)
        except HandoffVerificationError as exc:
            return _Phase2GridResult(
                success=False,
                records=(),
                total_node_seconds=0.0,
                blocked_reason=f"Phase 2 blocked by handoff verification failure: {exc}",
                audited_node_ids=(),
            )

        await self._start_watchdog()
        pending = {node.node_id for node in handoff.graph.nodes}
        completed: set[str] = set()
        failed: set[str] = set()
        node_map = {node.node_id: node for node in handoff.graph.nodes}
        semaphore = asyncio.Semaphore(self.node_concurrency)

        records: list[NodeExecutionRecord] = []
        audited_node_ids: list[str] = []
        total_node_seconds = 0.0
        blocked_reason: str | None = None

        try:
            while pending:
                try:
                    self._handoff_manager().verify(expected_checksum=handoff.checksum)
                except HandoffVerificationError as exc:
                    blocked_reason = f"Phase 2 wave blocked by handoff verification failure: {exc}"
                    break

                ready = [
                    node_id
                    for node_id in pending
                    if all(dep in completed for dep in node_map[node_id].depends_on)
                ]
                if not ready:
                    blocked_reason = "Circular dependency or execution stall detected in DAG."
                    break

                tasks = [
                    self._execute_node_safe(node_map[node_id], semaphore)
                    for node_id in ready
                ]
                results = await asyncio.gather(*tasks)

                for result in results:
                    pending.remove(result.node_id)
                    total_node_seconds += result.duration_seconds
                    record = NodeExecutionRecord(
                        node_id=result.node_id,
                        trace_id=result.trace_id,
                        status=result.status,
                        level1_passed=result.level1_passed,
                        duration_seconds=result.duration_seconds,
                        note=result.note,
                        commands_run=result.commands_run,
                    )
                    records.append(record)
                    if result.success:
                        completed.add(result.node_id)
                        audited_node_ids.append(result.node_id)
                    else:
                        failed.add(result.node_id)
                        if blocked_reason is None:
                            blocked_reason = result.note or f"Node {result.node_id} failed."
        finally:
            await self._stop_watchdog()
            self._node_runtime_states.clear()

        success = not failed and blocked_reason is None
        return _Phase2GridResult(
            success=success,
            records=tuple(records),
            total_node_seconds=total_node_seconds,
            blocked_reason=blocked_reason,
            audited_node_ids=tuple(audited_node_ids),
        )

    async def _execute_node_safe(
        self,
        node: ExecutionNode,
        semaphore: asyncio.Semaphore,
    ) -> _NodeRunResult:
        """Execute one node with explicit Phase 2a RED then Phase 2b GREEN."""
        async with semaphore:
            start = time.monotonic()
            trace_id = f"{node.node_id}-{uuid4().hex[:8]}"
            commands_run: list[str] = []
            runtime = await self._register_node_runtime(node=node, trace_id=trace_id)
            await self._write_node_log(
                runtime,
                f"Starting node execution: {node.title}",
                status=NodeStatus.RUNNING,
            )

            try:
                red_ok, red_note, red_commands = await self._phase2a_red_test_generation(
                    node,
                    runtime=runtime,
                )
                commands_run.extend(red_commands)
                if runtime.evicted:
                    return await self._build_evicted_result(
                        runtime=runtime,
                        start=start,
                        commands_run=commands_run,
                        level1_passed=False,
                    )
                if not red_ok:
                    await self._apply_node_rollback(
                        runtime,
                        reason=f"Phase 2a RED gate failed: {red_note}",
                    )
                    duration = time.monotonic() - start
                    return _NodeRunResult(
                        node_id=node.node_id,
                        trace_id=trace_id,
                        success=False,
                        status=NodeStatus.FAILED,
                        level1_passed=False,
                        duration_seconds=duration,
                        note=f"Phase 2a RED gate failed: {red_note}",
                        commands_run=tuple(commands_run),
                    )

                green_ok, green_result, green_commands = await self._phase2b_green_implementation(
                    node,
                    runtime=runtime,
                )
                commands_run.extend(green_commands)
                if runtime.evicted:
                    return await self._build_evicted_result(
                        runtime=runtime,
                        start=start,
                        commands_run=commands_run,
                        level1_passed=False,
                    )
                if not green_ok:
                    message = "Phase 2b GREEN validation failed."
                    if green_result is not None:
                        message = (
                            f"{message} command={green_result.command} "
                            f"return_code={green_result.return_code}"
                        )
                    await self._apply_node_rollback(runtime, reason=message)
                    duration = time.monotonic() - start
                    return _NodeRunResult(
                        node_id=node.node_id,
                        trace_id=trace_id,
                        success=False,
                        status=NodeStatus.FAILED,
                        level1_passed=False,
                        duration_seconds=duration,
                        note=message,
                        commands_run=tuple(commands_run),
                    )

                audit_ok, audit_rationale = await self._phase3_node_audit(
                    node,
                    runtime=runtime,
                )
                if runtime.evicted:
                    return await self._build_evicted_result(
                        runtime=runtime,
                        start=start,
                        commands_run=commands_run,
                        level1_passed=True,
                    )
                if not audit_ok:
                    await self._phase4_change_request_loop(node, audit_rationale)
                    await self._apply_node_rollback(
                        runtime,
                        reason=f"Phase 3 audit rejected: {audit_rationale}",
                    )
                    duration = time.monotonic() - start
                    return _NodeRunResult(
                        node_id=node.node_id,
                        trace_id=trace_id,
                        success=False,
                        status=NodeStatus.FAILED,
                        level1_passed=True,
                        duration_seconds=duration,
                        note=f"Phase 3 audit rejected: {audit_rationale}",
                        commands_run=tuple(commands_run),
                    )

                runtime.status = NodeStatus.SUCCESS
                await self._write_node_log(runtime, "Node passed RED/GREEN and audit gates.")
                duration = time.monotonic() - start
                return _NodeRunResult(
                    node_id=node.node_id,
                    trace_id=trace_id,
                    success=True,
                    status=NodeStatus.SUCCESS,
                    level1_passed=True,
                    duration_seconds=duration,
                    note="Node passed RED/GREEN and audit gates.",
                    commands_run=tuple(commands_run),
                )
            except Exception as exc:
                note = f"Node execution crashed: {exc}"
                runtime.status = NodeStatus.FAILED
                await self._apply_node_rollback(runtime, reason=note)
                await self._write_node_log(runtime, note)
                duration = time.monotonic() - start
                return _NodeRunResult(
                    node_id=node.node_id,
                    trace_id=trace_id,
                    success=False,
                    status=NodeStatus.FAILED,
                    level1_passed=False,
                    duration_seconds=duration,
                    note=note,
                    commands_run=tuple(commands_run),
                )
            finally:
                await self._finalize_node_runtime(runtime)

    async def _register_node_runtime(
        self,
        *,
        node: ExecutionNode,
        trace_id: str,
    ) -> _NodeRuntimeState:
        log_path = self._handoff_manager().paths.nodes_dir / node.node_id / "execution.log"
        log_buffer = _AsyncLogBuffer(log_path)
        await log_buffer.start()
        started_at = time.monotonic()
        runtime = _NodeRuntimeState(
            node_id=node.node_id,
            trace_id=trace_id,
            log_path=log_path,
            log_buffer=log_buffer,
            rollback_snapshots=self._capture_node_rollbacks(node),
            started_at=started_at,
            status=NodeStatus.PENDING,
            last_status_at=started_at,
            last_log_at=started_at,
        )
        self._node_runtime_states[node.node_id] = runtime
        await self._write_node_log(runtime, "Execution runtime initialized.")
        return runtime

    def _capture_node_rollbacks(self, node: ExecutionNode) -> list[FileRollback]:
        snapshots: list[FileRollback] = []
        for rel_path in self._collect_node_target_paths(node):
            target = self._resolve_workspace_path(rel_path)
            if target is None:
                continue
            snapshots.append(self._capture_rollback(target))
        return snapshots

    async def _write_node_log(
        self,
        runtime: _NodeRuntimeState,
        message: str,
        *,
        status: NodeStatus | None = None,
    ) -> None:
        if status is not None:
            runtime.status = status
        now = time.monotonic()
        runtime.last_status_at = now
        runtime.last_log_at = now
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = (
            f"{timestamp} [TraceID:{runtime.trace_id}] "
            f"[{runtime.node_id}] [{runtime.status.value}] {message}"
        )
        await runtime.log_buffer.write(line)
        logger.info("[TraceID:%s] [node:%s] %s", runtime.trace_id, runtime.node_id, message)

    async def _apply_node_rollback(self, runtime: _NodeRuntimeState, *, reason: str) -> None:
        if runtime.rollback_applied:
            return
        self._restore_rollbacks(runtime.rollback_snapshots)
        runtime.rollback_applied = True
        await self._write_node_log(
            runtime,
            f"Rollback restored workspace snapshots: {reason}",
        )

    async def _build_evicted_result(
        self,
        *,
        runtime: _NodeRuntimeState,
        start: float,
        commands_run: list[str],
        level1_passed: bool,
    ) -> _NodeRunResult:
        note = runtime.eviction_reason or "Node evicted by watchdog."
        runtime.status = NodeStatus.EVICTED
        await self._apply_node_rollback(runtime, reason=note)
        duration = time.monotonic() - start
        return _NodeRunResult(
            node_id=runtime.node_id,
            trace_id=runtime.trace_id,
            success=False,
            status=NodeStatus.EVICTED,
            level1_passed=level1_passed,
            duration_seconds=duration,
            note=note,
            commands_run=tuple(commands_run),
        )

    async def _finalize_node_runtime(self, runtime: _NodeRuntimeState) -> None:
        if runtime.status not in {NodeStatus.SUCCESS, NodeStatus.FAILED, NodeStatus.EVICTED}:
            runtime.status = NodeStatus.EVICTED if runtime.evicted else NodeStatus.FAILED
        await self._write_node_log(
            runtime,
            f"Runtime finalized. status={runtime.status.value} runtime={time.monotonic() - runtime.started_at:.2f}s",
        )
        await runtime.log_buffer.close()
        self._node_runtime_states.pop(runtime.node_id, None)

    def _touch_node_runtime(self, node_id: str) -> None:
        runtime = self._node_runtime_states.get(node_id)
        if runtime is not None:
            runtime.last_status_at = time.monotonic()

    async def _start_watchdog(self) -> None:
        task = self._watchdog_task
        if task is not None and not task.done():
            return
        self._watchdog_stop_event = asyncio.Event()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info(
            "V2: Watchdog started (timeout=%.1fs, poll=%.1fs).",
            self.watchdog_timeout_seconds,
            self.watchdog_poll_interval_seconds,
        )

    async def _stop_watchdog(self) -> None:
        stop_event = self._watchdog_stop_event
        task = self._watchdog_task
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            try:
                await asyncio.wait_for(
                    task,
                    timeout=max(1.0, self.watchdog_poll_interval_seconds * 2),
                )
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        self._watchdog_task = None
        self._watchdog_stop_event = None

    async def _watchdog_loop(self) -> None:
        stop_event = self._watchdog_stop_event
        if stop_event is None:
            return
        try:
            while not stop_event.is_set():
                await self._run_watchdog_cycle()
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.watchdog_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    async def _run_watchdog_cycle(self) -> None:
        now = time.monotonic()
        for runtime in list(self._node_runtime_states.values()):
            if runtime.status in {NodeStatus.SUCCESS, NodeStatus.FAILED, NodeStatus.EVICTED}:
                continue
            idle_seconds = now - max(runtime.last_status_at, runtime.last_log_at)
            if idle_seconds <= self.watchdog_timeout_seconds:
                continue
            await self._evict_node(
                runtime,
                reason=(
                    f"No status/log heartbeat for {idle_seconds:.1f}s "
                    f"(threshold={self.watchdog_timeout_seconds:.1f}s)."
                ),
            )

    async def _evict_node(self, runtime: _NodeRuntimeState, *, reason: str) -> None:
        if runtime.evicted:
            return
        runtime.evicted = True
        runtime.eviction_reason = reason
        runtime.status = NodeStatus.EVICTED
        runtime.last_status_at = time.monotonic()
        process = runtime.active_process
        if process is not None and process.returncode is None:
            await self._terminate_async_process(process)
        await self._write_node_log(
            runtime,
            f"Watchdog eviction triggered: {reason}",
            status=NodeStatus.EVICTED,
        )

    async def _phase2a_red_test_generation(
        self,
        node: ExecutionNode,
        *,
        runtime: _NodeRuntimeState | None = None,
    ) -> tuple[bool, str, tuple[str, ...]]:
        """
        Phase 2a RED gate:
        - Commands declared as red tests must fail before GREEN implementation.
        - Supported step markers: `red_test: <command>` or `phase2a: <command>`.
        """
        commands = self._extract_red_test_commands(node)
        if not commands:
            return True, "No explicit RED commands configured.", ()

        for command in commands:
            result = await self._execute_validation_command(
                command,
                node_id=node.node_id,
                trace_id=None if runtime is None else runtime.trace_id,
            )
            if result.return_code == 0:
                return (
                    False,
                    f"RED command unexpectedly passed before implementation: {command}",
                    commands,
                )
        return True, "RED gate confirmed failing tests before implementation.", commands

    async def _phase2b_green_implementation(
        self,
        node: ExecutionNode,
        *,
        runtime: _NodeRuntimeState | None = None,
    ) -> tuple[bool, CommandResult | None, tuple[str, ...]]:
        """
        Phase 2b GREEN gate:
        - Generate/stage node files from frozen contract context.
        - Validate generated implementation using node validation commands.
        """
        generation_ok, generation_note, generation_commands = await self._phase2b_generate_node_files(
            node,
            runtime=runtime,
        )
        if not generation_ok:
            return (
                False,
                CommandResult(
                    command=f"phase2b_generate:{node.node_id}",
                    return_code=1,
                    stderr=generation_note,
                ),
                generation_commands,
            )

        commands = tuple(cmd.strip() for cmd in node.validation_commands if cmd.strip())
        if not commands:
            return True, None, generation_commands

        ok, result, executed_commands = await self._run_validation_commands(
            commands=commands,
            stage_label=f"Node {node.node_id} GREEN validation",
            node_id=node.node_id,
            trace_id=None if runtime is None else runtime.trace_id,
        )
        return ok, result, generation_commands + executed_commands

    async def _phase2b_generate_node_files(
        self,
        node: ExecutionNode,
        *,
        runtime: _NodeRuntimeState | None = None,
    ) -> tuple[bool, str, tuple[str, ...]]:
        """
        Materialize node files in workspace and stage artifacts for atomic merge.
        New files are always generated; modified files are generated only if missing.
        """
        operations: list[str] = []
        target_paths = self._collect_node_target_paths(node)
        for relative_path in target_paths:
            if runtime is not None and runtime.evicted:
                return (
                    False,
                    runtime.eviction_reason or "Node evicted by watchdog.",
                    tuple(operations),
                )
            workspace_path = self._resolve_workspace_path(relative_path)
            if workspace_path is None:
                return False, f"Path escapes workspace root: {relative_path}", tuple(operations)

            artifact_path = self._resolve_node_artifact_output_path(
                node_id=node.node_id,
                relative_path=relative_path,
            )
            if artifact_path is None:
                return False, f"Artifact path escapes workspace root: {relative_path}", tuple(operations)

            generate_file = self._should_generate_file(
                node=node,
                relative_path=relative_path,
                workspace_path=workspace_path,
            )
            if generate_file:
                existing_content = ""
                if workspace_path.exists() and workspace_path.is_file():
                    existing_content = workspace_path.read_text(encoding="utf-8", errors="replace")
                prompt = self._build_phase2b_generation_prompt(
                    node=node,
                    relative_path=relative_path,
                    existing_content=existing_content,
                )
                try:
                    raw_content = await asyncio.to_thread(self.llm_client.generate_fix, prompt)
                except Exception as exc:
                    return (
                        False,
                        f"Code generation failed for {relative_path}: {exc}",
                        tuple(operations),
                    )
                if runtime is not None and runtime.evicted:
                    return (
                        False,
                        runtime.eviction_reason or "Node evicted by watchdog.",
                        tuple(operations),
                    )
                generated_content = self._normalize_generated_content(raw_content)
                if not generated_content.strip():
                    return (
                        False,
                        f"Code generation returned empty content for {relative_path}.",
                        tuple(operations),
                    )
                normalized = (
                    generated_content if generated_content.endswith("\n") else f"{generated_content}\n"
                )
                workspace_path.parent.mkdir(parents=True, exist_ok=True)
                workspace_path.write_text(normalized, encoding="utf-8")
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(normalized, encoding="utf-8")
                operations.append(f"generate:{relative_path}")
                if runtime is not None:
                    await self._write_node_log(runtime, f"Generated file: {relative_path}")
                continue

            if workspace_path.exists() and workspace_path.is_file():
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(workspace_path, artifact_path)
                operations.append(f"stage:{relative_path}")
                if runtime is not None:
                    await self._write_node_log(runtime, f"Staged existing file: {relative_path}")
                continue

            return (
                False,
                f"Missing modified file and generation disabled for {relative_path}.",
                tuple(operations),
            )
        return True, "Node files prepared.", tuple(operations)

    async def _phase3_node_audit(
        self,
        node: ExecutionNode,
        *,
        runtime: _NodeRuntimeState | None = None,
    ) -> tuple[bool, str]:
        """Phase 3 hard gate: reviewer audit against frozen contract."""
        if runtime is not None and runtime.evicted:
            return False, runtime.eviction_reason or "Node evicted by watchdog."
        if node.contract is None:
            rationale = "Node has no frozen contract; audit failed."
            self._persist_audit_scorecard(
                node=node,
                passed=False,
                rationale=rationale,
                reviewer_output="",
            )
            logger.error("V2: Node %s audit failed: %s", node.node_id, rationale)
            return False, rationale

        if runtime is not None:
            await self._write_node_log(runtime, "Starting Phase 3 audit.")
        prompt = self._build_node_audit_prompt(node)
        reviewer_output = ""
        passed = False
        rationale = "Audit not executed."
        try:
            reviewer_output = await asyncio.to_thread(
                self.reviewer_llm_client.generate_fix,
                prompt,
            )
            passed, rationale = self._parse_audit_verdict(reviewer_output)
        except Exception as exc:
            passed = False
            rationale = f"Reviewer call failed: {exc}"

        self._persist_audit_scorecard(
            node=node,
            passed=passed,
            rationale=rationale,
            reviewer_output=reviewer_output,
        )
        if runtime is not None:
            audit_message = "Phase 3 audit passed." if passed else f"Phase 3 audit rejected: {rationale}"
            await self._write_node_log(runtime, audit_message)
        if not passed:
            logger.error("V2: Node %s audit rejected: %s", node.node_id, rationale)
        return passed, rationale

    async def _phase4_change_request_loop(self, node: ExecutionNode, rationale: str) -> Path:
        """Skeleton for contract change requests after audit rejection."""
        version_bump = self._infer_version_bump(rationale)
        contract_checksum = (
            ""
            if node.contract is None
            else node.contract.compute_checksum()
        )
        payload: dict[str, Any] = {
            "node_id": node.node_id,
            "status": "required",
            "requested_version_bump": version_bump,
            "rationale": rationale,
            "current_contract_checksum": contract_checksum,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        path = self._handoff_manager().paths.nodes_dir / node.node_id / "change_request.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        logger.info(
            "V2: Change request skeleton generated for node %s at %s",
            node.node_id,
            path,
        )
        return path

    async def _phase5_atomic_merge(
        self,
        *,
        handoff: HandoffArtifact,
        audited_node_ids: set[str],
    ) -> tuple[bool, str]:
        """
        Phase 5 atomic merge:
        - Applies staged artifacts from audited nodes as one transaction.
        - Rolls back all touched files if any merge operation fails.
        """
        operations: list[tuple[Path, Path]] = []
        rollbacks: list[FileRollback] = []

        for node in handoff.graph.nodes:
            if node.node_id not in audited_node_ids:
                continue
            for rel_path in [*node.new_files, *node.modified_files]:
                target = self._resolve_workspace_path(rel_path)
                if target is None:
                    return False, f"Atomic merge blocked: path escapes workspace root: {rel_path}"
                source = self._resolve_node_artifact_path(node.node_id, rel_path)
                if source is None and not target.exists():
                    return (
                        False,
                        f"Atomic merge blocked: no staged artifact or workspace file for {rel_path}",
                    )
                chosen_source = source if source is not None else target
                rollbacks.append(self._capture_rollback(target))
                operations.append((chosen_source, target))

        if not operations:
            return True, "No merge operations required."

        try:
            for source, target in operations:
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.resolve() == target.resolve():
                    continue
                shutil.copyfile(source, target)
            return True, f"Atomic merge applied {len(operations)} file operation(s)."
        except Exception as exc:
            self._restore_rollbacks(rollbacks)
            return False, f"Atomic merge failed and rolled back: {exc}"

    async def _phase6_global_validation(self, handoff: HandoffArtifact) -> bool:
        """Phase 6 hard gate: run workspace-level global validations."""
        commands = tuple(
            command.strip()
            for command in handoff.graph.global_validation_commands
            if command.strip()
        )
        if not commands:
            logger.info("V2: Phase 6 skipped (no global validation commands configured).")
            return True

        ok, result, _ = await self._run_validation_commands(
            commands=commands,
            stage_label="Phase 6 Global Validation",
        )
        if not ok and result is not None:
            logger.error(
                "V2: Global validation failed: command=%s return_code=%s stderr=%s",
                result.command,
                result.return_code,
                result.stderr.strip(),
            )
        return ok

    async def _phase6b_visual_validation(
        self,
        *,
        handoff: HandoffArtifact,
        requirement: str,
    ) -> tuple[bool, str]:
        if not self.enable_visual_linter:
            return True, "Visual linter disabled."
        if self.visual_linter is None:
            return True, "Visual linter unavailable."
        if not self.visual_linter.should_run(self.workspace_root):
            return True, "No UI entrypoint detected; visual audit skipped."

        max_auto_heal_attempts = (
            self.max_visual_auto_heal_attempts if self.enable_visual_auto_heal else 0
        )
        auto_heal_attempt = 0

        while True:
            ui_guidance = self._build_visual_design_guidance(
                handoff=handoff,
                requirement=requirement,
            )
            result = await self.visual_linter.run(
                workspace_root=self.workspace_root,
                ui_design_guidance=ui_guidance,
                target_url=self.visual_linter_target_url,
            )
            if result.status == "error":
                note = f"Phase 6b visual audit error: {result.rationale}"
                if self.visual_linter_fail_open:
                    logger.warning("V2: %s (fail-open enabled)", note)
                    return True, note
                return False, note
            if result.passed:
                if auto_heal_attempt == 0:
                    return True, "Phase 6b visual audit passed."
                return (
                    True,
                    f"Phase 6b visual audit passed after {auto_heal_attempt} auto-heal attempt(s).",
                )

            bug_text = "; ".join(result.visual_bugs).strip()
            detail = bug_text or result.rationale or "Visual defects detected."
            if auto_heal_attempt >= max_auto_heal_attempts:
                if auto_heal_attempt == 0:
                    return False, f"Phase 6b visual audit failed: {detail}"
                return (
                    False,
                    (
                        f"Phase 6b visual audit failed after {auto_heal_attempt} "
                        f"auto-heal attempt(s): {detail}"
                    ),
                )

            next_attempt = auto_heal_attempt + 1
            node = self._build_visual_auto_heal_node(
                handoff=handoff,
                visual_result=result,
                attempt_index=next_attempt,
            )
            self._persist_visual_auto_heal_node(
                node=node,
                visual_result=result,
                attempt_index=next_attempt,
            )
            logger.warning(
                "V2: Visual audit failed. Running auto-heal node %s (attempt %s/%s).",
                node.node_id,
                next_attempt,
                max_auto_heal_attempts,
            )
            wave_ok, wave_note = await self._execute_visual_auto_heal_wave(
                handoff=handoff,
                node=node,
                visual_result=result,
            )
            if not wave_ok:
                return False, wave_note
            auto_heal_attempt = next_attempt

    def _build_visual_design_guidance(
        self,
        *,
        handoff: HandoffArtifact,
        requirement: str,
    ) -> str:
        lines: list[str] = [
            f"Feature: {handoff.graph.feature_name}",
            f"Summary: {handoff.graph.summary}",
            "Original requirement:",
            requirement.strip(),
        ]

        ui_nodes: list[ExecutionNode] = []
        ui_file_suffixes = (".html", ".css", ".scss", ".sass", ".tsx", ".jsx", ".vue")
        for node in handoff.graph.nodes:
            file_paths = [*node.new_files, *node.modified_files]
            has_ui_file = any(path.lower().endswith(ui_file_suffixes) for path in file_paths)
            contract_text = ""
            if node.contract is not None:
                contract_text = " ".join(
                    [
                        node.contract.purpose,
                        " ".join(node.contract.invariants),
                        " ".join(node.contract.public_api),
                    ]
                ).lower()
            has_ui_intent = has_ui_file or ("ui" in node.title.lower()) or ("ui" in contract_text)
            if has_ui_intent:
                ui_nodes.append(node)

        if not ui_nodes:
            ui_nodes = list(handoff.graph.nodes)

        for node in ui_nodes:
            lines.append(f"Node {node.node_id}: {node.title} | {node.summary}")
            if node.contract is not None:
                lines.append(f"Contract Purpose: {node.contract.purpose}")
                if node.contract.invariants:
                    lines.append("Contract Invariants:")
                    lines.extend(f"- {item}" for item in node.contract.invariants)
        return "\n".join(line for line in lines if line.strip())

    def _build_visual_auto_heal_node(
        self,
        *,
        handoff: HandoffArtifact,
        visual_result: VisualAuditResult,
        attempt_index: int,
    ) -> ExecutionNode:
        target_paths = self._discover_visual_fix_targets(visual_result=visual_result)
        node_id = self._next_visual_fix_node_id(attempt_index=attempt_index)
        visual_bugs = [bug.strip() for bug in visual_result.visual_bugs if bug.strip()]
        suggested_css_fixes = visual_result.suggested_css_fixes.strip()

        summary_parts = [
            "Auto-heal visual defects from Phase 6b scorecard.",
            (
                "Visual bugs: " + "; ".join(visual_bugs)
                if visual_bugs
                else "Visual bugs were reported without explicit bug list."
            ),
        ]
        if suggested_css_fixes:
            summary_parts.append(f"Suggested CSS fixes: {suggested_css_fixes}")
        summary = " ".join(summary_parts)

        invariants: list[str] = [
            "Preserve existing backend/API behavior.",
            "Apply only minimal UI/markup/style changes needed to resolve visual defects.",
        ]
        invariants.extend(f"Resolve visual bug: {bug}" for bug in visual_bugs)
        if suggested_css_fixes:
            invariants.append("Use reviewer suggested CSS fixes where compatible.")

        contract = Contract(
            node_id=node_id,
            purpose=(
                "Patch UI presentation defects detected by visual audit "
                "without altering non-UI behavior."
            ),
            inputs=[
                {"name": "visual_bugs", "type": "list[str]"},
                {"name": "suggested_css_fixes", "type": "str"},
                {"name": "target_files", "type": "list[str]"},
            ],
            outputs=[
                {"name": "updated_ui_files", "type": "list[str]"},
                {"name": "visual_audit_status", "type": "pass|fail"},
            ],
            public_api=[
                "No public backend API changes.",
                "UI-only adjustments in targeted presentation files.",
            ],
            invariants=invariants,
            error_taxonomy={
                "VisualRegression": "Rendered UI still violates required design constraints.",
                "FunctionalRegression": "Non-visual behavior changed while applying visual patch.",
            },
            examples=[
                {
                    "visual_bugs": visual_bugs[:3],
                    "suggested_css_fixes": suggested_css_fixes,
                    "target_files": list(target_paths),
                }
            ],
        )
        steps = [
            "Apply a minimal visual patch for the reported UI defects.",
            (
                "Visual bug context: " + "; ".join(visual_bugs)
                if visual_bugs
                else "Visual bug context: no explicit bug list provided."
            ),
        ]
        if suggested_css_fixes:
            steps.append(f"Reviewer suggested CSS fixes: {suggested_css_fixes}")

        return ExecutionNode(
            node_id=node_id,
            title=f"Visual Fix Wave {attempt_index}",
            summary=summary,
            new_files=list(target_paths),
            modified_files=[],
            steps=steps,
            validation_commands=[],
            depends_on=[],
            contract=contract,
            contract_node=True,
            shared_resources=list(target_paths),
        )

    def _discover_visual_fix_targets(
        self,
        *,
        visual_result: VisualAuditResult,
    ) -> tuple[str, ...]:
        root = self.workspace_root.resolve()
        candidates: list[Path] = []
        preferred = [
            root / "index.html",
            root / "src" / "index.html",
            root / "public" / "index.html",
        ]
        for candidate in preferred:
            if candidate.exists() and candidate.is_file():
                candidates.append(candidate)

        if visual_result.entrypoint:
            explicit = (root / visual_result.entrypoint).resolve()
            if explicit.exists() and explicit.is_file():
                candidates.append(explicit)

        for suffix in ("*.css", "*.scss", "*.sass", "*.html"):
            for candidate in root.rglob(suffix):
                if self._is_visual_fix_ignored_path(candidate):
                    continue
                if candidate.is_file():
                    candidates.append(candidate)

        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                relative = candidate.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            if relative in seen:
                continue
            seen.add(relative)
            deduped.append(relative)
            if len(deduped) >= 4:
                break
        if deduped:
            return tuple(deduped)
        return ("index.html",)

    def _is_visual_fix_ignored_path(self, path: Path) -> bool:
        ignored_parts = {".git", ".senior_agent", ".venv", "venv", "node_modules"}
        return any(part in ignored_parts for part in path.parts)

    def _next_visual_fix_node_id(self, *, attempt_index: int) -> str:
        base = f"visual_fix_{attempt_index:02d}"
        nodes_dir = self._handoff_manager().paths.nodes_dir
        candidate = base
        suffix = 1
        while (nodes_dir / candidate).exists():
            suffix += 1
            candidate = f"{base}_{suffix}"
        return candidate

    def _persist_visual_auto_heal_node(
        self,
        *,
        node: ExecutionNode,
        visual_result: VisualAuditResult,
        attempt_index: int,
    ) -> None:
        handoff_root = self._handoff_manager().paths.root_dir
        payload: dict[str, Any] = {
            "attempt": attempt_index,
            "node": node.to_dict(),
            "visual_bugs": list(visual_result.visual_bugs),
            "suggested_css_fixes": visual_result.suggested_css_fixes,
            "visual_rationale": visual_result.rationale,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        node_payload_path = handoff_root / "nodes" / node.node_id / "auto_heal_node.json"
        node_payload_path.parent.mkdir(parents=True, exist_ok=True)
        node_payload_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        aggregate_path = handoff_root / "visual_auto_heal_nodes.json"
        records: list[dict[str, Any]] = []
        if aggregate_path.exists():
            try:
                raw = json.loads(aggregate_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    records = [item for item in raw if isinstance(item, dict)]
            except json.JSONDecodeError:
                records = []
        records.append(payload)
        aggregate_path.write_text(
            json.dumps(records, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    async def _execute_visual_auto_heal_wave(
        self,
        *,
        handoff: HandoffArtifact,
        node: ExecutionNode,
        visual_result: VisualAuditResult,
    ) -> tuple[bool, str]:
        semaphore = asyncio.Semaphore(1)
        node_result = await self._execute_node_safe(node, semaphore)
        if not node_result.success:
            return (
                False,
                (
                    f"Visual auto-heal node {node.node_id} failed: "
                    f"{node_result.note or node_result.status.value}"
                ),
            )

        followup_graph = DependencyGraph(
            feature_name=f"{handoff.graph.feature_name} - Visual Auto Heal",
            summary=(
                "Follow-up visual-fix wave generated from failed visual audit. "
                f"Rationale: {visual_result.rationale}"
            ),
            nodes=[node],
            global_validation_commands=list(handoff.graph.global_validation_commands),
        )
        followup_handoff = HandoffArtifact(
            graph=followup_graph,
            checksum=followup_graph.compute_handoff_checksum(),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        merge_ok, merge_note = await self._phase5_atomic_merge(
            handoff=followup_handoff,
            audited_node_ids={node.node_id},
        )
        if not merge_ok:
            return False, f"Visual auto-heal merge failed: {merge_note}"

        phase6_ok = await self._phase6_global_validation(followup_handoff)
        if not phase6_ok:
            return False, "Visual auto-heal global validation failed."
        return True, f"Visual auto-heal node {node.node_id} applied successfully."

    async def _run_validation_commands(
        self,
        *,
        commands: tuple[str, ...],
        stage_label: str,
        node_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[bool, CommandResult | None, tuple[str, ...]]:
        last_result: CommandResult | None = None
        executed: list[str] = []
        for command in commands:
            executed.append(command)
            result = await self._execute_validation_command(
                command,
                node_id=node_id,
                trace_id=trace_id,
            )
            last_result = result
            if result.return_code != 0:
                logger.error(
                    "V2: %s command failed: %s (code=%s)",
                    stage_label,
                    command,
                    result.return_code,
                )
                return False, result, tuple(executed)
        return True, last_result, tuple(executed)

    async def _execute_validation_command(
        self,
        command: str,
        *,
        node_id: str | None = None,
        trace_id: str | None = None,
    ) -> CommandResult:
        if node_id is not None:
            return await self._execute_validation_command_direct(
                command,
                node_id=node_id,
                trace_id=trace_id,
            )
        daemon_result = await self._execute_validation_command_via_daemon(command)
        if daemon_result is not None:
            return daemon_result
        return await self._execute_validation_command_direct(
            command,
            node_id=node_id,
            trace_id=trace_id,
        )

    async def _execute_validation_command_direct(
        self,
        command: str,
        *,
        node_id: str | None = None,
        trace_id: str | None = None,
    ) -> CommandResult:
        runtime = self._node_runtime_states.get(node_id or "")
        if runtime is not None:
            if trace_id is None:
                trace_id = runtime.trace_id
            await self._write_node_log(runtime, f"Running command: {command}")
        self._touch_node_runtime(node_id or "")
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        if runtime is not None:
            runtime.active_process = process
        timeout_seconds = self.validation_command_timeout_seconds
        try:
            if timeout_seconds is None:
                stdout, stderr = await process.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
        except asyncio.TimeoutError:
            await self._terminate_async_process(process)
            if runtime is not None:
                await self._write_node_log(
                    runtime,
                    f"Command timed out after {timeout_seconds:.1f}s: {command}",
                )
            return CommandResult(
                command=command,
                return_code=124,
                stdout="",
                stderr=(
                    f"Validation command timed out after {timeout_seconds:.1f}s "
                    "(watchdog reaper hard-killed process tree)."
                ),
            )
        finally:
            if runtime is not None:
                runtime.active_process = None
                self._touch_node_runtime(runtime.node_id)

        result = CommandResult(
            command=command,
            return_code=int(process.returncode or 0),
            stdout=stdout.decode() if isinstance(stdout, bytes) else stdout,
            stderr=stderr.decode() if isinstance(stderr, bytes) else stderr,
        )
        if runtime is not None:
            await self._write_node_log(
                runtime,
                f"Command completed (code={result.return_code}): {command}",
            )
        elif trace_id:
            logger.info("[TraceID:%s] command completed (code=%s): %s", trace_id, result.return_code, command)
        return result

    async def _execute_validation_command_via_daemon(self, command: str) -> CommandResult | None:
        state = await self._get_or_start_validation_daemon()
        if state is None:
            return None

        effective_timeout = self.validation_command_timeout_seconds
        response_timeout = (
            (effective_timeout or _DEFAULT_VALIDATION_COMMAND_TIMEOUT_SECONDS)
            + self.watchdog_kill_grace_seconds
            + _DAEMON_RESPONSE_PADDING_SECONDS
        )
        response = await asyncio.to_thread(
            self._send_validation_daemon_request_blocking,
            state,
            {
                "action": "run",
                "command": command,
                "cwd": str(self.workspace_root),
                "timeout_seconds": effective_timeout,
            },
            response_timeout,
        )
        if response is None:
            self._stop_validation_daemon_blocking("unresponsive")
            return CommandResult(
                command=command,
                return_code=124,
                stdout="",
                stderr=(
                    f"Validation daemon became unresponsive after {response_timeout:.1f}s "
                    "(watchdog reaper hard-kill)."
                ),
            )
        return CommandResult(
            command=command,
            return_code=int(response.get("return_code", 1)),
            stdout=str(response.get("stdout", "")),
            stderr=str(response.get("stderr", "")),
        )

    async def _get_or_start_validation_daemon(self) -> _ValidationDaemonState | None:
        if not self._supports_validation_daemon():
            return None

        state = self._validation_daemon_state
        now = time.monotonic()
        if state is not None:
            idle_seconds = now - state.last_used_at
            if (
                state.workspace_root != self.workspace_root
                or state.process.poll() is not None
                or idle_seconds > self.daemon_cache_ttl_seconds
            ):
                self._stop_validation_daemon_blocking("stale-or-dead")
                state = None

        if state is None:
            state = await asyncio.to_thread(self._start_validation_daemon_blocking)
            self._validation_daemon_state = state
        return state

    async def _shutdown_validation_daemon(self) -> None:
        await asyncio.to_thread(self._stop_validation_daemon_blocking, "shutdown")

    def _start_validation_daemon_blocking(self) -> _ValidationDaemonState | None:
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
                cwd=str(self.workspace_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=os.name != "nt",
                env=env,
            )
        except OSError as exc:
            logger.warning("V2: Unable to start validation daemon: %s", exc)
            return None

        if process.stdin is None or process.stdout is None:
            self._terminate_process_tree(process)
            return None

        state = _ValidationDaemonState(
            workspace_root=self.workspace_root,
            process=process,
            lock=threading.Lock(),
            last_used_at=time.monotonic(),
        )
        ping_response = self._send_validation_daemon_request_blocking(
            state,
            {"action": "ping"},
            self.daemon_startup_timeout_seconds,
        )
        if not isinstance(ping_response, dict) or ping_response.get("status") != "ok":
            self._terminate_process_tree(process)
            return None
        return state

    def _send_validation_daemon_request_blocking(
        self,
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
        deadline = time.monotonic() + max(_MIN_VALIDATION_TIMEOUT_SECONDS, timeout_seconds)

        with state.lock:
            if process.poll() is not None:
                return None
            try:
                process.stdin.write(json.dumps(envelope, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except OSError:
                return None

            while time.monotonic() < deadline:
                remaining = max(_MIN_VALIDATION_TIMEOUT_SECONDS, deadline - time.monotonic())
                raw_line = self._read_line_with_timeout(process.stdout, remaining)
                if raw_line is None:
                    return None
                try:
                    response = json.loads(raw_line.strip())
                except json.JSONDecodeError:
                    continue
                if not isinstance(response, dict):
                    continue
                if str(response.get("request_id", "")).strip() != request_id:
                    continue
                state.last_used_at = time.monotonic()
                return response
        return None

    def _read_line_with_timeout(
        self,
        stream: Any,
        timeout_seconds: float,
    ) -> str | None:
        container: dict[str, str | None] = {"line": None}

        def _reader() -> None:
            container["line"] = stream.readline()

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        thread.join(timeout_seconds)
        if thread.is_alive():
            return None
        line = container.get("line")
        if line is None:
            return None
        if line == "":
            return None
        return str(line)

    def _stop_validation_daemon_blocking(self, reason: str) -> None:
        state = self._validation_daemon_state
        if state is None:
            return
        try:
            if state.process.poll() is None:
                self._send_validation_daemon_request_blocking(
                    state,
                    {"action": "shutdown"},
                    min(1.0, self.watchdog_kill_grace_seconds),
                )
        except Exception:
            pass
        self._terminate_process_tree(state.process)
        self._validation_daemon_state = None
        logger.info(
            "V2: Stopped validation daemon for workspace=%s reason=%s",
            self.workspace_root,
            reason,
        )

    def _supports_validation_daemon(self) -> bool:
        return self.enable_persistent_daemons and self.executor is run_shell_command

    async def _terminate_async_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt" and process.pid is not None:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self.watchdog_kill_grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if os.name != "nt" and process.pid is not None:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self.watchdog_kill_grace_seconds)
        except asyncio.TimeoutError:
            pass

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
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
            process.wait(timeout=self.watchdog_kill_grace_seconds)
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
            process.wait(timeout=self.watchdog_kill_grace_seconds)
        except subprocess.TimeoutExpired:
            pass

    def _build_node_audit_prompt(self, node: ExecutionNode) -> str:
        contract_payload = node.contract.to_dict() if node.contract is not None else {}
        file_snapshots = self._collect_node_file_snapshots(node)
        return (
            "You are the Senior Reviewer for a contract-first implementation node.\n"
            "Evaluate if implementation matches the frozen contract exactly.\n"
            "Respond with JSON only using this schema:\n"
            "{\"pass\": <true|false>, \"rationale\": \"<concise reason>\"}\n\n"
            f"Node ID: {node.node_id}\n"
            f"Node Title: {node.title}\n"
            f"Node Summary: {node.summary}\n\n"
            f"Frozen Contract JSON:\n{json.dumps(contract_payload, indent=2, sort_keys=True)}\n\n"
            "Code under review:\n"
            f"{file_snapshots}\n"
        )

    def _collect_node_file_snapshots(self, node: ExecutionNode) -> str:
        sections: list[str] = []
        for raw_path in [*node.new_files, *node.modified_files]:
            target = self._resolve_workspace_path(raw_path)
            if target is None:
                sections.append(f"--- {raw_path} ---\nERROR: path escapes workspace root.")
                continue
            if not target.exists():
                sections.append(f"--- {raw_path} ---\nMISSING FILE")
                continue
            if not target.is_file():
                sections.append(f"--- {raw_path} ---\nNOT A FILE")
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = target.read_text(encoding="utf-8", errors="replace")
            if len(content) > 8_000:
                content = content[:8_000] + "\n...[truncated]..."
            sections.append(f"--- {raw_path} ---\n{content}")
        if not sections:
            return "No code files were associated with this node."
        return "\n\n".join(sections)

    def _parse_audit_verdict(self, reviewer_output: str) -> tuple[bool, str]:
        cleaned = reviewer_output.strip()
        if cleaned.startswith("```"):
            fence_start = cleaned.find("{")
            fence_end = cleaned.rfind("}")
            if fence_start != -1 and fence_end != -1 and fence_end > fence_start:
                cleaned = cleaned[fence_start : fence_end + 1]

        payload: dict[str, Any] | None = None
        try:
            candidate = json.loads(cleaned)
            if isinstance(candidate, dict):
                payload = candidate
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    candidate = json.loads(cleaned[start : end + 1])
                    if isinstance(candidate, dict):
                        payload = candidate
                except json.JSONDecodeError:
                    payload = None

        if payload is None:
            return False, "Reviewer returned invalid JSON verdict."

        passed = bool(payload.get("pass", False))
        rationale = str(payload.get("rationale", "")).strip()
        if not rationale:
            rationale = "Reviewer did not provide rationale."
        return passed, rationale

    def _persist_audit_scorecard(
        self,
        *,
        node: ExecutionNode,
        passed: bool,
        rationale: str,
        reviewer_output: str,
    ) -> None:
        scorecard_path = self._handoff_manager().paths.nodes_dir / node.node_id / "audit_scorecard.json"
        scorecard_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "node_id": node.node_id,
            "pass": bool(passed),
            "rationale": str(rationale),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if reviewer_output.strip():
            payload["reviewer_output"] = reviewer_output.strip()
        scorecard_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _extract_red_test_commands(self, node: ExecutionNode) -> tuple[str, ...]:
        commands: list[str] = []
        for raw_step in node.steps:
            step = raw_step.strip()
            lower = step.lower()
            if lower.startswith("red_test:") or lower.startswith("phase2a:"):
                _, _, command = step.partition(":")
                cleaned = command.strip()
                if cleaned:
                    commands.append(cleaned)
        return tuple(commands)

    def _collect_node_target_paths(self, node: ExecutionNode) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for raw_path in [*node.new_files, *node.modified_files]:
            cleaned = raw_path.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            ordered.append(cleaned)
        return tuple(ordered)

    def _should_generate_file(
        self,
        *,
        node: ExecutionNode,
        relative_path: str,
        workspace_path: Path,
    ) -> bool:
        if relative_path in node.new_files:
            return True
        return not workspace_path.exists()

    def _build_phase2b_generation_prompt(
        self,
        *,
        node: ExecutionNode,
        relative_path: str,
        existing_content: str,
    ) -> str:
        contract_payload = {} if node.contract is None else node.contract.to_dict()
        existing_block = existing_content if existing_content.strip() else "<empty>"
        if len(existing_block) > 6000:
            existing_block = f"{existing_block[:6000]}\n...[truncated]..."
        return (
            "You are the Lead Developer for a contract-first execution node.\n"
            "Generate the full file content for the target file.\n"
            "Return code only, no markdown fences and no commentary.\n"
            "Respect the frozen contract and implement only what is needed.\n\n"
            f"Node ID: {node.node_id}\n"
            f"Node Title: {node.title}\n"
            f"Node Summary: {node.summary}\n"
            f"Target File: {relative_path}\n\n"
            f"Frozen Contract JSON:\n{json.dumps(contract_payload, indent=2, sort_keys=True)}\n\n"
            f"Existing File Content:\n{existing_block}\n"
        )

    def _normalize_generated_content(self, raw: str) -> str:
        text = raw.strip()
        if not text:
            return ""
        fence_matches = list(
            re.finditer(r"```(?:[a-zA-Z0-9_+-]+)?\n([\s\S]*?)```", text, flags=re.MULTILINE)
        )
        if fence_matches:
            candidate = fence_matches[-1].group(1).strip()
            if candidate:
                return candidate
        return text

    def _resolve_node_artifact_output_path(self, node_id: str, relative_path: str) -> Path | None:
        artifact_path = (
            self._handoff_manager().paths.nodes_dir
            / node_id
            / "artifacts"
            / relative_path
        ).resolve()
        try:
            artifact_path.relative_to(self.workspace_root)
            return artifact_path
        except ValueError:
            return None

    def _resolve_node_artifact_path(self, node_id: str, relative_path: str) -> Path | None:
        artifact_path = (
            self._handoff_manager().paths.nodes_dir
            / node_id
            / "artifacts"
            / relative_path
        ).resolve()
        if not artifact_path.exists() or not artifact_path.is_file():
            return None
        try:
            artifact_path.relative_to(self.workspace_root)
            return artifact_path
        except ValueError:
            return None

    def _capture_rollback(self, path: Path) -> FileRollback:
        existed_before = path.exists()
        if existed_before and path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="replace")
        else:
            content = None
        return FileRollback(path=path, existed_before=existed_before, content=content)

    def _restore_rollbacks(self, rollbacks: list[FileRollback]) -> None:
        for rollback in reversed(rollbacks):
            if rollback.existed_before:
                rollback.path.parent.mkdir(parents=True, exist_ok=True)
                rollback.path.write_text(rollback.content or "", encoding="utf-8")
            else:
                if rollback.path.exists():
                    rollback.path.unlink()

    def _infer_version_bump(self, rationale: str) -> str:
        text = rationale.lower()
        if "major" in text:
            return "MAJOR"
        if "minor" in text:
            return "MINOR"
        return "PATCH"

    def _build_telemetry(
        self,
        *,
        total_node_seconds: float,
        wall_clock_seconds: float,
        node_records: list[NodeExecutionRecord],
        level2_failed: bool,
    ) -> OrchestrationTelemetry:
        gain = 1.0
        if wall_clock_seconds > 0:
            gain = max(1.0, total_node_seconds / wall_clock_seconds)
        denominator = wall_clock_seconds * max(1, self.node_concurrency)
        grid_efficiency = 0.0
        if denominator > 0:
            grid_efficiency = total_node_seconds / denominator
        level1_pass_nodes = sum(1 for record in node_records if record.level1_passed)
        level1_failed_nodes = len(node_records) - level1_pass_nodes
        return OrchestrationTelemetry(
            total_node_seconds=total_node_seconds,
            wall_clock_seconds=wall_clock_seconds,
            parallel_gain=gain,
            grid_efficiency=grid_efficiency,
            initial_concurrency=self.node_concurrency,
            final_concurrency=self.node_concurrency,
            adaptive_throttle_events=0,
            level1_pass_nodes=level1_pass_nodes,
            level1_failed_nodes=level1_failed_nodes,
            level2_failures=1 if level2_failed else 0,
        )

    def _persist_v2_session_report(self, report: SessionReport) -> None:
        report_path = self.workspace_root / ".senior_agent" / "v2_session_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _emit_visual_dashboard(self, *, handoff: HandoffArtifact, report: SessionReport) -> None:
        try:
            plan_payload = {
                "feature_name": handoff.graph.feature_name,
                "summary": handoff.graph.summary,
                "dependency_graph": handoff.graph.to_dict(),
                "new_files": [path for node in handoff.graph.nodes for path in node.new_files],
                "modified_files": [path for node in handoff.graph.nodes for path in node.modified_files],
                "steps": [step for node in handoff.graph.nodes for step in node.steps],
                "validation_commands": list(handoff.graph.global_validation_commands),
                "design_guidance": "V2 execution report",
            }
            legacy_plan = LegacyImplementationPlan.from_dict(plan_payload)

            legacy_node_records = [
                LegacyNodeExecutionRecord(
                    node_id=record.node_id,
                    trace_id=record.trace_id,
                    status=LegacyNodeStatus(record.status.value),
                    level1_passed=record.level1_passed,
                    duration_seconds=record.duration_seconds,
                    note=record.note,
                    commands_run=record.commands_run,
                )
                for record in report.node_records
            ]
            assert report.telemetry is not None
            telemetry = report.telemetry
            legacy_telemetry = LegacyOrchestrationTelemetry(
                total_node_seconds=telemetry.total_node_seconds,
                wall_clock_seconds=telemetry.wall_clock_seconds,
                parallel_gain=telemetry.parallel_gain,
                initial_concurrency=telemetry.initial_concurrency,
                final_concurrency=telemetry.final_concurrency,
                adaptive_throttle_events=telemetry.adaptive_throttle_events,
                level1_pass_nodes=telemetry.level1_pass_nodes,
                level1_failed_nodes=telemetry.level1_failed_nodes,
                level2_failures=telemetry.level2_failures,
            )
            legacy_report = LegacySessionReport(
                command=report.command,
                initial_result=LegacyCommandResult(
                    command=report.initial_result.command,
                    return_code=report.initial_result.return_code,
                    stdout=report.initial_result.stdout,
                    stderr=report.initial_result.stderr,
                ),
                final_result=LegacyCommandResult(
                    command=report.final_result.command,
                    return_code=report.final_result.return_code,
                    stdout=report.final_result.stdout,
                    stderr=report.final_result.stderr,
                ),
                node_records=legacy_node_records,
                telemetry=legacy_telemetry,
                success=report.success,
                blocked_reason=report.blocked_reason,
            )

            slug = self._slugify(handoff.graph.feature_name)
            mermaid_path = self.workspace_root / f"{slug}.mermaid"
            dashboard_json_path = self.workspace_root / f"{slug}.dashboard.json"
            dashboard_html_path = self.workspace_root / f"{slug}.dashboard.html"

            mermaid = self.visual_reporter.generate_mermaid_summary(legacy_plan, legacy_report)
            mermaid_path.write_text(mermaid + "\n", encoding="utf-8")

            dashboard_payload = self.visual_reporter.generate_dashboard_payload(
                legacy_plan,
                legacy_report,
                workspace_root=self.workspace_root,
                stage="final",
            )
            dashboard_json_path.write_text(
                json.dumps(dashboard_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            relative_json = dashboard_json_path.relative_to(self.workspace_root).as_posix()
            dashboard_html = self.visual_reporter.generate_dashboard_html(
                initial_payload=dashboard_payload,
                dashboard_json_relative_path=relative_json,
            )
            dashboard_html_path.write_text(dashboard_html, encoding="utf-8")
        except Exception as exc:  # pragma: no cover - report generation must not break flow
            logger.warning("V2: Unable to emit visual dashboard artifacts: %s", exc)

    def _slugify(self, value: str) -> str:
        cleaned = value.strip().lower()
        cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
        cleaned = cleaned.strip("_")
        return cleaned or "v2_execution"

    def _handoff_manager(self) -> HandoffManager:
        return HandoffManager(
            workspace_root=self.workspace_root,
            handoff_dir=self.handoff_dir,
        )

    def _resolve_workspace_path(self, relative_path: str) -> Path | None:
        candidate = (self.workspace_root / relative_path).resolve()
        try:
            candidate.relative_to(self.workspace_root)
            return candidate
        except ValueError:
            return None

    @staticmethod
    def _coerce_dependency_graph(raw_graph: object) -> DependencyGraph:
        if isinstance(raw_graph, DependencyGraph):
            return raw_graph
        if hasattr(raw_graph, "to_dict"):
            payload = raw_graph.to_dict()
            if isinstance(payload, dict):
                return DependencyGraph.from_dict(payload)
        raise ValueError("Planner dependency graph is not compatible with V2 model.")
