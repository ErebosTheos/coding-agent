import hashlib
import json
from pathlib import Path
from typing import Optional


class LLMCache:
    """File-based LLM response cache.

    Cache layout: <cache_dir>/<first-2-hex>/<full-sha256>.json
    Matches git object storage convention for balanced directory fan-out.
    """

    def __init__(self, cache_dir: str):
        self._root = Path(cache_dir)

    def _key_path(self, prompt: str, provider: str, model: Optional[str]) -> Path:
        raw = f"{provider}:{model or ''}:{prompt}"
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return self._root / digest[:2] / f"{digest}.json"

    def get(self, prompt: str, provider: str, model: Optional[str]) -> Optional[str]:
        path = self._key_path(prompt, provider, model)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text()).get("response")
        except Exception:
            return None

    def set(self, prompt: str, provider: str, model: Optional[str], response: str) -> None:
        path = self._key_path(prompt, provider, model)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"response": response}))
