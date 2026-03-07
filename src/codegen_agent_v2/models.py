"""V2 data model types — all frozen dataclasses."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


# ── Manifest (source of truth) ─────────────────────────────────────────────

@dataclass(frozen=True)
class ManifestAuth:
    sub_field: str          # "email" | "username" | "id"
    login_endpoint: str     # "/api/v1/auth/login"
    token_type: str = "bearer"


@dataclass(frozen=True)
class ManifestModel:
    class_name: str
    table_name: str
    columns: dict[str, str]  # col_name → "Integer, primary_key=True"


@dataclass(frozen=True)
class ManifestSchema:
    class_name: str
    fields: dict[str, str]   # field_name → Python type str


@dataclass(frozen=True)
class ManifestRoute:
    method: str
    path: str
    auth_required: bool
    summary: str = ""


@dataclass(frozen=True)
class ProjectManifest:
    """Single source of truth for the whole project.

    Generated during planning, updated from disk after Layer 2 (models),
    and injected into every subsequent LLM call to prevent cross-file drift.
    """
    project_name: str
    stack: str
    api_prefix: str                    # "/api/v1"
    auth: ManifestAuth
    models: dict[str, ManifestModel]   # class_name → ManifestModel
    schemas: dict[str, ManifestSchema] # class_name → ManifestSchema
    routes: list[ManifestRoute]
    db_url_default: str = "sqlite+aiosqlite:///./app.db"
    accessibility_required: bool = False
    modules: list[str] = field(default_factory=list)


# ── Planning ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FileSpec:
    file_path: str
    purpose: str
    layer: int
    exports: list[str]
    depends_on: list[str] = field(default_factory=list)
    priority: str = "medium"   # "low" | "medium" | "high"


@dataclass(frozen=True)
class LayeredPlan:
    manifest: ProjectManifest
    files: list[FileSpec]
    validation_commands: list[str]


# ── Execution ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GeneratedFile:
    file_path: str
    content: str
    layer: int
    lines: int
    sha256: str


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    issues: list[str]         # syntax errors, stub names, etc.
    retry_reason: str = ""


@dataclass(frozen=True)
class LayerResult:
    layer: int
    name: str
    status: str               # "passed" | "failed" | "needs_review"
    files: list[GeneratedFile]
    duration_s: float
    heal_rounds: int
    errors: list[str]


@dataclass(frozen=True)
class PipelineResult:
    project_id: str
    status: str               # "COMPLETE" | "LAYER_FAILED" | "NEEDS_REVIEW" | "FAILED"
    layers: list[LayerResult]
    qa_score: float
    files_created: int
    duration_s: float
    cost_usd: float
    manifest: ProjectManifest | None = None


# ── Layer definitions ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class LayerDef:
    index: int
    name: str
    on_failure: str           # "hard_stop" | "needs_review"
    max_heal_rounds: int = 3


LAYER_DEFS: list[LayerDef] = [
    LayerDef(1, "Foundation",        "hard_stop"),
    LayerDef(2, "Models",            "hard_stop"),
    LayerDef(3, "Schemas",           "hard_stop"),
    LayerDef(4, "Core & Services",   "hard_stop"),
    LayerDef(5, "API & Entry",       "hard_stop"),
    LayerDef(6, "Frontend & Tests",  "needs_review"),
]


# ── Exceptions ────────────────────────────────────────────────────────────

class LayerGateError(Exception):
    def __init__(self, layer: int, name: str, errors: list[str]) -> None:
        self.layer = layer
        self.name = name
        self.errors = errors
        super().__init__(f"Layer {layer} ({name}) hard-stop: {errors[0] if errors else 'unknown'}")


class NeedsReviewError(Exception):
    def __init__(self, layer: int, name: str, errors: list[str]) -> None:
        self.layer = layer
        self.name = name
        self.errors = errors
        super().__init__(f"Layer {layer} ({name}) needs review: {errors[0] if errors else 'unknown'}")


# ── SSE events ────────────────────────────────────────────────────────────

@dataclass
class PipelineEvent:
    type: str
    data: dict[str, Any]
