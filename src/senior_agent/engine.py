from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
import logging
import pickle
import random
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from senior_agent.classifier import classify_failure
from senior_agent.llm_client import (
    CodexCLIClient,
    GeminiCLIClient,
    LocalOffloadClient,
    MultiCloudRouter,
)
from senior_agent.models import (
    AttemptRecord,
    CommandResult,
    FailureContext,
    FailureType,
    FileRollback,
    FixStrategy,
    SessionReport,
)
from senior_agent.strategies import LLMStrategy
from senior_agent.utils import is_within_workspace

Executor = Callable[[str, Path], CommandResult]
Classifier = Callable[[str, str, str], FailureType]
logger = logging.getLogger(__name__)
CHECKPOINT_SCHEMA_VERSION = 1
_CHECKPOINT_BINARY_MAGIC = b"SENIOR_AGENT_CHECKPOINT_BIN_V1\n"


def run_shell_command(command: str, workspace: Path) -> CommandResult:
    logger.info("Executing command: %s (cwd=%s)", command, workspace)
    completed = subprocess.run(
        command,
        shell=True,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        command=command,
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


class SeniorAgent:
    """Run a bounded autonomous recovery loop for a failing command."""

    def __init__(
        self,
        max_attempts: int = 3,
        classifier: Classifier = classify_failure,
        executor: Executor = run_shell_command,
        default_strategies: Sequence[FixStrategy] | None = None,
        default_validation_commands: Sequence[str] | None = None,
        retry_backoff_base_seconds: float = 0.0,
        retry_backoff_max_seconds: float = 30.0,
        retry_backoff_jitter_seconds: float = 0.0,
        adaptive_strategy_ordering: bool = False,
        enable_verification_cache: bool = True,
        checkpoint_serialization_mode: str = "json",
        sleep_func: Callable[[float], None] = time.sleep,
        random_func: Callable[[], float] = random.random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if retry_backoff_base_seconds < 0:
            raise ValueError("retry_backoff_base_seconds must be >= 0")
        if retry_backoff_max_seconds < 0:
            raise ValueError("retry_backoff_max_seconds must be >= 0")
        if retry_backoff_jitter_seconds < 0:
            raise ValueError("retry_backoff_jitter_seconds must be >= 0")
        if retry_backoff_base_seconds > 0 and retry_backoff_max_seconds <= 0:
            raise ValueError(
                "retry_backoff_max_seconds must be > 0 when retry_backoff_base_seconds is enabled"
            )
        if retry_backoff_base_seconds > retry_backoff_max_seconds:
            raise ValueError(
                "retry_backoff_base_seconds must be <= retry_backoff_max_seconds"
            )
        self.max_attempts = max_attempts
        self.classifier = classifier
        self.executor = executor
        self.default_strategies = tuple(default_strategies or ())
        self.default_validation_commands = tuple(default_validation_commands or ())
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.retry_backoff_max_seconds = retry_backoff_max_seconds
        self.retry_backoff_jitter_seconds = retry_backoff_jitter_seconds
        self.adaptive_strategy_ordering = adaptive_strategy_ordering
        self.enable_verification_cache = enable_verification_cache
        mode = checkpoint_serialization_mode.strip().lower()
        if mode not in {"json", "binary", "ab"}:
            raise ValueError("checkpoint_serialization_mode must be one of: json, binary, ab.")
        self.checkpoint_serialization_mode = mode
        self.sleep_func = sleep_func
        self.random_func = random_func

    def heal(
        self,
        command: str,
        strategies: Sequence[FixStrategy] | None = None,
        workspace: str | Path = ".",
        validation_commands: Sequence[str] | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> SessionReport:
        """Execute, attempt fixes, and verify with optional validation commands."""

        workspace_path = Path(workspace).resolve()
        checkpoint_file = self._resolve_checkpoint_path(
            workspace=workspace_path,
            checkpoint_path=checkpoint_path,
            require_existing=False,
        )
        active_strategies = (
            tuple(strategies) if strategies is not None else self.default_strategies
        )
        active_validation_commands = (
            tuple(validation_commands)
            if validation_commands is not None
            else self.default_validation_commands
        )
        checkpoint_metadata = self._build_checkpoint_metadata(
            workspace=workspace_path,
            strategies=active_strategies,
            validation_commands=active_validation_commands,
        )
        logger.info(
            "Starting senior-agent session: command=%s workspace=%s strategies=%s validations=%s max_attempts=%s",
            command,
            workspace_path,
            len(active_strategies),
            len(active_validation_commands),
            self.max_attempts,
        )
        verification_cache: dict[tuple[int, str], CommandResult] = {}
        verification_epoch = 0
        initial_success, initial_result = self._run_verification(
            command=command,
            workspace=workspace_path,
            validation_commands=active_validation_commands,
            verification_cache=verification_cache,
            verification_epoch=verification_epoch,
        )
        if initial_success:
            logger.info("Initial command succeeded; no healing attempts required.")
            return self._finalize_report(
                command=command,
                initial_result=initial_result,
                final_result=initial_result,
                attempts=[],
                success=True,
                blocked_reason=None,
                checkpoint_path=checkpoint_file,
                checkpoint_metadata=checkpoint_metadata,
            )

        self._checkpoint_progress(
            command=command,
            initial_result=initial_result,
            final_result=initial_result,
            attempts=[],
            checkpoint_path=checkpoint_file,
            checkpoint_metadata=checkpoint_metadata,
        )
        return self._heal_from_state(
            command=command,
            initial_result=initial_result,
            final_result=initial_result,
            attempts=[],
            start_attempt_number=1,
            active_strategies=active_strategies,
            workspace_path=workspace_path,
            active_validation_commands=active_validation_commands,
            verification_cache=verification_cache,
            verification_epoch=verification_epoch,
            checkpoint_path=checkpoint_file,
            checkpoint_metadata=checkpoint_metadata,
        )

    def resume(
        self,
        checkpoint_path: str | Path,
        strategies: Sequence[FixStrategy] | None = None,
        workspace: str | Path = ".",
        validation_commands: Sequence[str] | None = None,
    ) -> SessionReport:
        """Resume a previously persisted heal session from a checkpoint file."""

        workspace_path = Path(workspace).resolve()
        checkpoint_file = self._resolve_checkpoint_path(
            workspace=workspace_path,
            checkpoint_path=checkpoint_path,
            require_existing=True,
        )
        persisted_report, persisted_metadata = self._load_checkpoint(checkpoint_file)
        active_strategies = (
            tuple(strategies) if strategies is not None else self.default_strategies
        )
        active_validation_commands = (
            tuple(validation_commands)
            if validation_commands is not None
            else self.default_validation_commands
        )
        checkpoint_metadata = self._build_checkpoint_metadata(
            workspace=workspace_path,
            strategies=active_strategies,
            validation_commands=active_validation_commands,
        )
        if persisted_report.success:
            self._validate_checkpoint_metadata(
                checkpoint_path=checkpoint_file,
                metadata=persisted_metadata,
                expected_metadata=checkpoint_metadata,
            )
            logger.info("Loaded successful checkpoint; no resume work needed.")
            return persisted_report
        if not persisted_report.command:
            raise ValueError("Checkpoint does not contain a resumable command.")

        self._validate_checkpoint_metadata(
            checkpoint_path=checkpoint_file,
            metadata=persisted_metadata,
            expected_metadata=checkpoint_metadata,
        )
        logger.info(
            "Resuming senior-agent session: command=%s workspace=%s prior_attempts=%s",
            persisted_report.command,
            workspace_path,
            len(persisted_report.attempts),
        )
        verification_cache: dict[tuple[int, str], CommandResult] = {}
        return self._heal_from_state(
            command=persisted_report.command,
            initial_result=persisted_report.initial_result,
            final_result=persisted_report.final_result,
            attempts=list(persisted_report.attempts),
            start_attempt_number=len(persisted_report.attempts) + 1,
            active_strategies=active_strategies,
            workspace_path=workspace_path,
            active_validation_commands=active_validation_commands,
            verification_cache=verification_cache,
            verification_epoch=0,
            checkpoint_path=checkpoint_file,
            checkpoint_metadata=checkpoint_metadata,
        )

    def _heal_from_state(
        self,
        *,
        command: str,
        initial_result: CommandResult,
        final_result: CommandResult,
        attempts: list[AttemptRecord],
        start_attempt_number: int,
        active_strategies: Sequence[FixStrategy],
        workspace_path: Path,
        active_validation_commands: Sequence[str],
        verification_cache: dict[tuple[int, str], CommandResult],
        verification_epoch: int,
        checkpoint_path: Path | None,
        checkpoint_metadata: dict[str, str | int],
    ) -> SessionReport:
        if not active_strategies:
            logger.warning("Command failed but no strategies were configured.")
            return self._finalize_report(
                command=command,
                initial_result=initial_result,
                final_result=final_result,
                attempts=attempts,
                success=False,
                blocked_reason="No fix strategies configured.",
                checkpoint_path=checkpoint_path,
                checkpoint_metadata=checkpoint_metadata,
            )

        max_attempts = self.max_attempts
        if start_attempt_number > max_attempts:
            blocked_reason = (
                f"Reached max attempts ({max_attempts}) without a successful verification."
            )
            logger.warning(blocked_reason)
            return self._finalize_report(
                command=command,
                initial_result=initial_result,
                final_result=final_result,
                attempts=attempts,
                success=False,
                blocked_reason=blocked_reason,
                checkpoint_path=checkpoint_path,
                checkpoint_metadata=checkpoint_metadata,
            )

        for attempt_number in range(start_attempt_number, max_attempts + 1):
            failure_type = self.classifier(
                final_result.command,
                final_result.stdout,
                final_result.stderr,
            )
            ordered_strategies = (
                self._order_strategies_for_failure(
                    strategies=active_strategies,
                    failure_type=failure_type,
                )
                if self.adaptive_strategy_ordering
                else tuple(active_strategies)
            )
            strategy_index = min(attempt_number - 1, len(ordered_strategies) - 1)
            strategy = ordered_strategies[strategy_index]
            context = FailureContext(
                command_result=final_result,
                failure_type=failure_type,
                workspace=workspace_path,
                attempt_number=attempt_number,
            )
            try:
                outcome = strategy.apply(context)
            except Exception as exc:  # pragma: no cover - defensive guardrail
                strategy_name = str(getattr(strategy, "name", strategy.__class__.__name__))
                logger.exception(
                    "Strategy apply failed: attempt=%s strategy=%s",
                    attempt_number,
                    strategy_name,
                )
                attempts.append(
                    AttemptRecord(
                        attempt_number=attempt_number,
                        strategy_name=strategy_name,
                        failure_type=failure_type,
                        applied=False,
                        note=f"Strategy raised exception: {exc}",
                        changed_files=(),
                    )
                )
                self._checkpoint_progress(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )
                self._sleep_before_next_attempt(
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                )
                continue
            for changed_file in outcome.changed_files:
                if not is_within_workspace(workspace_path, changed_file):
                    blocked_reason = (
                        "Blocked strategy due to out-of-repo modification attempt: "
                        f"{strategy.name} -> {changed_file}"
                    )
                    logger.error(blocked_reason)
                    attempts.append(
                        AttemptRecord(
                            attempt_number=attempt_number,
                            strategy_name=strategy.name,
                            failure_type=failure_type,
                            applied=False,
                            note=blocked_reason,
                            changed_files=(),
                        )
                    )
                    self._checkpoint_progress(
                        command=command,
                        initial_result=initial_result,
                        final_result=final_result,
                        attempts=attempts,
                        checkpoint_path=checkpoint_path,
                        checkpoint_metadata=checkpoint_metadata,
                    )
                    return self._finalize_report(
                        command=command,
                        initial_result=initial_result,
                        final_result=final_result,
                        attempts=attempts,
                        success=False,
                        blocked_reason=blocked_reason,
                        checkpoint_path=checkpoint_path,
                        checkpoint_metadata=checkpoint_metadata,
                    )

            rollback_contract_error = self._validate_rollback_contract(
                workspace=workspace_path,
                changed_files=outcome.changed_files,
                rollback_entries=outcome.rollback_entries,
            )
            if rollback_contract_error is not None:
                logger.error(rollback_contract_error)
                attempts.append(
                    AttemptRecord(
                        attempt_number=attempt_number,
                        strategy_name=strategy.name,
                        failure_type=failure_type,
                        applied=False,
                        note=rollback_contract_error,
                        changed_files=(),
                    )
                )
                self._checkpoint_progress(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )
                return self._finalize_report(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    success=False,
                    blocked_reason=rollback_contract_error,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )

            logger.info(
                "Attempt %s strategy=%s applied=%s note=%s",
                attempt_number,
                strategy.name,
                outcome.applied,
                outcome.note,
            )

            if not outcome.applied:
                attempts.append(
                    AttemptRecord(
                        attempt_number=attempt_number,
                        strategy_name=strategy.name,
                        failure_type=failure_type,
                        applied=outcome.applied,
                        note=outcome.note,
                        changed_files=outcome.changed_files,
                        diff_summary=outcome.diff_summary,
                    )
                )
                self._checkpoint_progress(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )
                self._sleep_before_next_attempt(
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                )
                continue

            verification_epoch += 1
            verification_success, verification_result = self._run_verification(
                command=command,
                workspace=workspace_path,
                validation_commands=active_validation_commands,
                verification_cache=verification_cache,
                verification_epoch=verification_epoch,
            )
            final_result = verification_result
            if verification_success:
                attempts.append(
                    AttemptRecord(
                        attempt_number=attempt_number,
                        strategy_name=strategy.name,
                        failure_type=failure_type,
                        applied=outcome.applied,
                        note=outcome.note,
                        changed_files=outcome.changed_files,
                        diff_summary=outcome.diff_summary,
                    )
                )
                self._checkpoint_progress(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )
                logger.info("Verification succeeded after attempt %s.", attempt_number)
                return self._finalize_report(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    success=True,
                    blocked_reason=None,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )

            rollback_success, rollback_note = self._rollback_changes(
                workspace=workspace_path,
                rollback_entries=outcome.rollback_entries,
            )
            attempt_note = f"{outcome.note} Verification failed. {rollback_note}"
            attempts.append(
                AttemptRecord(
                    attempt_number=attempt_number,
                    strategy_name=strategy.name,
                    failure_type=failure_type,
                    applied=outcome.applied,
                    note=attempt_note,
                    changed_files=outcome.changed_files,
                    diff_summary=outcome.diff_summary,
                )
            )
            self._checkpoint_progress(
                command=command,
                initial_result=initial_result,
                final_result=final_result,
                attempts=attempts,
                checkpoint_path=checkpoint_path,
                checkpoint_metadata=checkpoint_metadata,
            )

            if not rollback_success:
                logger.error("Rollback failed after verification failure: %s", rollback_note)
                return self._finalize_report(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    success=False,
                    blocked_reason=rollback_note,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )

            verification_epoch += 1
            post_rollback_success, post_rollback_result = self._run_verification(
                command=command,
                workspace=workspace_path,
                validation_commands=active_validation_commands,
                verification_cache=verification_cache,
                verification_epoch=verification_epoch,
            )
            final_result = post_rollback_result
            if post_rollback_success:
                logger.info(
                    "Rollback restored a passing state after failed verification on attempt %s.",
                    attempt_number,
                )
                return self._finalize_report(
                    command=command,
                    initial_result=initial_result,
                    final_result=final_result,
                    attempts=attempts,
                    success=True,
                    blocked_reason=None,
                    checkpoint_path=checkpoint_path,
                    checkpoint_metadata=checkpoint_metadata,
                )
            self._sleep_before_next_attempt(
                attempt_number=attempt_number,
                max_attempts=max_attempts,
            )

        blocked_reason = (
            f"Reached max attempts ({max_attempts}) without a successful verification."
        )
        logger.warning(blocked_reason)
        return self._finalize_report(
            command=command,
            initial_result=initial_result,
            final_result=final_result,
            attempts=attempts,
            success=False,
            blocked_reason=blocked_reason,
            checkpoint_path=checkpoint_path,
            checkpoint_metadata=checkpoint_metadata,
        )

    @staticmethod
    def _build_report(
        *,
        command: str,
        initial_result: CommandResult,
        final_result: CommandResult,
        attempts: Sequence[AttemptRecord],
        success: bool,
        blocked_reason: str | None,
    ) -> SessionReport:
        return SessionReport(
            command=command,
            initial_result=initial_result,
            final_result=final_result,
            attempts=list(attempts),
            success=success,
            blocked_reason=blocked_reason,
        )

    def _checkpoint_progress(
        self,
        *,
        command: str,
        initial_result: CommandResult,
        final_result: CommandResult,
        attempts: Sequence[AttemptRecord],
        checkpoint_path: Path | None,
        checkpoint_metadata: dict[str, str | int],
    ) -> None:
        if checkpoint_path is None:
            return
        report = self._build_report(
            command=command,
            initial_result=initial_result,
            final_result=final_result,
            attempts=attempts,
            success=False,
            blocked_reason=None,
        )
        self._persist_checkpoint(
            report,
            checkpoint_path,
            checkpoint_metadata,
            checkpoint_serialization_mode=self.checkpoint_serialization_mode,
        )

    def _finalize_report(
        self,
        *,
        command: str,
        initial_result: CommandResult,
        final_result: CommandResult,
        attempts: Sequence[AttemptRecord],
        success: bool,
        blocked_reason: str | None,
        checkpoint_path: Path | None,
        checkpoint_metadata: dict[str, str | int],
    ) -> SessionReport:
        report = self._build_report(
            command=command,
            initial_result=initial_result,
            final_result=final_result,
            attempts=attempts,
            success=success,
            blocked_reason=blocked_reason,
        )
        self._persist_checkpoint(
            report,
            checkpoint_path,
            checkpoint_metadata,
            checkpoint_serialization_mode=self.checkpoint_serialization_mode,
        )
        return report

    def _build_checkpoint_metadata(
        self,
        workspace: Path,
        strategies: Sequence[FixStrategy],
        validation_commands: Sequence[str],
    ) -> dict[str, str | int]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "workspace": str(workspace.resolve()),
            "strategy_fingerprint": SeniorAgent._build_strategy_fingerprint(strategies),
            "validation_fingerprint": SeniorAgent._build_validation_fingerprint(
                validation_commands
            ),
            "checkpoint_format": self.checkpoint_serialization_mode,
        }

    @staticmethod
    def _build_strategy_fingerprint(strategies: Sequence[FixStrategy]) -> str:
        hasher = hashlib.sha256()
        for index, strategy in enumerate(strategies):
            strategy_type = (
                f"{strategy.__class__.__module__}.{strategy.__class__.__qualname__}"
            )
            strategy_name = str(getattr(strategy, "name", strategy.__class__.__name__))
            payload = {
                "index": index,
                "type": strategy_type,
                "name": strategy_name,
                "config": SeniorAgent._normalize_fingerprint_value(strategy),
            }
            fingerprint_entry = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            hasher.update(fingerprint_entry.encode("utf-8"))
            hasher.update(b"\n")
        return hasher.hexdigest()

    @staticmethod
    def _normalize_fingerprint_value(value: object) -> object:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Path):
            return {"__path__": str(value)}
        if isinstance(value, Enum):
            enum_type = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
            return {"__enum__": enum_type, "value": value.value}
        if isinstance(value, bytes):
            return {"__bytes__": value.hex()}
        if callable(value):
            module = getattr(value, "__module__", value.__class__.__module__)
            qualname = getattr(value, "__qualname__", value.__class__.__qualname__)
            return {"__callable__": f"{module}.{qualname}"}
        if isinstance(value, dict):
            normalized_items: dict[str, object] = {}
            for key in sorted(value.keys(), key=lambda item: str(item)):
                normalized_items[str(key)] = SeniorAgent._normalize_fingerprint_value(
                    value[key]
                )
            return normalized_items
        if isinstance(value, (list, tuple)):
            return [
                SeniorAgent._normalize_fingerprint_value(item) for item in value
            ]
        if isinstance(value, set):
            normalized_items = [
                SeniorAgent._normalize_fingerprint_value(item) for item in value
            ]
            return sorted(
                normalized_items,
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
            )
        if is_dataclass(value):
            dataclass_params = getattr(value, "__dataclass_params__", None)
            is_frozen_dataclass = bool(
                dataclass_params is not None and dataclass_params.frozen
            )
            dataclass_fields: dict[str, object] = {}
            for field in fields(value):
                if field.name.startswith("_"):
                    continue
                field_value = getattr(value, field.name)
                if (
                    not is_frozen_dataclass
                    and isinstance(field_value, (list, dict, set))
                ):
                    continue
                dataclass_fields[field.name] = SeniorAgent._normalize_fingerprint_value(
                    field_value
                )
            dataclass_type = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
            return {"__dataclass__": dataclass_type, "fields": dataclass_fields}
        if hasattr(value, "__dict__"):
            attrs = {
                name: SeniorAgent._normalize_fingerprint_value(attr_value)
                for name, attr_value in sorted(vars(value).items())
                if (
                    not name.startswith("_")
                    and not isinstance(attr_value, (list, dict, set))
                )
            }
            if attrs:
                object_type = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
                return {"__object__": object_type, "attrs": attrs}
        fallback_type = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
        return {"__type__": fallback_type}

    @staticmethod
    def _build_validation_fingerprint(validation_commands: Sequence[str]) -> str:
        hasher = hashlib.sha256()
        for index, command in enumerate(validation_commands):
            fingerprint_entry = f"{index}:{command}\n"
            hasher.update(fingerprint_entry.encode("utf-8"))
        return hasher.hexdigest()

    @staticmethod
    def _validate_checkpoint_metadata(
        *,
        checkpoint_path: Path,
        metadata: dict[str, object] | None,
        expected_metadata: dict[str, str | int],
    ) -> None:
        if metadata is None:
            raise ValueError(
                "Checkpoint is missing compatibility metadata and cannot be resumed safely. "
                f"Create a new checkpoint with the current agent: {checkpoint_path}"
            )

        schema_version = metadata.get("schema_version")
        if schema_version != expected_metadata["schema_version"]:
            raise ValueError(
                "Checkpoint schema version mismatch: "
                f"expected={expected_metadata['schema_version']} actual={schema_version}"
            )

        workspace_value = metadata.get("workspace")
        if workspace_value != expected_metadata["workspace"]:
            raise ValueError(
                "Checkpoint workspace mismatch: "
                f"expected={expected_metadata['workspace']} actual={workspace_value}"
            )

        fingerprint_value = metadata.get("strategy_fingerprint")
        if fingerprint_value != expected_metadata["strategy_fingerprint"]:
            raise ValueError(
                "Checkpoint strategy fingerprint mismatch: checkpoint strategies do not "
                "match the current resume configuration."
            )
        validation_value = metadata.get("validation_fingerprint")
        if validation_value != expected_metadata["validation_fingerprint"]:
            raise ValueError(
                "Checkpoint validation fingerprint mismatch: checkpoint validation "
                "commands do not match the current resume configuration."
            )
        expected_format = expected_metadata.get("checkpoint_format")
        actual_format = metadata.get("checkpoint_format")
        if (
            expected_format is not None
            and actual_format is not None
            and expected_format != actual_format
        ):
            raise ValueError(
                "Checkpoint serialization mode mismatch: "
                f"expected={expected_format} actual={actual_format}"
            )

    @staticmethod
    def _resolve_checkpoint_path(
        *,
        workspace: Path,
        checkpoint_path: str | Path | None,
        require_existing: bool,
    ) -> Path | None:
        if checkpoint_path is None:
            return None

        candidate = Path(checkpoint_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        resolved = candidate.resolve()
        if not is_within_workspace(workspace, resolved):
            raise ValueError(
                f"Checkpoint path is outside workspace and is not allowed: {resolved}"
            )
        if require_existing and not resolved.exists():
            raise FileNotFoundError(f"Checkpoint file does not exist: {resolved}")
        if resolved.exists() and resolved.is_dir():
            raise ValueError(f"Checkpoint path must be a file, got directory: {resolved}")
        return resolved

    @staticmethod
    def _load_checkpoint(
        checkpoint_path: Path,
    ) -> tuple[SessionReport, dict[str, object] | None]:
        raw_bytes = checkpoint_path.read_bytes()
        if raw_bytes.startswith(_CHECKPOINT_BINARY_MAGIC):
            binary_payload = raw_bytes[len(_CHECKPOINT_BINARY_MAGIC) :]
            payload = pickle.loads(binary_payload)
            if not isinstance(payload, dict):
                raise ValueError("Binary checkpoint payload must be a mapping.")
            report_payload = payload.get("report")
            if not isinstance(report_payload, dict):
                raise ValueError("Binary checkpoint report payload must be a JSON object.")
            report = SessionReport.from_dict(report_payload)
            metadata = {
                "schema_version": payload.get("schema_version"),
                "workspace": payload.get("workspace"),
                "strategy_fingerprint": payload.get("strategy_fingerprint"),
                "validation_fingerprint": payload.get("validation_fingerprint"),
                "checkpoint_format": payload.get("checkpoint_format"),
            }
            return report, metadata

        raw_json = raw_bytes.decode("utf-8")
        payload = json.loads(raw_json)
        if isinstance(payload, dict) and "report" in payload:
            report_payload = payload.get("report")
            if not isinstance(report_payload, dict):
                raise ValueError("Checkpoint report payload must be a JSON object.")
            report = SessionReport.from_dict(report_payload)
            metadata = {
                "schema_version": payload.get("schema_version"),
                "workspace": payload.get("workspace"),
                "strategy_fingerprint": payload.get("strategy_fingerprint"),
                "validation_fingerprint": payload.get("validation_fingerprint"),
                "checkpoint_format": payload.get("checkpoint_format"),
            }
            return report, metadata
        return SessionReport.from_json(raw_json), None

    @staticmethod
    def _persist_checkpoint(
        report: SessionReport,
        checkpoint_path: Path | None,
        checkpoint_metadata: dict[str, str | int],
        checkpoint_serialization_mode: str = "json",
    ) -> None:
        if checkpoint_path is None:
            return
        payload = {
            "schema_version": checkpoint_metadata["schema_version"],
            "workspace": checkpoint_metadata["workspace"],
            "strategy_fingerprint": checkpoint_metadata["strategy_fingerprint"],
            "validation_fingerprint": checkpoint_metadata["validation_fingerprint"],
            "checkpoint_format": checkpoint_metadata.get("checkpoint_format", checkpoint_serialization_mode),
            "report": report.to_dict(),
        }
        mode = checkpoint_serialization_mode.strip().lower()
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

        if mode in {"json", "ab"}:
            temp_path = checkpoint_path.parent / f".{checkpoint_path.name}.tmp"
            temp_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temp_path.replace(checkpoint_path)

        if mode in {"binary", "ab"}:
            binary_target = (
                checkpoint_path
                if mode == "binary"
                else checkpoint_path.with_suffix(f"{checkpoint_path.suffix}.bin")
            )
            binary_temp = binary_target.parent / f".{binary_target.name}.tmp"
            binary_data = _CHECKPOINT_BINARY_MAGIC + pickle.dumps(
                payload,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            binary_temp.write_bytes(binary_data)
            binary_temp.replace(binary_target)

    def _validate_rollback_contract(
        self,
        workspace: Path,
        changed_files: Sequence[Path],
        rollback_entries: Sequence[FileRollback],
    ) -> str | None:
        if not changed_files:
            return None
        if not rollback_entries:
            return (
                "Blocked strategy because it reported changed files without "
                "rollback snapshots."
            )

        changed_paths: set[Path] = set()
        for changed_file in changed_files:
            candidate = changed_file if changed_file.is_absolute() else workspace / changed_file
            changed_paths.add(candidate.resolve())

        rollback_paths: set[Path] = set()
        for entry in rollback_entries:
            entry_candidate = entry.path if entry.path.is_absolute() else workspace / entry.path
            resolved_entry = entry_candidate.resolve()
            if not is_within_workspace(workspace, resolved_entry):
                return (
                    "Blocked strategy because rollback snapshot path is outside workspace: "
                    f"{resolved_entry}"
                )
            rollback_paths.add(resolved_entry)

        missing_paths = sorted(changed_paths - rollback_paths)
        if missing_paths:
            missing_rendered = ", ".join(str(path) for path in missing_paths)
            return (
                "Blocked strategy because rollback snapshots were missing for changed "
                f"files: {missing_rendered}"
            )
        return None

    def _rollback_changes(
        self,
        workspace: Path,
        rollback_entries: Sequence[FileRollback],
    ) -> tuple[bool, str]:
        if not rollback_entries:
            return False, (
                "Verification failed and rollback was not possible because no "
                "rollback entries were provided by the strategy."
            )

        restored = 0
        for entry in rollback_entries:
            path = entry.path if entry.path.is_absolute() else workspace / entry.path
            resolved_path = path.resolve()
            if not is_within_workspace(workspace, resolved_path):
                return (
                    False,
                    f"Rollback blocked because target path is outside workspace: {resolved_path}",
                )
            try:
                if entry.existed_before:
                    if entry.content is None:
                        return (
                            False,
                            f"Rollback data missing original content for {resolved_path}.",
                        )
                    resolved_path.parent.mkdir(parents=True, exist_ok=True)
                    resolved_path.write_text(entry.content, encoding="utf-8")
                else:
                    if resolved_path.exists():
                        resolved_path.unlink()
            except OSError as exc:
                return False, f"Rollback failed for {resolved_path}: {exc}"
            restored += 1
        return True, f"Rollback restored {restored} file(s)."

    def _run_verification(
        self,
        command: str,
        workspace: Path,
        validation_commands: Sequence[str],
        verification_cache: dict[tuple[int, str], CommandResult],
        verification_epoch: int,
    ) -> tuple[bool, CommandResult]:
        primary_result = self.executor(command, workspace)
        if primary_result.return_code != 0:
            return False, primary_result

        last_result = primary_result
        for validation_command in validation_commands:
            cache_key = (verification_epoch, validation_command)
            if self.enable_verification_cache:
                cached_result = verification_cache.get(cache_key)
                if cached_result is not None:
                    logger.info(
                        "Using cached validation result: command=%s epoch=%s",
                        validation_command,
                        verification_epoch,
                    )
                    last_result = cached_result
                    continue
            logger.info(
                "Running validation command: %s (cwd=%s)",
                validation_command,
                workspace,
            )
            validation_result = self.executor(validation_command, workspace)
            last_result = validation_result
            if validation_result.return_code != 0:
                return False, validation_result
            if self.enable_verification_cache:
                verification_cache[cache_key] = validation_result
        return True, last_result

    @staticmethod
    def _order_strategies_for_failure(
        *,
        strategies: Sequence[FixStrategy],
        failure_type: FailureType,
    ) -> tuple[FixStrategy, ...]:
        matching: list[FixStrategy] = []
        neutral: list[FixStrategy] = []
        non_matching: list[FixStrategy] = []
        for strategy in strategies:
            allowed_failures = getattr(strategy, "allowed_failures", None)
            if allowed_failures is None:
                neutral.append(strategy)
                continue
            if failure_type in allowed_failures:
                matching.append(strategy)
                continue
            non_matching.append(strategy)
        return tuple(matching + neutral + non_matching)

    def _sleep_before_next_attempt(self, *, attempt_number: int, max_attempts: int) -> None:
        """Throttle consecutive retries with optional exponential backoff and jitter."""

        if self.retry_backoff_base_seconds <= 0:
            return
        if attempt_number >= max_attempts:
            return

        base_delay = self.retry_backoff_base_seconds * (2 ** (attempt_number - 1))
        capped_base_delay = min(base_delay, self.retry_backoff_max_seconds)
        jitter = 0.0
        if self.retry_backoff_jitter_seconds > 0:
            jitter_seed = self.random_func()
            jitter_ratio = min(max(jitter_seed, 0.0), 1.0)
            jitter = jitter_ratio * self.retry_backoff_jitter_seconds
        delay_seconds = min(capped_base_delay + jitter, self.retry_backoff_max_seconds)
        if delay_seconds <= 0:
            return

        logger.info(
            "Backoff before retry: next_attempt=%s delay_seconds=%.3f",
            attempt_number + 1,
            delay_seconds,
        )
        self.sleep_func(delay_seconds)


def create_default_senior_agent(
    provider: str = "codex",
    api_key: str | None = None,
    model: str | None = None,
    workspace: str | Path = ".",
    max_attempts: int = 3,
    timeout_seconds: int = 180,
    validation_commands: Sequence[str] | None = None,
    retry_backoff_base_seconds: float = 0.0,
    retry_backoff_max_seconds: float = 30.0,
    retry_backoff_jitter_seconds: float = 0.0,
    adaptive_strategy_ordering: bool = True,
    enable_verification_cache: bool = True,
    enable_tiered_routing: bool = True,
    enable_local_offload: bool = True,
    local_model: str = "deepseek-coder:latest",
    enable_speculative_racing: bool = True,
    session_budget_usd: float = 2.0,
    estimated_cloud_request_cost_usd: float = 0.02,
) -> SeniorAgent:
    workspace_path = Path(workspace).resolve()
    provider_normalized = provider.lower()

    codex_client = CodexCLIClient(
        api_key=api_key,
        model=model if provider_normalized == "codex" else None,
        workspace=workspace_path,
        timeout_seconds=timeout_seconds,
    )
    gemini_client = GeminiCLIClient(
        api_key=api_key,
        model=model if provider_normalized == "gemini" else None,
        workspace=workspace_path,
        timeout_seconds=timeout_seconds,
    )

    if provider_normalized == "codex":
        primary_client = codex_client
        secondary_client = gemini_client
    elif provider_normalized == "gemini":
        primary_client = gemini_client
        secondary_client = codex_client
    elif provider_normalized in {"dual", "auto"}:
        primary_client = codex_client
        secondary_client = gemini_client
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    llm_client = primary_client
    if enable_tiered_routing:
        local_client = (
            LocalOffloadClient(
                model=local_model,
                workspace=workspace_path,
                timeout_seconds=min(timeout_seconds, 60),
            )
            if enable_local_offload and shutil.which("ollama") is not None
            else None
        )
        llm_client = MultiCloudRouter(
            cloud_clients=(primary_client, secondary_client),
            local_client=local_client,
            enable_speculative_racing=enable_speculative_racing,
            session_budget_usd=session_budget_usd,
            estimated_cloud_request_cost_usd=estimated_cloud_request_cost_usd,
        )

    default_llm_strategy = LLMStrategy(
        llm_client=llm_client,
        name=f"{provider_normalized}_llm_strategy",
    )
    return SeniorAgent(
        max_attempts=max_attempts,
        classifier=classify_failure,
        executor=run_shell_command,
        default_strategies=(default_llm_strategy,),
        default_validation_commands=validation_commands,
        retry_backoff_base_seconds=retry_backoff_base_seconds,
        retry_backoff_max_seconds=retry_backoff_max_seconds,
        retry_backoff_jitter_seconds=retry_backoff_jitter_seconds,
        adaptive_strategy_ordering=adaptive_strategy_ordering,
        enable_verification_cache=enable_verification_cache,
    )
