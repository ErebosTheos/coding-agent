import json
import os
import asyncio
import random
from pathlib import Path
from typing import Dict, Any, Optional
from .protocol import LLMClient, LLMTimeoutError, LLMError
from .gemini_cli import GeminiCLIClient
from .claude_cli import ClaudeCLIClient
from .anthropic_api import AnthropicAPIClient
from .openai_api import OpenAIClient
from .codex_cli import CodexCLIClient
from .cache import LLMCache
from .caching_client import CachingLLMClient

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


class _RetryingLLMClient:
    """Wraps any LLMClient with transient-error retry and role fallback.

    Retry policy (same as execute_with_retry):
    - Up to max_retries retries for LLMTimeoutError and empty-output LLMError.
    - Jittered exponential backoff between retries.
    - One fallback attempt after primary retries are exhausted.
    - astream() is passed through without retry (streams are not idempotent).
    """

    def __init__(
        self,
        primary: LLMClient,
        role: str,
        fallback: Optional[LLMClient] = None,
        max_retries: int = 2,
        call_primary: Optional[LLMClient] = None,
        call_fallback: Optional[LLMClient] = None,
    ):
        # Keep raw clients for introspection/debugging.
        self._primary = primary
        self._role = role
        self._fallback = fallback
        # Runtime call clients may include wrappers (cache, tracing, etc.).
        self._primary_client = call_primary or primary
        self._fallback_client = call_fallback or fallback
        self._max_retries = max_retries

    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._primary_client.generate(prompt, system_prompt=system_prompt)
                if not response or not response.strip():
                    raise LLMError(f"Empty response from role '{self._role}' on attempt {attempt + 1}")
                return response
            except LLMTimeoutError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = (2 ** attempt) * 0.5 + random.uniform(0, 0.5)
                    await asyncio.sleep(delay)
            except LLMError as exc:
                last_exc = exc
                is_empty = "empty response" in str(exc).lower()
                if is_empty and attempt < self._max_retries:
                    delay = (2 ** attempt) * 0.5 + random.uniform(0, 0.5)
                    await asyncio.sleep(delay)
                else:
                    break

        if self._fallback_client is not None:
            try:
                response = await self._fallback_client.generate(prompt, system_prompt=system_prompt)
                if response and response.strip():
                    return response
            except Exception:
                pass

        raise LLMError(
            f"Role '{self._role}' failed after {self._max_retries} retries: {last_exc}"
        ) from last_exc

    async def astream(self, prompt: str, system_prompt: str = ""):
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            yielded_any = False
            try:
                async for chunk in self._primary_client.astream(prompt, system_prompt=system_prompt):
                    yielded_any = True
                    yield chunk
                return
            except (LLMTimeoutError, LLMError) as exc:
                last_exc = exc
                if yielded_any:
                    raise  # mid-stream failure — can't safely retry
                if attempt == 0:
                    await asyncio.sleep(1.0)
        if self._fallback_client is not None:
            try:
                async for chunk in self._fallback_client.astream(prompt, system_prompt=system_prompt):
                    yield chunk
                return
            except Exception:
                pass
        raise LLMError(
            f"Role '{self._role}' astream failed after retry: {last_exc}"
        ) from last_exc


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

        use_cache = os.environ.get("CODEGEN_CACHE", "").strip() == "1"
        client_key = f"{role}:{provider}:{model}:{'c' if use_cache else 'r'}"

        if client_key not in self._clients:
            raw = self._create_client(provider, model)
            fallback_raw = self._get_fallback_client(role)
            if use_cache:
                cache_dir = os.path.join(".codegen_agent", "llm_cache")
                cache = LLMCache(cache_dir)
                call_client: LLMClient = CachingLLMClient(raw, cache, provider, model)
            else:
                call_client = raw
            client = _RetryingLLMClient(
                primary=raw,
                role=role,
                fallback=fallback_raw,
                call_primary=call_client,
                call_fallback=fallback_raw,
            )
            self._clients[client_key] = client

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

    def _get_fallback_client(self, role: str) -> Optional[LLMClient]:
        env_prov = f"CODEGEN_{role.upper()}_FALLBACK_PROVIDER"
        env_model = f"CODEGEN_{role.upper()}_FALLBACK_MODEL"
        prov_raw = os.environ.get(env_prov, "").lower()
        if not prov_raw:
            return None
        provider = _PROVIDER_ALIASES.get(prov_raw, prov_raw)
        model = os.environ.get(env_model) or _DEFAULT_MODELS.get(provider)
        return self._create_client(provider, model)

    async def execute_with_retry(
        self,
        role: str,
        prompt: str,
        system_prompt: str = "",
    ) -> tuple[str, int, bool, Optional[str]]:
        """Backward-compatible wrapper — retry is now handled by _RetryingLLMClient."""
        client = self.get_client_for_role(role)
        response = await client.generate(prompt, system_prompt=system_prompt)
        return response, 0, False, None
