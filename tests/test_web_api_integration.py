import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient
    from senior_agent.web_api import create_app
except Exception as exc:  # pragma: no cover - dependency guard
    _IMPORT_ERROR = exc


@unittest.skipIf(_IMPORT_ERROR is not None, "web_api dependencies are unavailable.")
class WebAPIIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_client(
        self,
        *,
        api_key: str | None,
        bind_host: str,
        allow_unsecure: bool = False,
    ) -> TestClient:
        with mock.patch(
            "senior_agent.web_api._resolve_role_provider_map",
            return_value={"architect": "gemini", "developer": "codex"},
        ):
            app = create_app(
                provider="gemini",
                workspace=self.workspace,
                api_key=api_key,
                bind_host=bind_host,
                allow_unsecure=allow_unsecure,
            )
        return TestClient(app)

    def test_execute_requires_api_key_when_configured(self) -> None:
        client = self._build_client(api_key="secret-key", bind_host="127.0.0.1")

        with mock.patch("senior_agent.web_api._enqueue_job_execution"):
            unauthorized = client.post(
                "/api/execute",
                json={"requirement": "Build feature"},
            )
            self.assertEqual(unauthorized.status_code, 401)

            authorized = client.post(
                "/api/execute",
                headers={"X-API-Key": "secret-key"},
                json={"requirement": "Build feature"},
            )
            self.assertEqual(authorized.status_code, 202)
            payload = authorized.json()
            self.assertEqual(payload["job_type"], "execute_feature")
            self.assertIn("job_id", payload)

    def test_execute_program_requires_api_key_when_configured(self) -> None:
        client = self._build_client(api_key="secret-key", bind_host="127.0.0.1")

        with mock.patch("senior_agent.web_api._enqueue_job_execution"):
            unauthorized = client.post(
                "/api/execute-program",
                json={"requirement": "Build product", "max_phases": 3},
            )
            self.assertEqual(unauthorized.status_code, 401)

            authorized = client.post(
                "/api/execute-program",
                headers={"X-API-Key": "secret-key"},
                json={"requirement": "Build product", "max_phases": 3},
            )
            self.assertEqual(authorized.status_code, 202)
            payload = authorized.json()
            self.assertEqual(payload["job_type"], "execute_program")

    def test_heal_rejects_non_local_bind_without_unsecure_flag(self) -> None:
        client = self._build_client(
            api_key="secret-key",
            bind_host="0.0.0.0",
            allow_unsecure=False,
        )

        response = client.post(
            "/api/heal",
            headers={"X-API-Key": "secret-key"},
            json={"command": "python -m pytest"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("--unsecure", response.json().get("detail", ""))

    def test_heal_allows_non_local_bind_with_unsecure_flag(self) -> None:
        client = self._build_client(
            api_key="secret-key",
            bind_host="0.0.0.0",
            allow_unsecure=True,
        )

        with mock.patch("senior_agent.web_api._enqueue_job_execution"):
            response = client.post(
                "/api/heal",
                headers={"X-API-Key": "secret-key"},
                json={"command": "python -m pytest"},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["job_type"], "self_heal")


if __name__ == "__main__":
    unittest.main()
