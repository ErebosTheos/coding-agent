"""Cross-project failure pattern store.

Maps a stable failure fingerprint → the fix description that resolved it.
Persists to ~/.codegen_agent/patterns.json so knowledge compounds across projects.

Usage in healer:
    store = PatternStore()
    fp = store.fingerprint(failure_type, error_text)
    hint = store.lookup(fp)           # inject into prompt if found
    store.record(fp, "Fixed by ...")  # call after successful heal
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

_STORE_PATH = Path.home() / ".codegen_agent" / "patterns.json"
_MAX_PATTERNS = 300  # keep the most recent N entries

# Lines to skip when building a fingerprint (noise, not signal)
_SKIP_LINE_RE = re.compile(
    r"^\s*(Traceback|File \"|During handling|The above|$|#|\s+at )",
    re.IGNORECASE,
)


class PatternStore:
    """Thread-safe (cooperative async) cross-project pattern dictionary."""

    def __init__(self, store_path: Path = _STORE_PATH):
        self._path = store_path
        self._data: dict[str, dict] = self._load()

    # ── Public API ─────────────────────────────────────────────────────────────

    def fingerprint(self, failure_type: str, error_text: str) -> str:
        """Stable 16-char hex fingerprint of a failure.

        Uses the failure type plus the first informative non-boilerplate line
        so the same root cause maps to the same key across projects.
        """
        key_line = ""
        for line in (error_text or "").splitlines():
            stripped = line.strip()
            if stripped and not _SKIP_LINE_RE.match(line) and len(stripped) > 15:
                key_line = stripped[:300]
                break
        raw = f"{failure_type}:{key_line}"
        return hashlib.sha256(raw.encode(), usedforsecurity=False).hexdigest()[:16]

    def lookup(self, fingerprint: str) -> Optional[str]:
        """Return the stored fix description for this fingerprint, or None."""
        entry = self._data.get(fingerprint)
        return entry.get("fix") if entry else None

    def record(self, fingerprint: str, fix_description: str, file_path: str = "") -> None:
        """Record a successful fix.  Overwrites any existing entry for this fingerprint."""
        self._data[fingerprint] = {
            "fix": fix_description,
            "file": file_path,
            "ts": time.time(),
        }
        self._save()

    def known_patterns_prompt(self, fingerprints: list[str]) -> str:
        """Return a healer-prompt section listing known fixes for the given fingerprints.

        Returns an empty string if no matches — callers can safely concatenate it.
        """
        matches = [
            self._data[fp]["fix"]
            for fp in fingerprints
            if fp in self._data and self._data[fp].get("fix")
        ]
        if not matches:
            return ""
        lines = "\n".join(f"- {m}" for m in matches[:5])
        return f"\nKnown fixes from previous projects (apply if relevant):\n{lines}\n"

    def size(self) -> int:
        return len(self._data)

    # ── Private ────────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            return {}

    def _save(self) -> None:
        # Trim to _MAX_PATTERNS most-recent entries
        if len(self._data) > _MAX_PATTERNS:
            sorted_items = sorted(
                self._data.items(),
                key=lambda kv: kv[1].get("ts", 0),
                reverse=True,
            )
            self._data = dict(sorted_items[:_MAX_PATTERNS])
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, indent=2), encoding="utf-8"
            )
        except Exception:
            pass  # persistence is best-effort; never crash the healer
