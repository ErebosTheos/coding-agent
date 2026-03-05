import json
import os
from codegen_agent.llm.router import LLMRouter, _RetryingLLMClient
from codegen_agent.llm.gemini_cli import GeminiCLIClient
from codegen_agent.llm.anthropic_api import AnthropicAPIClient


def _primary(client):
    assert isinstance(client, _RetryingLLMClient)
    raw = client._primary
    # CODEGEN_CACHE=1 in .env wraps the call-client in CachingLLMClient, but
    # _RetryingLLMClient._primary is always the unwrapped raw provider.
    # If it somehow is wrapped (future regression), unwrap one level for the check.
    from codegen_agent.llm.caching_client import CachingLLMClient
    if isinstance(raw, CachingLLMClient):
        return raw._client
    return raw

def test_router_default_config():
    """When CODEGEN_PROVIDER=gemini (no .env override), router returns GeminiCLIClient."""
    old = os.environ.pop("CODEGEN_PROVIDER", None)
    try:
        router = LLMRouter.__new__(LLMRouter)
        router.config = {
            "default": {"provider": "gemini_cli", "model": None},
            "roles": {r: {"provider": "gemini_cli", "model": None}
                      for r in ("planner", "architect", "executor", "tester", "healer", "qa_auditor")},
        }
        router._clients = {}
        client = router.get_client_for_role("planner")
        assert isinstance(_primary(client), GeminiCLIClient)
    finally:
        if old is not None:
            os.environ["CODEGEN_PROVIDER"] = old

def test_router_env_provider_claude(monkeypatch, tmp_path):
    """CODEGEN_PROVIDER=claude yields AnthropicAPIClient."""
    monkeypatch.setenv("CODEGEN_PROVIDER", "claude")
    monkeypatch.chdir(tmp_path)  # no .env in tmp_path
    router = LLMRouter()
    client = router.get_client_for_role("planner")
    primary = _primary(client)
    assert isinstance(primary, AnthropicAPIClient)
    assert primary.model == "claude-sonnet-4-6"

def test_router_json_config(tmp_path):
    config_file = tmp_path / "config.json"
    config = {
        "roles": {
            "planner": {"provider": "anthropic_api", "model": "claude-3-haiku"}
        }
    }
    config_file.write_text(json.dumps(config))
    
    router = LLMRouter(str(config_file))
    client = router.get_client_for_role("planner")
    from codegen_agent.llm.anthropic_api import AnthropicAPIClient
    primary = _primary(client)
    assert isinstance(primary, AnthropicAPIClient)
    assert primary.model == "claude-3-haiku"

def test_router_fallback_to_default(tmp_path):
    config_file = tmp_path / "config.json"
    config = {
        "default": {"provider": "claude_cli"}
    }
    config_file.write_text(json.dumps(config))
    
    router = LLMRouter(str(config_file))
    client = router.get_client_for_role("architect")
    from codegen_agent.llm.claude_cli import ClaudeCLIClient
    assert isinstance(_primary(client), ClaudeCLIClient)
