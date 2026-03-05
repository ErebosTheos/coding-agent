"""Dashboard configuration — defaults with env-var overrides.

All settings can also be overridden by a config.yaml placed in the project
root.  Environment variables always win over the YAML file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


@dataclass
class WorkerConfig:
    global_max: int = 4
    per_project_max: int = 2


@dataclass
class BuildConfig:
    batch_size: int = 5
    bug_fix_passes: int = 2


@dataclass
class SafetyConfig:
    blocklist: list[str] = field(default_factory=lambda: ["*.env", "*.key", "*.pem", "*.secret"])
    max_lines_per_fix: int = 500


@dataclass
class WatcherConfig:
    debounce_seconds: float = 5.0


@dataclass
class DashboardConfig:
    workers: WorkerConfig = field(default_factory=WorkerConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)


def _load(config_path: str | None = None) -> DashboardConfig:
    """Load config from optional YAML, then apply env-var overrides."""
    raw: dict = {}

    # 1. Try to load YAML
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    candidates.extend([
        Path(os.environ.get("CODEGEN_DASHBOARD_CONFIG", "dashboard_config.yaml")),
        Path("config.yaml"),
    ])
    for p in candidates:
        if p.exists() and _HAS_YAML:
            try:
                with open(p) as fh:
                    raw = _yaml.safe_load(fh) or {}
                break
            except Exception:
                pass

    def _i(section: str, key: str, default: int) -> int:
        env_key = f"CODEGEN_{section.upper()}_{key.upper()}"
        if env_key in os.environ:
            return int(os.environ[env_key])
        return int((raw.get(section) or {}).get(key, default))

    def _f(section: str, key: str, default: float) -> float:
        env_key = f"CODEGEN_{section.upper()}_{key.upper()}"
        if env_key in os.environ:
            return float(os.environ[env_key])
        return float((raw.get(section) or {}).get(key, default))

    return DashboardConfig(
        workers=WorkerConfig(
            global_max=_i("workers", "global_max", 4),
            per_project_max=_i("workers", "per_project_max", 2),
        ),
        build=BuildConfig(
            batch_size=_i("build", "batch_size", 5),
            bug_fix_passes=_i("build", "bug_fix_passes", 2),
        ),
        safety=SafetyConfig(
            blocklist=(raw.get("safety") or {}).get(
                "blocklist", ["*.env", "*.key", "*.pem", "*.secret"]
            ),
            max_lines_per_fix=_i("safety", "max_lines_per_fix", 500),
        ),
        watcher=WatcherConfig(
            debounce_seconds=_f("watcher", "debounce_seconds", 5.0),
        ),
    )


cfg: DashboardConfig = _load()
