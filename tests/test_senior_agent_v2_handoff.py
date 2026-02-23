import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent_v2.handoff import HandoffManager, HandoffVerificationError
from senior_agent_v2.models import Contract, DependencyGraph, ExecutionNode


def _sample_graph() -> DependencyGraph:
    contract_node = ExecutionNode(
        node_id="n1_contract",
        title="Freeze contracts",
        summary="Define contract for user service",
        new_files=["src/contracts/user.py"],
        modified_files=[],
        steps=["Write contract"],
        validation_commands=["python -m py_compile src/contracts/user.py"],
        depends_on=[],
        contract=Contract(
            node_id="n1_contract",
            purpose="Define request and response shape for user endpoints.",
            inputs=[{"name": "user_id", "type": "str"}],
            outputs=[{"name": "user", "type": "dict"}],
            public_api=["get_user(user_id: str) -> dict"],
            invariants=["User object includes id and status."],
            error_taxonomy={"UserNotFound": "Raised when user_id does not exist."},
            examples=[{"input": {"user_id": "u_1"}, "output": {"id": "u_1"}}],
        ),
        contract_node=True,
        shared_resources=["user_contracts"],
    )
    implementation_node = ExecutionNode(
        node_id="n2_impl",
        title="Implement user service",
        summary="Use frozen contract to implement user lookup",
        new_files=[],
        modified_files=["src/services/user_service.py"],
        steps=["Implement contract"],
        validation_commands=["python -m pytest tests/test_user_service.py -q"],
        depends_on=["n1_contract"],
        contract=Contract(
            node_id="n2_impl",
            purpose="Implement user lookup API exactly as defined in n1_contract.",
            inputs=[{"name": "user_id", "type": "str"}],
            outputs=[{"name": "user", "type": "dict"}],
            public_api=["get_user(user_id: str) -> dict"],
            invariants=["Function returns dict with id key."],
            error_taxonomy={"UserNotFound": "Raised when user_id does not exist."},
            examples=[{"input": {"user_id": "u_1"}, "output": {"id": "u_1"}}],
        ),
        contract_node=False,
        shared_resources=["user_contracts"],
    )
    return DependencyGraph(
        feature_name="User Service",
        summary="Two-node graph with contract freeze and implementation.",
        nodes=[contract_node, implementation_node],
        global_validation_commands=["python -m pytest -q"],
    )


class HandoffManagerTests(unittest.TestCase):
    def test_rejects_workspace_escape_handoff_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)

            with self.assertRaisesRegex(ValueError, "workspace-relative"):
                HandoffManager(workspace_root=workspace, handoff_dir="/tmp/handoff")

            manager = HandoffManager(workspace_root=workspace, handoff_dir="../escape")
            with self.assertRaisesRegex(
                HandoffVerificationError,
                "escapes workspace root",
            ):
                _ = manager.paths

    def test_export_writes_handoff_and_contract_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            graph = _sample_graph()
            manager = HandoffManager(workspace_root=workspace)

            artifact = manager.export(graph, timestamp="2026-02-23T17:30:00Z")
            verified = manager.verify(expected_checksum=artifact.checksum)

            self.assertEqual(artifact.checksum, graph.compute_handoff_checksum())
            self.assertEqual(verified.checksum, artifact.checksum)

            handoff_json_path = workspace / ".senior_agent" / "handoff.json"
            handoff_checksum_path = workspace / ".senior_agent" / "handoff.checksum"
            self.assertTrue(handoff_json_path.exists())
            self.assertTrue(handoff_checksum_path.exists())

            handoff_payload = json.loads(handoff_json_path.read_text(encoding="utf-8"))
            self.assertEqual(handoff_payload["timestamp"], "2026-02-23T17:30:00Z")
            self.assertEqual(handoff_payload["checksum"], artifact.checksum)

            checksum_lines = handoff_checksum_path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(line.startswith("graph_checksum ") for line in checksum_lines))
            self.assertTrue(any(line.startswith("handoff_json_sha256 ") for line in checksum_lines))
            self.assertTrue(any(line.startswith("contract n1_contract ") for line in checksum_lines))
            self.assertTrue(any(line.startswith("contract n2_impl ") for line in checksum_lines))

            contract_path = workspace / ".senior_agent" / "nodes" / "n1_contract" / "contract.json"
            self.assertTrue(contract_path.exists())
            contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))
            self.assertEqual(contract_payload["node_id"], "n1_contract")

    def test_verify_rejects_tampered_contract_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            manager = HandoffManager(workspace_root=workspace)
            manager.export(_sample_graph(), timestamp="2026-02-23T17:30:00Z")

            contract_path = workspace / ".senior_agent" / "nodes" / "n1_contract" / "contract.json"
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            payload["purpose"] = "Tampered purpose"
            contract_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                HandoffVerificationError,
                "Contract file checksum mismatch",
            ):
                manager.verify()

    def test_verify_rejects_tampered_handoff_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            manager = HandoffManager(workspace_root=workspace)
            manager.export(_sample_graph(), timestamp="2026-02-23T17:30:00Z")

            handoff_path = workspace / ".senior_agent" / "handoff.json"
            payload = json.loads(handoff_path.read_text(encoding="utf-8"))
            payload["graph"]["nodes"][0]["contract"]["purpose"] = "Tampered in handoff"
            handoff_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                HandoffVerificationError,
                "contract graph was modified after export",
            ):
                manager.verify()


if __name__ == "__main__":
    unittest.main()
