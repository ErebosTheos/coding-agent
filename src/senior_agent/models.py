from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class FailureType(str, Enum):
    BUILD_ERROR = "build_error"
    TEST_FAILURE = "test_failure"
    RUNTIME_EXCEPTION = "runtime_exception"
    PERF_REGRESSION = "perf_regression"
    LINT_TYPE_FAILURE = "lint_or_type_failure"
    UNKNOWN = "unknown"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    EVICTED = "evicted"


@dataclass(frozen=True)
class CommandResult:
    command: str
    return_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def combined_output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


@dataclass(frozen=True)
class FailureContext:
    command_result: CommandResult
    failure_type: FailureType
    workspace: Path
    attempt_number: int


@dataclass(frozen=True)
class FixOutcome:
    """Result returned by a fix strategy for one healing attempt.

    Contract:
    - If ``applied`` is ``True`` and ``changed_files`` is non-empty, strategy must
      include rollback snapshots for those files in ``rollback_entries``.
    """

    applied: bool
    note: str = ""
    changed_files: tuple[Path, ...] = ()
    diff_summary: tuple[str, ...] = ()
    rollback_entries: tuple["FileRollback", ...] = ()


class FixStrategy(Protocol):
    """Contract for strategies that attempt a single automated fix."""

    name: str

    def apply(self, context: FailureContext) -> FixOutcome:
        """Apply one fix attempt and return a contract-compliant ``FixOutcome``."""
        ...


@dataclass(frozen=True)
class ImplementationPlan:
    """Structured, serializable plan for implementing a requested feature."""

    feature_name: str
    summary: str
    new_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    design_guidance: str = ""
    dependency_graph: "DependencyGraph | None" = None

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(
            {
                "feature_name": self.feature_name,
                "summary": self.summary,
                "new_files": list(self.new_files),
                "modified_files": list(self.modified_files),
                "steps": list(self.steps),
                "validation_commands": list(self.validation_commands),
                "design_guidance": self.design_guidance,
                "dependency_graph": (
                    None if self.dependency_graph is None else self.dependency_graph.to_dict()
                ),
            },
            indent=indent,
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ImplementationPlan":
        if not isinstance(payload, dict):
            raise ValueError("Implementation plan payload must be an object.")

        feature_name = str(payload.get("feature_name", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        if not feature_name:
            raise ValueError("Implementation plan is missing required field: feature_name.")
        if not summary:
            raise ValueError("Implementation plan is missing required field: summary.")

        graph_payload = payload.get("dependency_graph")
        if graph_payload is None and isinstance(payload.get("nodes"), list):
            graph_payload = {
                "feature_name": feature_name,
                "summary": summary,
                "nodes": payload.get("nodes"),
                "global_validation_commands": payload.get("validation_commands", []),
            }
        dependency_graph = (
            DependencyGraph.from_dict(graph_payload)
            if isinstance(graph_payload, dict)
            else None
        )

        new_files = cls._coerce_string_list(payload.get("new_files"))
        modified_files = cls._coerce_string_list(payload.get("modified_files"))
        steps = cls._coerce_string_list(payload.get("steps"))
        validation_commands = cls._coerce_string_list(payload.get("validation_commands"))

        if dependency_graph is not None:
            if not new_files:
                new_files = dependency_graph.all_new_files()
            if not modified_files:
                modified_files = dependency_graph.all_modified_files()
            if not steps:
                steps = dependency_graph.all_steps()
            if not validation_commands:
                validation_commands = dependency_graph.global_validation_commands

        return cls(
            feature_name=feature_name,
            summary=summary,
            new_files=new_files,
            modified_files=modified_files,
            steps=steps,
            validation_commands=validation_commands,
            design_guidance=str(payload.get("design_guidance", "")).strip(),
            dependency_graph=dependency_graph,
        )

    @staticmethod
    def _coerce_string_list(raw_value: Any) -> list[str]:
        if not isinstance(raw_value, list):
            return []
        return [
            item.strip()
            for item in raw_value
            if isinstance(item, str) and item.strip()
        ]


@dataclass(frozen=True)
class ExecutionNode:
    """Atomic graph node used by the parallel grid orchestrator."""

    node_id: str
    title: str
    summary: str
    new_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    contract_node: bool = False
    shared_resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "summary": self.summary,
            "new_files": list(self.new_files),
            "modified_files": list(self.modified_files),
            "steps": list(self.steps),
            "validation_commands": list(self.validation_commands),
            "depends_on": list(self.depends_on),
            "contract_node": self.contract_node,
            "shared_resources": list(self.shared_resources),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, fallback_id: int) -> "ExecutionNode":
        if not isinstance(payload, dict):
            raise ValueError("Execution node payload must be an object.")
        node_id = str(payload.get("node_id", "")).strip() or f"node_{fallback_id}"
        title = str(payload.get("title", "")).strip() or node_id
        summary = str(payload.get("summary", "")).strip() or title
        if not node_id:
            raise ValueError("Execution node is missing node_id.")

        return cls(
            node_id=node_id,
            title=title,
            summary=summary,
            new_files=ImplementationPlan._coerce_string_list(payload.get("new_files")),
            modified_files=ImplementationPlan._coerce_string_list(payload.get("modified_files")),
            steps=ImplementationPlan._coerce_string_list(payload.get("steps")),
            validation_commands=ImplementationPlan._coerce_string_list(
                payload.get("validation_commands")
            ),
            depends_on=ImplementationPlan._coerce_string_list(payload.get("depends_on")),
            contract_node=bool(payload.get("contract_node", False)),
            shared_resources=ImplementationPlan._coerce_string_list(
                payload.get("shared_resources")
            ),
        )


@dataclass(frozen=True)
class DependencyGraph:
    """Dependency graph describing node-level parallel execution plan."""

    feature_name: str
    summary: str
    nodes: list[ExecutionNode] = field(default_factory=list)
    global_validation_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "summary": self.summary,
            "nodes": [node.to_dict() for node in self.nodes],
            "global_validation_commands": list(self.global_validation_commands),
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DependencyGraph":
        if not isinstance(payload, dict):
            raise ValueError("Dependency graph payload must be an object.")
        feature_name = str(payload.get("feature_name", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        if not feature_name:
            raise ValueError("Dependency graph is missing required field: feature_name.")
        if not summary:
            raise ValueError("Dependency graph is missing required field: summary.")

        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ValueError("Dependency graph is missing required field: nodes.")
        nodes = [
            ExecutionNode.from_dict(item, fallback_id=index)
            for index, item in enumerate(raw_nodes, start=1)
            if isinstance(item, dict)
        ]
        if not nodes:
            raise ValueError("Dependency graph must include at least one node.")

        graph = cls(
            feature_name=feature_name,
            summary=summary,
            nodes=nodes,
            global_validation_commands=ImplementationPlan._coerce_string_list(
                payload.get("global_validation_commands")
            ),
        )
        graph.validate()
        return graph

    def validate(self) -> None:
        ids = [node.node_id for node in self.nodes]
        duplicates = {node_id for node_id in ids if ids.count(node_id) > 1}
        if duplicates:
            raise ValueError(
                "Dependency graph has duplicate node identifiers: "
                + ", ".join(sorted(duplicates))
            )

        node_id_set = set(ids)
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in node_id_set:
                    raise ValueError(
                        f"Dependency graph references unknown dependency '{dep}' for node '{node.node_id}'."
                    )
                if dep == node.node_id:
                    raise ValueError(
                        f"Dependency graph node '{node.node_id}' cannot depend on itself."
                    )

        # Kahn's algorithm for cycle detection.
        indegree: dict[str, int] = {node.node_id: 0 for node in self.nodes}
        adjacency: dict[str, set[str]] = {node.node_id: set() for node in self.nodes}
        for node in self.nodes:
            for dep in node.depends_on:
                adjacency.setdefault(dep, set()).add(node.node_id)
                indegree[node.node_id] += 1

        queue = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while queue:
            current = queue.pop(0)
            visited += 1
            for child in adjacency.get(current, ()):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if visited != len(self.nodes):
            raise ValueError("Dependency graph contains a cycle.")

    def all_new_files(self) -> list[str]:
        return self._collect_unique_file_paths(lambda node: node.new_files)

    def all_modified_files(self) -> list[str]:
        return self._collect_unique_file_paths(lambda node: node.modified_files)

    def all_steps(self) -> list[str]:
        steps: list[str] = []
        seen: set[str] = set()
        for node in self.nodes:
            for step in node.steps:
                cleaned = step.strip()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    steps.append(cleaned)
        return steps

    def _collect_unique_file_paths(
        self,
        extractor: Any,
    ) -> list[str]:
        collected: list[str] = []
        seen: set[str] = set()
        for node in self.nodes:
            for raw_path in extractor(node):
                cleaned = raw_path.strip()
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                collected.append(cleaned)
        return collected


@dataclass(frozen=True)
class NodeExecutionRecord:
    node_id: str
    trace_id: str
    status: NodeStatus
    level1_passed: bool
    duration_seconds: float
    note: str = ""
    commands_run: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrchestrationTelemetry:
    total_node_seconds: float = 0.0
    wall_clock_seconds: float = 0.0
    parallel_gain: float = 1.0
    initial_concurrency: int = 1
    final_concurrency: int = 1
    adaptive_throttle_events: int = 0
    level1_pass_nodes: int = 0
    level1_failed_nodes: int = 0
    level2_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_node_seconds": self.total_node_seconds,
            "wall_clock_seconds": self.wall_clock_seconds,
            "parallel_gain": self.parallel_gain,
            "initial_concurrency": self.initial_concurrency,
            "final_concurrency": self.final_concurrency,
            "adaptive_throttle_events": self.adaptive_throttle_events,
            "level1_pass_nodes": self.level1_pass_nodes,
            "level1_failed_nodes": self.level1_failed_nodes,
            "level2_failures": self.level2_failures,
        }


@dataclass(frozen=True)
class AttemptRecord:
    attempt_number: int
    strategy_name: str
    failure_type: FailureType
    applied: bool
    note: str = ""
    changed_files: tuple[Path, ...] = ()
    diff_summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileRollback:
    """Snapshot required to restore a changed file after a failed verification."""

    path: Path
    existed_before: bool
    content: str | None = None


@dataclass(frozen=True)
class SessionReport:
    command: str
    initial_result: CommandResult
    final_result: CommandResult
    attempts: list[AttemptRecord] = field(default_factory=list)
    node_records: list[NodeExecutionRecord] = field(default_factory=list)
    telemetry: OrchestrationTelemetry | None = None
    success: bool = False
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "initial_result": self._command_result_to_dict(self.initial_result),
            "final_result": self._command_result_to_dict(self.final_result),
            "attempts": [self._attempt_record_to_dict(attempt) for attempt in self.attempts],
            "node_records": [self._node_record_to_dict(record) for record in self.node_records],
            "telemetry": None if self.telemetry is None else self.telemetry.to_dict(),
            "success": self.success,
            "blocked_reason": self.blocked_reason,
        }

    def to_json(self, indent: int | None = 2) -> str:
        """Serialize report state for persistence or interrupted-session recovery."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionReport":
        attempts_payload = payload.get("attempts", [])
        attempts = [
            cls._attempt_record_from_dict(item)
            for item in attempts_payload
            if isinstance(item, dict)
        ]
        return cls(
            command=str(payload.get("command", "")),
            initial_result=cls._command_result_from_dict(payload.get("initial_result")),
            final_result=cls._command_result_from_dict(payload.get("final_result")),
            attempts=attempts,
            node_records=cls._node_records_from_payload(payload.get("node_records")),
            telemetry=cls._telemetry_from_payload(payload.get("telemetry")),
            success=bool(payload.get("success", False)),
            blocked_reason=(
                None
                if payload.get("blocked_reason") is None
                else str(payload.get("blocked_reason"))
            ),
        )

    @classmethod
    def from_json(cls, raw_json: str) -> "SessionReport":
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            raise ValueError("Session report JSON must be an object.")
        return cls.from_dict(payload)

    @staticmethod
    def _command_result_to_dict(result: CommandResult) -> dict[str, Any]:
        return {
            "command": result.command,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    @staticmethod
    def _command_result_from_dict(payload: Any) -> CommandResult:
        if not isinstance(payload, dict):
            return CommandResult(command="", return_code=1)
        return CommandResult(
            command=str(payload.get("command", "")),
            return_code=int(payload.get("return_code", 1)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
        )

    @staticmethod
    def _attempt_record_to_dict(attempt: AttemptRecord) -> dict[str, Any]:
        return {
            "attempt_number": attempt.attempt_number,
            "strategy_name": attempt.strategy_name,
            "failure_type": attempt.failure_type.value,
            "applied": attempt.applied,
            "note": attempt.note,
            "changed_files": [str(path) for path in attempt.changed_files],
            "diff_summary": list(attempt.diff_summary),
        }

    @classmethod
    def _attempt_record_from_dict(cls, payload: dict[str, Any]) -> AttemptRecord:
        failure_type = cls._failure_type_from_value(payload.get("failure_type"))
        changed_files_payload = payload.get("changed_files", [])
        diff_summary_payload = payload.get("diff_summary", [])
        return AttemptRecord(
            attempt_number=int(payload.get("attempt_number", 0)),
            strategy_name=str(payload.get("strategy_name", "")),
            failure_type=failure_type,
            applied=bool(payload.get("applied", False)),
            note=str(payload.get("note", "")),
            changed_files=tuple(
                Path(path)
                for path in changed_files_payload
                if isinstance(path, str) and path.strip()
            ),
            diff_summary=tuple(
                item
                for item in diff_summary_payload
                if isinstance(item, str)
            ),
        )

    @staticmethod
    def _failure_type_from_value(value: Any) -> FailureType:
        if isinstance(value, FailureType):
            return value
        if isinstance(value, str):
            try:
                return FailureType(value)
            except ValueError:
                return FailureType.UNKNOWN
        return FailureType.UNKNOWN

    @staticmethod
    def _node_record_to_dict(record: NodeExecutionRecord) -> dict[str, Any]:
        return {
            "node_id": record.node_id,
            "trace_id": record.trace_id,
            "status": record.status.value,
            "level1_passed": record.level1_passed,
            "duration_seconds": record.duration_seconds,
            "note": record.note,
            "commands_run": list(record.commands_run),
        }

    @classmethod
    def _node_records_from_payload(cls, payload: Any) -> list[NodeExecutionRecord]:
        if not isinstance(payload, list):
            return []
        records: list[NodeExecutionRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            status_value = item.get("status")
            status = NodeStatus.PENDING
            if isinstance(status_value, str):
                try:
                    status = NodeStatus(status_value)
                except ValueError:
                    status = NodeStatus.PENDING
            records.append(
                NodeExecutionRecord(
                    node_id=str(item.get("node_id", "")),
                    trace_id=str(item.get("trace_id", "")),
                    status=status,
                    level1_passed=bool(item.get("level1_passed", False)),
                    duration_seconds=float(item.get("duration_seconds", 0.0)),
                    note=str(item.get("note", "")),
                    commands_run=tuple(
                        command
                        for command in item.get("commands_run", [])
                        if isinstance(command, str) and command.strip()
                    ),
                )
            )
        return records

    @classmethod
    def _telemetry_from_payload(cls, payload: Any) -> OrchestrationTelemetry | None:
        if not isinstance(payload, dict):
            return None
        return OrchestrationTelemetry(
            total_node_seconds=float(payload.get("total_node_seconds", 0.0)),
            wall_clock_seconds=float(payload.get("wall_clock_seconds", 0.0)),
            parallel_gain=float(payload.get("parallel_gain", 1.0)),
            initial_concurrency=int(payload.get("initial_concurrency", 1)),
            final_concurrency=int(payload.get("final_concurrency", 1)),
            adaptive_throttle_events=int(payload.get("adaptive_throttle_events", 0)),
            level1_pass_nodes=int(payload.get("level1_pass_nodes", 0)),
            level1_failed_nodes=int(payload.get("level1_failed_nodes", 0)),
            level2_failures=int(payload.get("level2_failures", 0)),
        )
