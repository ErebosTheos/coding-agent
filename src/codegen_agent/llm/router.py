import json
import os
from pathlib import Path
from typing import Dict, Any, Optional
from .protocol import LLMClient
from .gemini_cli import GeminiCLIClient
from .claude_cli import ClaudeCLIClient
from .anthropic_api import AnthropicAPIClient
from .openai_api import OpenAIClient
from .codex_cli import CodexCLIClient

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# Short alias → canonical provider name used in config
_PROVIDER_ALIASES = {
    # Claude Code CLI binary (local, no API key needed)
    "claude_code":   "claude_cli",
    "claude_cli":    "claude_cli",
    # Anthropic HTTP API (fast, requires ANTHROPIC_API_KEY)
    "claude":        "anthropic_api",
    "anthropic":     "anthropic_api",
    "anthropic_api": "anthropic_api",
    # Gemini CLI
    "gemini":        "gemini_cli",
    "gemini_cli":    "gemini_cli",
    # OpenAI HTTP API
    "openai":        "openai_api",
    "openai_api":    "openai_api",
    # Codex CLI (local binary)
    "codex":         "codex_cli",
    "codex_cli":     "codex_cli",
}

# Default models per provider when none is specified
_DEFAULT_MODELS = {
    "anthropic_api": "claude-sonnet-4-6",
    "openai_api":    "gpt-4o",
    "gemini_cli":    None,   # CLI picks its own default
    "claude_cli":    None,
    "codex_cli":     None,   # CLI picks its own default
}

_ALL_ROLES = ("planner", "architect", "executor", "tester", "healer", "qa_auditor")


def _load_dotenv(path: str = ".env") -> Dict[str, str]:
    """Minimal .env parser — no external dependency required."""
    env: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        env[key] = value
    return env


class LLMRouter:
    def __init__(self, config_path: Optional[str] = None):
        # Load .env first so env vars are available before config resolution
        dotenv = _load_dotenv()
        for k, v in dotenv.items():
            if k not in os.environ:
                os.environ[k] = v

        self.config = self._load_config(config_path)
        self._clients: Dict[str, LLMClient] = {}

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        # 1. Explicit config file (highest priority)
        if config_path and os.path.exists(config_path):
            if config_path.endswith((".yaml", ".yml")):
                if HAS_YAML:
                    with open(config_path) as f:
                        return yaml.safe_load(f)
                raise ImportError("pyyaml required for YAML configs.")
            with open(config_path) as f:
                return json.load(f)

        # 2. Build config from env vars
        #    CODEGEN_PROVIDER sets the global default.
        #    CODEGEN_<ROLE>_PROVIDER overrides per role.
        #    CODEGEN_MODEL / CODEGEN_<ROLE>_MODEL sets the model.
        default_provider = _PROVIDER_ALIASES.get(
            os.environ.get("CODEGEN_PROVIDER", "gemini_cli").lower(),
            "gemini_cli",
        )
        default_model = os.environ.get("CODEGEN_MODEL") or _DEFAULT_MODELS.get(default_provider)

        roles: Dict[str, Any] = {}
        for role in _ALL_ROLES:
            env_key = f"CODEGEN_{role.upper()}_PROVIDER"
            model_key = f"CODEGEN_{role.upper()}_MODEL"
            provider = _PROVIDER_ALIASES.get(
                os.environ.get(env_key, "").lower(),
                default_provider,
            )
            model = os.environ.get(model_key) or (
                _DEFAULT_MODELS.get(provider) if provider != default_provider else default_model
            )
            roles[role] = {"provider": provider, "model": model}

        return {
            "default": {"provider": default_provider, "model": default_model},
            "roles": roles,
        }

    def get_client_for_role(self, role: str) -> LLMClient:
        role_config = self.config.get("roles", {}).get(role, self.config.get("default", {}))
        provider = role_config.get("provider", "gemini_cli")
        model = role_config.get("model")

        client_key = f"{provider}:{model}"
        if client_key not in self._clients:
            self._clients[client_key] = self._create_client(provider, model)
        return self._clients[client_key]

    def _create_client(self, provider: str, model: Optional[str]) -> LLMClient:
        if provider == "anthropic_api":
            return AnthropicAPIClient(model=model or _DEFAULT_MODELS["anthropic_api"])
        if provider == "openai_api":
            return OpenAIClient(model=model or _DEFAULT_MODELS["openai_api"])
        if provider == "gemini_cli":
            return GeminiCLIClient(model=model)
        if provider == "claude_cli":
            return ClaudeCLIClient(model=model)
        if provider == "codex_cli":
            return CodexCLIClient(model=model)
        raise ValueError(
            f"Unknown provider '{provider}'. "
            f"Valid options: claude, gemini, openai/codex, claude_cli, gemini_cli"
        )
