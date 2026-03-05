"""PatchCache — persistent failure-signature → known-good patch store.

Caches successful healer patches keyed by failure hash (_failure_hash()).
On a cache hit, the patch is applied directly without any LLM call, making
repeat failure patterns instant to resolve.

Storage:
  .codegen_agent/patch_cache.json → {failure_hash: {file_path: content, ...}, ...}

Capacity: max 200 entries (oldest evicted when full).
Disabled at runtime by setting CODEGEN_PATCH_CACHE=0.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

_CACHE_FILENAME = ".codegen_agent/patch_cache.json"
_MAX_ENTRIES = 200


class PatchCache:
    """Thread-safe persistent LRU cache of failure_hash → {file_path: content}.

    Thread safety is provided by a single lock around mutations.  The async
    healer uses asyncio.to_thread for IO, so standard threading.Lock suffices.
    """

    def __init__(self, workspace: str) -> None:
        self._path = Path(workspace) / _CACHE_FILENAME
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, str]] = {}
        self._order: list[str] = []   # insertion order → oldest first
        self._loaded = False

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, failure_hash: str) -> Optional[dict[str, str]]:
        """Return the cached patch dict for *failure_hash*, or None on miss."""
        self._ensure_loaded()
        return self._data.get(failure_hash)

    def put(self, failure_hash: str, patch: dict[str, str]) -> None:
        """Store *patch* for *failure_hash*.  Evicts oldest entry when full."""
        if not patch:
            return
        self._ensure_loaded()
        with self._lock:
            if failure_hash in self._data:
                # Refresh position
                self._order.remove(failure_hash)
            elif len(self._data) >= _MAX_ENTRIES:
                oldest = self._order.pop(0)
                self._data.pop(oldest, None)
            self._data[failure_hash] = patch
            self._order.append(failure_hash)
            self._flush()

    @property
    def size(self) -> int:
        self._ensure_loaded()
        return len(self._data)

    # ── Private ────────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                if self._path.exists():
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._data = raw
                        self._order = list(raw.keys())
            except Exception:
                self._data = {}
                self._order = []
            self._loaded = True

    def _flush(self) -> None:
        """Write cache to disk.  Caller must hold _lock."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, indent=2), encoding="utf-8"
            )
        except Exception:
            pass  # non-fatal: cache write failure degrades gracefully
