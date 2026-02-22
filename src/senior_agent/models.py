from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class FailureType(str, Enum):
    BUILD_ERROR = "build_error"
    TEST_FAILURE = "test_failure"
    RUNTIME_EXCEPTION = "runtime_exception"
    PERF_REGRESSION = "perf_regression"
    LINT_TYPE_FAILURE = "lint_or_type_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CommandResult:
    command: str
    return_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def combined_output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


@dataclass(frozen=True)
class FailureContext:
    command_result: CommandResult
    failure_type: FailureType
    workspace: Path
    attempt_number: int


@dataclass(frozen=True)
class FixOutcome:
    """Result returned by a fix strategy for one healing attempt.

    Contract:
    - If ``applied`` is ``True`` and ``changed_files`` is non-empty, strategy must
      include rollback snapshots for those files in ``rollback_entries``.
    """

    applied: bool
    note: str = ""
    changed_files: tuple[Path, ...] = ()
    diff_summary: tuple[str, ...] = ()
    rollback_entries: tuple["FileRollback", ...] = ()


class FixStrategy(Protocol):
    """Contract for strategies that attempt a single automated fix."""

    name: str

    def apply(self, context: FailureContext) -> FixOutcome:
        """Apply one fix attempt and return a contract-compliant ``FixOutcome``."""
        ...


@dataclass(frozen=True)
class ImplementationPlan:
    """Structured, serializable plan for implementing a requested feature."""

    feature_name: str
    summary: str
    new_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    validation_commands: list[str] = field(default_factory=list)
    design_guidance: str = ""

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(
            {
                "feature_name": self.feature_name,
                "summary": self.summary,
                "new_files": list(self.new_files),
                "modified_files": list(self.modified_files),
                "steps": list(self.steps),
                "validation_commands": list(self.validation_commands),
                "design_guidance": self.design_guidance,
            },
            indent=indent,
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ImplementationPlan":
        if not isinstance(payload, dict):
            raise ValueError("Implementation plan payload must be an object.")

        feature_name = str(payload.get("feature_name", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        if not feature_name:
            raise ValueError("Implementation plan is missing required field: feature_name.")
        if not summary:
            raise ValueError("Implementation plan is missing required field: summary.")

        return cls(
            feature_name=feature_name,
            summary=summary,
            new_files=cls._coerce_string_list(payload.get("new_files")),
            modified_files=cls._coerce_string_list(payload.get("modified_files")),
            steps=cls._coerce_string_list(payload.get("steps")),
            validation_commands=cls._coerce_string_list(payload.get("validation_commands")),
            design_guidance=str(payload.get("design_guidance", "")).strip(),
        )

    @staticmethod
    def _coerce_string_list(raw_value: Any) -> list[str]:
        if not isinstance(raw_value, list):
            return []
        return [
            item.strip()
            for item in raw_value
            if isinstance(item, str) and item.strip()
        ]


@dataclass(frozen=True)
class AttemptRecord:
    attempt_number: int
    strategy_name: str
    failure_type: FailureType
    applied: bool
    note: str = ""
    changed_files: tuple[Path, ...] = ()
    diff_summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileRollback:
    """Snapshot required to restore a changed file after a failed verification."""

    path: Path
    existed_before: bool
    content: str | None = None


@dataclass(frozen=True)
class SessionReport:
    command: str
    initial_result: CommandResult
    final_result: CommandResult
    attempts: list[AttemptRecord] = field(default_factory=list)
    success: bool = False
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "initial_result": self._command_result_to_dict(self.initial_result),
            "final_result": self._command_result_to_dict(self.final_result),
            "attempts": [self._attempt_record_to_dict(attempt) for attempt in self.attempts],
            "success": self.success,
            "blocked_reason": self.blocked_reason,
        }

    def to_json(self, indent: int | None = 2) -> str:
        """Serialize report state for persistence or interrupted-session recovery."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionReport":
        attempts_payload = payload.get("attempts", [])
        attempts = [
            cls._attempt_record_from_dict(item)
            for item in attempts_payload
            if isinstance(item, dict)
        ]
        return cls(
            command=str(payload.get("command", "")),
            initial_result=cls._command_result_from_dict(payload.get("initial_result")),
            final_result=cls._command_result_from_dict(payload.get("final_result")),
            attempts=attempts,
            success=bool(payload.get("success", False)),
            blocked_reason=(
                None
                if payload.get("blocked_reason") is None
                else str(payload.get("blocked_reason"))
            ),
        )

    @classmethod
    def from_json(cls, raw_json: str) -> "SessionReport":
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            raise ValueError("Session report JSON must be an object.")
        return cls.from_dict(payload)

    @staticmethod
    def _command_result_to_dict(result: CommandResult) -> dict[str, Any]:
        return {
            "command": result.command,
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    @staticmethod
    def _command_result_from_dict(payload: Any) -> CommandResult:
        if not isinstance(payload, dict):
            return CommandResult(command="", return_code=1)
        return CommandResult(
            command=str(payload.get("command", "")),
            return_code=int(payload.get("return_code", 1)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
        )

    @staticmethod
    def _attempt_record_to_dict(attempt: AttemptRecord) -> dict[str, Any]:
        return {
            "attempt_number": attempt.attempt_number,
            "strategy_name": attempt.strategy_name,
            "failure_type": attempt.failure_type.value,
            "applied": attempt.applied,
            "note": attempt.note,
            "changed_files": [str(path) for path in attempt.changed_files],
            "diff_summary": list(attempt.diff_summary),
        }

    @classmethod
    def _attempt_record_from_dict(cls, payload: dict[str, Any]) -> AttemptRecord:
        failure_type = cls._failure_type_from_value(payload.get("failure_type"))
        changed_files_payload = payload.get("changed_files", [])
        diff_summary_payload = payload.get("diff_summary", [])
        return AttemptRecord(
            attempt_number=int(payload.get("attempt_number", 0)),
            strategy_name=str(payload.get("strategy_name", "")),
            failure_type=failure_type,
            applied=bool(payload.get("applied", False)),
            note=str(payload.get("note", "")),
            changed_files=tuple(
                Path(path)
                for path in changed_files_payload
                if isinstance(path, str) and path.strip()
            ),
            diff_summary=tuple(
                item
                for item in diff_summary_payload
                if isinstance(item, str)
            ),
        )

    @staticmethod
    def _failure_type_from_value(value: Any) -> FailureType:
        if isinstance(value, FailureType):
            return value
        if isinstance(value, str):
            try:
                return FailureType(value)
            except ValueError:
                return FailureType.UNKNOWN
        return FailureType.UNKNOWN
