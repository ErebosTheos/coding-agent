from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def _coerce_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)
    return values


def _coerce_string_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    values: dict[str, str] = {}
    for key, value in raw.items():
        cleaned_key = str(key).strip()
        cleaned_value = str(value).strip()
        if cleaned_key:
            values[cleaned_key] = cleaned_value
    return values


def _coerce_dict_list(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    values: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cleaned: dict[str, str] = {}
        for key, value in item.items():
            cleaned_key = str(key).strip()
            if not cleaned_key:
                continue
            cleaned[cleaned_key] = str(value).strip()
        if cleaned:
            values.append(cleaned)
    return values


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
class Contract:
    """Explicit behavioral contract for an atomic work unit."""

    node_id: str
    purpose: str
    inputs: list[dict[str, str]] = field(default_factory=list)
    outputs: list[dict[str, str]] = field(default_factory=list)
    public_api: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)
    error_taxonomy: dict[str, str] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "purpose": self.purpose,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "public_api": self.public_api,
            "invariants": self.invariants,
            "error_taxonomy": self.error_taxonomy,
            "examples": self.examples,
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        fallback_node_id: str = "",
    ) -> "Contract":
        if not isinstance(payload, dict):
            raise ValueError("Contract payload must be an object.")
        node_id = str(payload.get("node_id", "")).strip() or fallback_node_id
        purpose = str(payload.get("purpose", "")).strip()
        if not node_id:
            raise ValueError("Contract is missing required field: node_id.")
        if not purpose:
            raise ValueError(f"Contract '{node_id}' is missing required field: purpose.")
        examples = payload.get("examples")
        return cls(
            node_id=node_id,
            purpose=purpose,
            inputs=_coerce_dict_list(payload.get("inputs")),
            outputs=_coerce_dict_list(payload.get("outputs")),
            public_api=_coerce_string_list(payload.get("public_api")),
            invariants=_coerce_string_list(payload.get("invariants")),
            error_taxonomy=_coerce_string_dict(payload.get("error_taxonomy")),
            examples=list(examples) if isinstance(examples, list) else [],
        )

    def compute_checksum(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionNode:
    """Atomic graph node in the V2 Parallel Grid."""

    node_id: str
    title: str
    summary: str
    new_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    contract: Contract | None = None
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
            "contract": None if self.contract is None else self.contract.to_dict(),
            "contract_node": self.contract_node,
            "shared_resources": list(self.shared_resources),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any], fallback_id: int) -> "ExecutionNode":
        if not isinstance(payload, dict):
            raise ValueError("Execution node payload must be an object.")
        node_id = str(payload.get("node_id", "")).strip() or f"node_{fallback_id}"
        title = str(payload.get("title", "")).strip() or node_id
        summary = str(payload.get("summary", "")).strip() or title

        raw_contract = payload.get("contract")
        contract: Contract | None = None
        if isinstance(raw_contract, dict):
            contract = Contract.from_dict(raw_contract, fallback_node_id=node_id)

        return cls(
            node_id=node_id,
            title=title,
            summary=summary,
            new_files=_coerce_string_list(payload.get("new_files")),
            modified_files=_coerce_string_list(payload.get("modified_files")),
            steps=_coerce_string_list(payload.get("steps")),
            validation_commands=_coerce_string_list(payload.get("validation_commands")),
            depends_on=_coerce_string_list(payload.get("depends_on")),
            contract=contract,
            contract_node=bool(payload.get("contract_node", False)),
            shared_resources=_coerce_string_list(payload.get("shared_resources")),
        )


@dataclass(frozen=True)
class DependencyGraph:
    """The frozen execution plan for Phase 2 implementation."""

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
            global_validation_commands=_coerce_string_list(
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

    def compute_handoff_checksum(self) -> str:
        """Computes a master checksum for all node contracts."""

        node_checksums: list[str] = []
        for node in sorted(self.nodes, key=lambda n: n.node_id):
            if node.contract is None:
                continue
            node_checksums.append(f"{node.node_id}:{node.contract.compute_checksum()}")
        raw_manifest = "|".join(node_checksums)
        return hashlib.sha256(raw_manifest.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HandoffArtifact:
    """The Phase 1 -> Phase 2 handoff package."""

    graph: DependencyGraph
    checksum: str
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph": self.graph.to_dict(),
            "checksum": self.checksum,
            "timestamp": self.timestamp,
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HandoffArtifact":
        if not isinstance(payload, dict):
            raise ValueError("Handoff payload must be an object.")
        graph_payload = payload.get("graph")
        if not isinstance(graph_payload, dict):
            raise ValueError("Handoff payload is missing required field: graph.")
        checksum = str(payload.get("checksum", "")).strip()
        timestamp = str(payload.get("timestamp", "")).strip()
        if not checksum:
            raise ValueError("Handoff payload is missing required field: checksum.")
        if not timestamp:
            raise ValueError("Handoff payload is missing required field: timestamp.")
        return cls(
            graph=DependencyGraph.from_dict(graph_payload),
            checksum=checksum,
            timestamp=timestamp,
        )

    @classmethod
    def from_json(cls, raw: str) -> "HandoffArtifact":
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Handoff JSON payload must be an object.")
        return cls.from_dict(payload)


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
    grid_efficiency: float = 0.0
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
            "grid_efficiency": self.grid_efficiency,
            "initial_concurrency": self.initial_concurrency,
            "final_concurrency": self.final_concurrency,
            "adaptive_throttle_events": self.adaptive_throttle_events,
            "level1_pass_nodes": self.level1_pass_nodes,
            "level1_failed_nodes": self.level1_failed_nodes,
            "level2_failures": self.level2_failures,
        }


@dataclass(frozen=True)
class SessionReport:
    command: str
    initial_result: CommandResult
    final_result: CommandResult
    node_records: list[NodeExecutionRecord] = field(default_factory=list)
    telemetry: OrchestrationTelemetry | None = None
    success: bool = False
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "initial_result": self._command_result_to_dict(self.initial_result),
            "final_result": self._command_result_to_dict(self.final_result),
            "node_records": [self._node_record_to_dict(record) for record in self.node_records],
            "telemetry": None if self.telemetry is None else self.telemetry.to_dict(),
            "success": self.success,
            "blocked_reason": self.blocked_reason,
        }

    @staticmethod
    def _command_result_to_dict(result: CommandResult) -> dict[str, Any]:
        return {
            "command": result.command,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

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


@dataclass(frozen=True)
class FileRollback:
    """Snapshot required to restore a changed file after a failed verification."""

    path: Path
    existed_before: bool
    content: str | None = None
