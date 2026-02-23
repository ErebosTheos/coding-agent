from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from senior_agent_v2.models import Contract, DependencyGraph, HandoffArtifact


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class HandoffVerificationError(RuntimeError):
    """Raised when a Phase 1 handoff artifact fails integrity verification."""


@dataclass(frozen=True)
class HandoffPaths:
    root_dir: Path
    handoff_json_path: Path
    handoff_checksum_path: Path
    nodes_dir: Path


class HandoffManager:
    """Exports and verifies Phase 1 contract handoff artifacts."""

    def __init__(self, workspace_root: Path, handoff_dir: str = ".senior_agent") -> None:
        self._workspace_root = workspace_root.resolve()
        candidate = Path(handoff_dir)
        if candidate.is_absolute():
            raise ValueError("handoff_dir must be a workspace-relative path.")
        self._handoff_relative_dir = candidate

    @property
    def paths(self) -> HandoffPaths:
        root_dir = (self._workspace_root / self._handoff_relative_dir).resolve()
        if not self._is_within_workspace(root_dir):
            raise HandoffVerificationError(
                f"Handoff directory escapes workspace root: {root_dir.as_posix()}"
            )
        return HandoffPaths(
            root_dir=root_dir,
            handoff_json_path=root_dir / "handoff.json",
            handoff_checksum_path=root_dir / "handoff.checksum",
            nodes_dir=root_dir / "nodes",
        )

    def export(self, graph: DependencyGraph, *, timestamp: str | None = None) -> HandoffArtifact:
        artifact = HandoffArtifact(
            graph=graph,
            checksum=graph.compute_handoff_checksum(),
            timestamp=timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        paths = self.paths
        paths.root_dir.mkdir(parents=True, exist_ok=True)
        self._write_handoff_json(paths, artifact)
        self._write_node_contracts(paths, graph)
        self._write_checksum_manifest(paths, artifact)
        return artifact

    def load(self) -> HandoffArtifact:
        handoff_json_path = self.paths.handoff_json_path
        if not handoff_json_path.exists():
            raise HandoffVerificationError(
                f"Handoff artifact missing: {handoff_json_path.as_posix()}"
            )
        raw = handoff_json_path.read_text(encoding="utf-8")
        try:
            return HandoffArtifact.from_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HandoffVerificationError(
                f"Invalid handoff artifact JSON: {handoff_json_path.as_posix()}"
            ) from exc

    def verify(self, *, expected_checksum: str | None = None) -> HandoffArtifact:
        paths = self.paths
        artifact = self.load()
        manifest = self._load_checksum_manifest(paths)
        graph_manifest_checksum, handoff_json_hash, contract_hashes = manifest

        if graph_manifest_checksum != artifact.checksum:
            raise HandoffVerificationError(
                "Handoff checksum mismatch between handoff.json and handoff.checksum."
            )

        recomputed_graph_checksum = artifact.graph.compute_handoff_checksum()
        if recomputed_graph_checksum != artifact.checksum:
            raise HandoffVerificationError(
                "Handoff checksum mismatch: contract graph was modified after export."
            )

        if expected_checksum is not None and artifact.checksum != expected_checksum:
            raise HandoffVerificationError(
                "Handoff checksum mismatch against expected frozen checksum."
            )

        current_handoff_raw = paths.handoff_json_path.read_text(encoding="utf-8")
        current_handoff_hash = _sha256_text(current_handoff_raw)
        if handoff_json_hash != current_handoff_hash:
            raise HandoffVerificationError(
                "Handoff JSON hash mismatch: handoff.json was modified after export."
            )

        expected_contract_hashes: dict[str, str] = {
            node.node_id: node.contract.compute_checksum()
            for node in artifact.graph.nodes
            if node.contract is not None
        }
        if set(contract_hashes) != set(expected_contract_hashes):
            raise HandoffVerificationError(
                "Contract manifest mismatch: contract node set diverged from handoff graph."
            )

        for node_id, expected_hash in expected_contract_hashes.items():
            manifest_hash = contract_hashes.get(node_id)
            if manifest_hash != expected_hash:
                raise HandoffVerificationError(
                    f"Contract checksum mismatch in manifest for node '{node_id}'."
                )
            contract_path = paths.nodes_dir / node_id / "contract.json"
            if not contract_path.exists():
                raise HandoffVerificationError(
                    f"Missing contract file for node '{node_id}': {contract_path.as_posix()}"
                )
            raw_contract = contract_path.read_text(encoding="utf-8")
            try:
                payload = json.loads(raw_contract)
            except json.JSONDecodeError as exc:
                raise HandoffVerificationError(
                    f"Invalid contract JSON for node '{node_id}'."
                ) from exc
            if not isinstance(payload, dict):
                raise HandoffVerificationError(
                    f"Invalid contract payload type for node '{node_id}'."
                )
            try:
                contract = Contract.from_dict(payload, fallback_node_id=node_id)
            except ValueError as exc:
                raise HandoffVerificationError(
                    f"Invalid contract schema for node '{node_id}'."
                ) from exc
            actual_hash = contract.compute_checksum()
            if actual_hash != expected_hash:
                raise HandoffVerificationError(
                    f"Contract file checksum mismatch for node '{node_id}'."
                )
        return artifact

    @staticmethod
    def _write_handoff_json(paths: HandoffPaths, artifact: HandoffArtifact) -> None:
        paths.handoff_json_path.write_text(
            artifact.to_json(indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_node_contracts(paths: HandoffPaths, graph: DependencyGraph) -> None:
        shutil.rmtree(paths.nodes_dir, ignore_errors=True)
        paths.nodes_dir.mkdir(parents=True, exist_ok=True)
        for node in graph.nodes:
            if node.contract is None:
                continue
            node_dir = paths.nodes_dir / node.node_id
            node_dir.mkdir(parents=True, exist_ok=True)
            contract_path = node_dir / "contract.json"
            contract_path.write_text(
                json.dumps(node.contract.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _write_checksum_manifest(paths: HandoffPaths, artifact: HandoffArtifact) -> None:
        handoff_json_hash = _sha256_text(paths.handoff_json_path.read_text(encoding="utf-8"))
        contract_checksums = [
            (node.node_id, node.contract.compute_checksum())
            for node in artifact.graph.nodes
            if node.contract is not None
        ]
        contract_checksums.sort(key=lambda item: item[0])
        lines = [
            f"graph_checksum {artifact.checksum}",
            f"handoff_json_sha256 {handoff_json_hash}",
        ]
        lines.extend(f"contract {node_id} {checksum}" for node_id, checksum in contract_checksums)
        paths.handoff_checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _load_checksum_manifest(
        paths: HandoffPaths,
    ) -> tuple[str, str, dict[str, str]]:
        if not paths.handoff_checksum_path.exists():
            raise HandoffVerificationError(
                f"Handoff checksum file missing: {paths.handoff_checksum_path.as_posix()}"
            )
        graph_checksum = ""
        handoff_json_sha256 = ""
        contract_hashes: dict[str, str] = {}

        lines = paths.handoff_checksum_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split()
            if len(parts) == 2 and parts[0] == "graph_checksum":
                graph_checksum = parts[1]
                continue
            if len(parts) == 2 and parts[0] == "handoff_json_sha256":
                handoff_json_sha256 = parts[1]
                continue
            if len(parts) == 3 and parts[0] == "contract":
                contract_hashes[parts[1]] = parts[2]
                continue
            raise HandoffVerificationError(
                f"Invalid handoff checksum line format: '{line}'."
            )

        if not graph_checksum:
            raise HandoffVerificationError("Missing graph_checksum entry in handoff.checksum.")
        if not handoff_json_sha256:
            raise HandoffVerificationError(
                "Missing handoff_json_sha256 entry in handoff.checksum."
            )
        return graph_checksum, handoff_json_sha256, contract_hashes

    def _is_within_workspace(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self._workspace_root)
            return True
        except ValueError:
            return False
