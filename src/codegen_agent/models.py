from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Optional, Any

class FailureType(Enum):
    BUILD_ERROR = "BUILD_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    RUNTIME_EXCEPTION = "RUNTIME_EXCEPTION"
    PERF_REGRESSION = "PERF_REGRESSION"
    LINT_TYPE_FAILURE = "LINT_TYPE_FAILURE"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class FileRollback:
    path: str
    existed_before: bool
    content: Optional[str] = None

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    description: str
    priority: int = 1

@dataclass(frozen=True)
class Plan:
    project_name: str
    tech_stack: str
    features: List[Feature]
    entry_point: str
    test_strategy: str

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class Contract:
    purpose: str
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    public_api: List[str] = field(default_factory=list)
    invariants: List[str] = field(default_factory=list)

@dataclass(frozen=True)
class ExecutionNode:
    node_id: str
    file_path: str
    purpose: str
    depends_on: List[str] = field(default_factory=list)
    contract: Optional[Contract] = None

@dataclass(frozen=True)
class Architecture:
    file_tree: List[str]
    nodes: List[ExecutionNode]
    global_validation_commands: List[str]

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class GeneratedFile:
    file_path: str
    content: str
    node_id: str
    sha256: str

@dataclass(frozen=True)
class ExecutionResult:
    generated_files: List[GeneratedFile]
    skipped_nodes: List[str] = field(default_factory=list)
    failed_nodes: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class TestSuite:
    test_files: Dict[str, str]  # path -> content
    validation_commands: List[str]
    framework: str

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class HealAttempt:
    attempt_number: int
    failure_type: FailureType
    fix_applied: str
    changed_files: List[str]
    note: Optional[str] = None

@dataclass(frozen=True)
class HealingReport:
    success: bool
    attempts: List[HealAttempt]
    final_command_result: Optional[CommandResult] = None
    blocked_reason: Optional[str] = None

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class QAReport:
    score: float  # 0-100
    issues: List[str]
    suggestions: List[str]
    approved: bool

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class VisualAuditResult:
    passed: bool
    visual_bugs: List[str]
    suggested_css_fixes: str
    screenshot_path: Optional[str] = None

    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class StageTrace:
    stage: str
    provider: str
    model: Optional[str]
    start_monotonic: float
    end_monotonic: float
    duration_seconds: float
    start_unix_ts: float
    end_unix_ts: float
    prompt_chars: int
    response_chars: int
    retries: int = 0
    fallback_used: bool = False
    fallback_reason: Optional[str] = None

@dataclass(frozen=True)
class PipelineReport:
    prompt: str
    plan: Optional[Plan] = None
    architecture: Optional[Architecture] = None
    execution_result: Optional[ExecutionResult] = None
    dependency_resolution: Optional[Any] = None # New stage
    test_suite: Optional[TestSuite] = None
    healing_report: Optional[HealingReport] = None
    qa_report: Optional[QAReport] = None
    visual_audit: Optional[VisualAuditResult] = None
    wall_clock_seconds: float = 0.0
    stage_traces: List["StageTrace"] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)
