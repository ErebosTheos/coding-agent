from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
from pathlib import Path

from senior_agent.dependency_manager import DependencyManager
from senior_agent.engine import Executor, SeniorAgent, run_shell_command
from senior_agent.llm_client import LLMClient, LLMClientError
from senior_agent.models import CommandResult, FileRollback, ImplementationPlan, SessionReport
from senior_agent.patterns import CODE_FENCE_PATTERN
from senior_agent.planner import FeaturePlanner
from senior_agent.symbol_graph import SymbolGraph
from senior_agent.style_mimic import StyleMimic
from senior_agent.test_writer import TestWriter
from senior_agent.utils import is_within_workspace
from senior_agent.visual_reporter import VisualReporter

logger = logging.getLogger(__name__)

class MultiAgentOrchestrator:
    """Coordinate plan -> implement -> verify flow for feature requests."""

    def __init__(
        self,
        llm_client: LLMClient,
        planner: FeaturePlanner,
        executor: Executor = run_shell_command,
        visual_reporter: VisualReporter | None = None,
        test_writer: TestWriter | None = None,
        dependency_manager: DependencyManager | None = None,
        style_mimic: StyleMimic | None = None,
        symbol_graph: SymbolGraph | None = None,
        architect_llm_client: LLMClient | None = None,
        reviewer_llm_client: LLMClient | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.architect_llm_client = architect_llm_client
        self.reviewer_llm_client = reviewer_llm_client
        self.planner = planner
        self.executor = executor
        self.visual_reporter = visual_reporter or VisualReporter()
        self.test_writer = test_writer or TestWriter(llm_client=llm_client)
        self.dependency_manager = dependency_manager or DependencyManager(executor=executor)
        self.style_mimic = style_mimic or StyleMimic()
        self.symbol_graph = symbol_graph or SymbolGraph()
        self._rollback_agent = SeniorAgent(max_attempts=1, executor=executor)
        self._environment_workspace = Path(".").resolve()

    def execute_feature_request(
        self,
        requirement: str,
        codebase_summary: str,
        workspace: str | Path = ".",
    ) -> bool:
        workspace_root = Path(workspace).resolve()
        self._environment_workspace = workspace_root
        if not workspace_root.exists() or not workspace_root.is_dir():
            logger.error("Workspace path is invalid or missing: %s", workspace_root)
            return False

        try:
            plan = self.planner.plan_feature(requirement, codebase_summary)
        except (LLMClientError, ValueError) as exc:
            logger.error("Feature planning failed: %s", exc)
            fallback_plan = ImplementationPlan(
                feature_name=requirement.strip() or "Unplanned Feature",
                summary="Feature planning failed.",
            )
            blocked_reason = f"Feature planning failed: {exc}"
            self._emit_visual_summary(
                plan=fallback_plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=CommandResult(
                        command="planning",
                        return_code=1,
                        stdout="",
                        stderr=blocked_reason,
                    ),
                    success=False,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Unexpected planner failure: %s", exc)
            fallback_plan = ImplementationPlan(
                feature_name=requirement.strip() or "Unplanned Feature",
                summary="Feature planning failed.",
            )
            blocked_reason = f"Unexpected planner failure: {exc}"
            self._emit_visual_summary(
                plan=fallback_plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=CommandResult(
                        command="planning",
                        return_code=1,
                        stdout="",
                        stderr=blocked_reason,
                    ),
                    success=False,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        self.test_writer.workspace = workspace_root

        try:
            style_rules = self.style_mimic.infer_project_style(workspace_root)
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Style inference failed; falling back to default style guidance: %s", exc)
            style_rules = "Style: preserve existing conventions."

        try:
            self.symbol_graph.build_graph(workspace_root)
            plan = self._augment_plan_with_symbol_graph_validation(
                plan=plan,
                workspace_root=workspace_root,
            )
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception(
                "Symbol graph augmentation failed; continuing without proactive impact validation: %s",
                exc,
            )

        plan, generated_file_overrides, test_generation_note = self._augment_plan_with_generated_tests(
            plan=plan,
            workspace_root=workspace_root,
        )
        self._log_plan(plan)
        if test_generation_note is not None:
            blocked_reason = test_generation_note
            final_result = CommandResult(
                command="test-generation",
                return_code=1,
                stdout="",
                stderr=blocked_reason,
            )
            logger.error("TDD generation failed: %s", blocked_reason)
            self._emit_visual_summary(
                plan=plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=final_result,
                    success=False,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        validation_commands = tuple(command.strip() for command in plan.validation_commands if command.strip())
        if not validation_commands:
            inferred_validation_commands = tuple(
                self._autodetect_validation_commands(workspace_root)
            )
            if inferred_validation_commands:
                validation_commands = inferred_validation_commands
                plan = ImplementationPlan(
                    feature_name=plan.feature_name,
                    summary=plan.summary,
                    new_files=list(plan.new_files),
                    modified_files=list(plan.modified_files),
                    steps=list(plan.steps),
                    validation_commands=list(inferred_validation_commands),
                    design_guidance=plan.design_guidance,
                )
                logger.info(
                    "No validation commands in plan '%s'; using autodetected defaults: %s",
                    plan.feature_name,
                    ", ".join(validation_commands),
                )
            else:
                blocked_reason = (
                    "No validation commands were provided in the plan and no "
                    "safe defaults could be detected."
                )
                final_result = CommandResult(
                    command="validation-autodetect",
                    return_code=1,
                    stdout="",
                    stderr=blocked_reason,
                )
                logger.error(blocked_reason)
                self._emit_visual_summary(
                    plan=plan,
                    report=self._build_session_report(
                        command=requirement,
                        final_result=final_result,
                        success=False,
                        blocked_reason=blocked_reason,
                    ),
                    workspace_root=workspace_root,
                )
                return False

        success = False
        blocked_reason: str | None = None
        final_result = CommandResult(
            command=requirement,
            return_code=0,
            stdout="Feature plan generated.",
            stderr="",
        )

        if not self._check_environment(list(validation_commands)):
            blocked_reason = (
                "Environment check failed for planned validation commands. "
                "Aborting before file generation."
            )
            final_result = CommandResult(
                command="environment-check",
                return_code=1,
                stdout="",
                stderr=blocked_reason,
            )
            logger.critical(
                "Environment check failed for planned validation commands. "
                "Aborting before file generation."
            )
            self._emit_visual_summary(
                plan=plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=final_result,
                    success=success,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        rollback_map: dict[Path, FileRollback] = {}
        applied_ok, failure_note = self._apply_plan(
            plan=plan,
            workspace_root=workspace_root,
            rollback_map=rollback_map,
            generated_file_overrides=generated_file_overrides,
            style_rules=style_rules,
        )
        if not applied_ok:
            blocked_reason = failure_note or "Feature implementation failed."
            final_result = CommandResult(
                command="plan-apply",
                return_code=1,
                stdout="",
                stderr=blocked_reason,
            )
            self._critical_failure_and_rollback(
                reason=blocked_reason,
                workspace_root=workspace_root,
                rollback_entries=tuple(rollback_map.values()),
            )
            self._emit_visual_summary(
                plan=plan,
                report=self._build_session_report(
                    command=requirement,
                    final_result=final_result,
                    success=success,
                    blocked_reason=blocked_reason,
                ),
                workspace_root=workspace_root,
            )
            return False

        if validation_commands:
            validation_ok, validation_result = self._run_validation(validation_commands, workspace_root)
            if validation_result is not None:
                final_result = validation_result
            if not validation_ok:
                blocked_reason = (
                    "Validation command execution failed after applying planned "
                    "file changes."
                )
                self._critical_failure_and_rollback(
                    reason=blocked_reason,
                    workspace_root=workspace_root,
                    rollback_entries=tuple(rollback_map.values()),
                )
                self._emit_visual_summary(
                    plan=plan,
                    report=self._build_session_report(
                        command=requirement,
                        final_result=final_result,
                        success=success,
                        blocked_reason=blocked_reason,
                    ),
                    workspace_root=workspace_root,
                )
                return False

        if self.reviewer_llm_client is not None:
            review_passed, review_note = self._run_gatekeeper_review(
                plan=plan,
                requirement=requirement,
                workspace_root=workspace_root,
                validation_commands=validation_commands,
                final_result=final_result,
            )
            if review_note:
                logger.info("Gatekeeper review note: %s", review_note)
            if not review_passed:
                blocked_reason = f"Gatekeeper review rejected changes: {review_note}"
                final_result = CommandResult(
                    command="gatekeeper-review",
                    return_code=1,
                    stdout="",
                    stderr=blocked_reason,
                )
                self._critical_failure_and_rollback(
                    reason=blocked_reason,
                    workspace_root=workspace_root,
                    rollback_entries=tuple(rollback_map.values()),
                )
                self._emit_visual_summary(
                    plan=plan,
                    report=self._build_session_report(
                        command=requirement,
                        final_result=final_result,
                        success=success,
                        blocked_reason=blocked_reason,
                    ),
                    workspace_root=workspace_root,
                )
                return False

        success = True
        self._emit_visual_summary(
            plan=plan,
            report=self._build_session_report(
                command=requirement,
                final_result=final_result,
                success=success,
                blocked_reason=blocked_reason,
            ),
            workspace_root=workspace_root,
        )
        return True

    def _log_plan(self, plan: ImplementationPlan) -> None:
        logger.info(
            "ImplementationPlan: feature=%s summary=%s new_files=%s modified_files=%s steps=%s validations=%s",
            plan.feature_name,
            plan.summary,
            len(plan.new_files),
            len(plan.modified_files),
            len(plan.steps),
            len(plan.validation_commands),
        )

    def _apply_plan(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        rollback_map: dict[Path, FileRollback],
        generated_file_overrides: dict[str, str],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        for file_path in plan.new_files:
            created_ok, create_note = self._create_new_file(
                plan=plan,
                workspace_root=workspace_root,
                file_path=file_path,
                rollback_map=rollback_map,
                generated_file_overrides=generated_file_overrides,
                style_rules=style_rules,
            )
            if not created_ok:
                return False, create_note

        for file_path in plan.modified_files:
            modified_ok, modify_note = self._modify_existing_file(
                plan=plan,
                workspace_root=workspace_root,
                file_path=file_path,
                rollback_map=rollback_map,
                style_rules=style_rules,
            )
            if not modified_ok:
                return False, modify_note

        return True, None

    def _create_new_file(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        file_path: str,
        rollback_map: dict[Path, FileRollback],
        generated_file_overrides: dict[str, str],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        resolved_target = self._resolve_target_path(workspace_root, file_path)
        if resolved_target is None:
            return False, f"Invalid new-file path in plan: {file_path!r}"

        snap_ok, snap_note = self._ensure_rollback_entry(
            resolved_target=resolved_target,
            rollback_map=rollback_map,
        )
        if not snap_ok:
            return False, snap_note

        snapshot = rollback_map[resolved_target]
        if snapshot.existed_before:
            return (
                False,
                f"Refusing to create existing file declared as new: {resolved_target}",
            )

        relative_target = resolved_target.relative_to(workspace_root)
        generated = generated_file_overrides.get(relative_target.as_posix())
        if generated is None:
            prompt = self._build_new_file_prompt(plan, relative_target, style_rules=style_rules)
            generated = self._generate_code(prompt, relative_target)
            if generated is None:
                return False, f"LLM generation returned no usable code for new file {relative_target}."

        try:
            resolved_target.parent.mkdir(parents=True, exist_ok=True)
            resolved_target.write_text(generated, encoding="utf-8")
        except (OSError, UnicodeEncodeError) as exc:
            return False, f"Failed to write new file {resolved_target}: {exc}"

        logger.info("Created new file: %s", relative_target)
        return True, None

    def _augment_plan_with_symbol_graph_validation(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> ImplementationPlan:
        if not plan.modified_files:
            return plan

        impacted_test_files = self._discover_impacted_test_files(
            modified_files=plan.modified_files,
            workspace_root=workspace_root,
        )
        if not impacted_test_files:
            return plan

        proactive_commands = self.test_writer.build_validation_commands(impacted_test_files)
        if not proactive_commands:
            return plan

        merged_validation_commands = list(plan.validation_commands)
        added_commands = 0
        for command in proactive_commands:
            cleaned = command.strip()
            if cleaned and cleaned not in merged_validation_commands:
                merged_validation_commands.append(cleaned)
                added_commands += 1

        if added_commands == 0:
            return plan

        logger.info(
            "Symbol graph added proactive validation commands: impacted_tests=%s added_commands=%s",
            len(impacted_test_files),
            added_commands,
        )
        return ImplementationPlan(
            feature_name=plan.feature_name,
            summary=plan.summary,
            new_files=list(plan.new_files),
            modified_files=list(plan.modified_files),
            steps=list(plan.steps),
            validation_commands=merged_validation_commands,
            design_guidance=plan.design_guidance,
        )

    def _discover_impacted_test_files(
        self,
        *,
        modified_files: list[str],
        workspace_root: Path,
    ) -> list[str]:
        impacted_tests: list[str] = []
        seen_tests: set[str] = set()

        for raw_modified_file in modified_files:
            resolved_modified = self._resolve_target_path(workspace_root, raw_modified_file)
            if resolved_modified is None:
                continue

            symbols = self.symbol_graph.get_defined_symbols(resolved_modified)
            if not symbols:
                continue

            for symbol_name in symbols:
                dependent_files = self.symbol_graph.get_dependents(
                    resolved_modified,
                    symbol_name,
                )
                for dependent_file in dependent_files:
                    candidate_paths = self._candidate_test_paths_for_source(
                        source_file=dependent_file,
                        workspace_root=workspace_root,
                    )
                    for candidate in candidate_paths:
                        if candidate in seen_tests:
                            continue
                        seen_tests.add(candidate)
                        impacted_tests.append(candidate)

        return impacted_tests

    def _candidate_test_paths_for_source(
        self,
        *,
        source_file: Path,
        workspace_root: Path,
    ) -> list[str]:
        if not is_within_workspace(workspace_root, source_file):
            return []
        if not source_file.exists() or not source_file.is_file():
            return []

        suffix = source_file.suffix.lower()
        stem = source_file.stem
        candidates: list[Path] = []

        if suffix == ".py":
            candidates.extend(
                [
                    workspace_root / "tests" / f"test_{stem}.py",
                    source_file.parent / f"test_{stem}.py",
                ]
            )
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            candidates.extend(
                [
                    workspace_root / "tests" / f"{stem}.test{suffix}",
                    source_file.parent / f"{stem}.test{suffix}",
                ]
            )
        elif suffix == ".go":
            candidates.extend(
                [
                    source_file.parent / f"{stem}_test.go",
                    workspace_root / "tests" / f"{stem}_test.go",
                ]
            )
        else:
            return []

        resolved_candidates: list[str] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if not is_within_workspace(workspace_root, resolved):
                continue
            if not resolved.exists() or not resolved.is_file():
                continue
            relative = resolved.relative_to(workspace_root).as_posix()
            resolved_candidates.append(relative)
        return resolved_candidates

    def _augment_plan_with_generated_tests(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> tuple[ImplementationPlan, dict[str, str], str | None]:
        if not plan.new_files:
            return plan, {}, None

        files_content, context_note = self._collect_files_content_for_test_generation(
            plan=plan,
            workspace_root=workspace_root,
        )
        if context_note is not None:
            return plan, {}, context_note

        try:
            generated_tests = self.test_writer.generate_test_suite(plan, files_content)
            validation_additions = self.test_writer.build_validation_commands(generated_tests.keys())
        except (LLMClientError, ValueError) as exc:
            return plan, {}, f"Test suite generation failed: {exc}"
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Unexpected test-writer failure: %s", exc)
            return plan, {}, f"Unexpected test suite generation failure: {exc}"

        if not generated_tests:
            return plan, {}, None

        normalized_tests: dict[str, str] = {}
        for raw_path, raw_content in generated_tests.items():
            candidate = str(raw_path).strip()
            if not candidate:
                continue
            resolved = self._resolve_target_path(workspace_root, candidate)
            if resolved is None:
                return plan, {}, f"Generated test file path is invalid: {candidate!r}"

            relative = resolved.relative_to(workspace_root).as_posix()
            normalized_content = self._normalize_generated_content(str(raw_content))
            if not normalized_content.strip():
                return plan, {}, f"Generated test file is empty: {relative}"
            if not normalized_content.endswith("\n"):
                normalized_content = f"{normalized_content}\n"
            normalized_tests[relative] = normalized_content

        if not normalized_tests:
            return plan, {}, None

        new_files = list(plan.new_files)
        for generated_test_path in normalized_tests:
            if generated_test_path not in new_files:
                new_files.append(generated_test_path)

        validation_commands = list(plan.validation_commands)
        for validation_command in validation_additions:
            cleaned = validation_command.strip()
            if cleaned and cleaned not in validation_commands:
                validation_commands.append(cleaned)

        updated_plan = ImplementationPlan(
            feature_name=plan.feature_name,
            summary=plan.summary,
            new_files=new_files,
            modified_files=list(plan.modified_files),
            steps=list(plan.steps),
            validation_commands=validation_commands,
            design_guidance=plan.design_guidance,
        )
        logger.info(
            "TDD generated test files: files=%s commands=%s",
            len(normalized_tests),
            len(validation_additions),
        )
        return updated_plan, normalized_tests, None

    def _collect_files_content_for_test_generation(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
    ) -> tuple[dict[str, str], str | None]:
        collected: dict[str, str] = {}
        candidates = list(dict.fromkeys((*plan.new_files, *plan.modified_files)))

        for raw_path in candidates:
            candidate = str(raw_path).strip()
            if not candidate:
                continue

            resolved = self._resolve_target_path(workspace_root, candidate)
            if resolved is None:
                return {}, f"Invalid file path in plan while preparing tests: {candidate!r}"

            content = ""
            if resolved.exists():
                if not resolved.is_file():
                    return {}, f"Test context path is not a regular file: {resolved}"
                try:
                    content = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    return {}, f"Failed to read file for test context {resolved}: {exc}"

            collected[candidate] = content
            collected[resolved.relative_to(workspace_root).as_posix()] = content

        return collected, None

    def _modify_existing_file(
        self,
        *,
        plan: ImplementationPlan,
        workspace_root: Path,
        file_path: str,
        rollback_map: dict[Path, FileRollback],
        style_rules: str,
    ) -> tuple[bool, str | None]:
        resolved_target = self._resolve_target_path(workspace_root, file_path)
        if resolved_target is None:
            return False, f"Invalid modified-file path in plan: {file_path!r}"

        snap_ok, snap_note = self._ensure_rollback_entry(
            resolved_target=resolved_target,
            rollback_map=rollback_map,
        )
        if not snap_ok:
            return False, snap_note

        snapshot = rollback_map[resolved_target]
        if not snapshot.existed_before:
            return False, f"Planned modified file does not exist: {resolved_target}"
        if snapshot.content is None:
            return False, f"Rollback snapshot for {resolved_target} is missing file content."

        relative_target = resolved_target.relative_to(workspace_root)
        prompt = self._build_modify_file_prompt(
            plan=plan,
            relative_target=relative_target,
            current_content=snapshot.content,
            style_rules=style_rules,
        )
        generated = self._generate_code(prompt, relative_target)
        if generated is None:
            return False, f"LLM generation returned no usable code for modified file {relative_target}."

        try:
            resolved_target.write_text(generated, encoding="utf-8")
        except (OSError, UnicodeEncodeError) as exc:
            return False, f"Failed to write modified file {resolved_target}: {exc}"

        logger.info("Updated file: %s", relative_target)
        return True, None

    def _ensure_rollback_entry(
        self,
        *,
        resolved_target: Path,
        rollback_map: dict[Path, FileRollback],
    ) -> tuple[bool, str | None]:
        if resolved_target in rollback_map:
            return True, None

        if resolved_target.exists():
            if not resolved_target.is_file():
                return False, f"Rollback snapshot target is not a regular file: {resolved_target}"
            try:
                content = resolved_target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return False, f"Failed to capture rollback snapshot for {resolved_target}: {exc}"
            rollback_map[resolved_target] = FileRollback(
                path=resolved_target,
                existed_before=True,
                content=content,
            )
        else:
            rollback_map[resolved_target] = FileRollback(
                path=resolved_target,
                existed_before=False,
                content=None,
            )

        return True, None

    def _critical_failure_and_rollback(
        self,
        *,
        reason: str,
        workspace_root: Path,
        rollback_entries: tuple[FileRollback, ...],
    ) -> None:
        logger.critical(reason)
        if not rollback_entries:
            logger.critical("No rollback snapshots captured; no atomic recovery could be performed.")
            return

        rollback_success, rollback_note = self._rollback_agent._rollback_changes(
            workspace=workspace_root,
            rollback_entries=rollback_entries,
        )
        if rollback_success:
            logger.critical("Atomic rollback succeeded: %s", rollback_note)
            return
        logger.critical("Atomic rollback failed: %s", rollback_note)

    def _resolve_target_path(self, workspace_root: Path, raw_path: str) -> Path | None:
        candidate_raw = str(raw_path).strip()
        if not candidate_raw:
            logger.error("Encountered empty file path in implementation plan.")
            return None

        candidate = Path(candidate_raw)
        resolved = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
        if not is_within_workspace(workspace_root, resolved):
            logger.error(
                "Blocked out-of-workspace file operation: workspace=%s target=%s",
                workspace_root,
                resolved,
            )
            return None
        return resolved

    def _generate_code(self, prompt: str, relative_target: Path) -> str | None:
        try:
            raw_output = self.llm_client.generate_fix(prompt)
        except LLMClientError as exc:
            logger.error("LLM generation failed for %s: %s", relative_target, exc)
            return None
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Unexpected LLM failure for %s: %s", relative_target, exc)
            return None

        normalized = self._normalize_generated_content(raw_output)
        if not normalized.strip():
            logger.error("LLM returned empty code for %s", relative_target)
            return None
        if not normalized.endswith("\n"):
            normalized = f"{normalized}\n"
        return normalized

    @staticmethod
    def _normalize_generated_content(raw_output: str) -> str:
        stripped = raw_output.strip()
        match = CODE_FENCE_PATTERN.search(stripped)
        if match:
            return match.group("code").strip()
        return stripped

    def _check_environment(self, commands: list[str]) -> bool:
        seen_binaries: set[str] = set()
        for command in commands:
            command_text = command.strip()
            if not command_text:
                continue

            try:
                tokens = shlex.split(command_text)
            except ValueError as exc:
                logger.critical(
                    "Could not parse validation command for environment check: command=%s error=%s",
                    command_text,
                    exc,
                )
                return False
            if not tokens:
                continue

            binary = tokens[0]
            if binary in seen_binaries:
                continue
            seen_binaries.add(binary)

            if shutil.which(binary) is None:
                logger.critical(
                    "Missing validation binary required for orchestrator execution: binary=%s command=%s",
                    binary,
                    command_text,
                )
                return False

        return True

    def _run_validation(
        self,
        commands: tuple[str, ...],
        workspace_root: Path,
    ) -> tuple[bool, CommandResult | None]:
        last_result: CommandResult | None = None
        for command in commands:
            logger.info("Running orchestrator validation command: %s (cwd=%s)", command, workspace_root)
            try:
                result = self.executor(command, workspace_root)
            except Exception as exc:  # pragma: no cover - defensive guardrail
                logger.exception("Validation command raised error: command=%s error=%s", command, exc)
                return False, last_result

            last_result = result

            if result.return_code != 0:
                dependency_fixed = self.dependency_manager.check_and_fix_dependencies(
                    result=result,
                    workspace=workspace_root,
                )
                if dependency_fixed:
                    logger.info(
                        "Retrying validation command after dependency auto-install: %s",
                        command,
                    )
                    try:
                        retry_result = self.executor(command, workspace_root)
                    except Exception as exc:  # pragma: no cover - defensive guardrail
                        logger.exception(
                            "Retried validation command raised error: command=%s error=%s",
                            command,
                            exc,
                        )
                        return False, result
                    last_result = retry_result
                    if retry_result.return_code == 0:
                        continue
                    result = retry_result

                logger.error(
                    "Validation command failed: command=%s return_code=%s stderr=%s",
                    command,
                    result.return_code,
                    result.stderr.strip(),
                )
                return False, result

        logger.info("All orchestrator validation commands passed (%s).", len(commands))
        return True, last_result

    def _autodetect_validation_commands(self, workspace_root: Path) -> list[str]:
        commands: list[str] = []
        package_json = workspace_root / "package.json"
        if package_json.exists() and package_json.is_file():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if isinstance(scripts, dict):
                if isinstance(scripts.get("test"), str):
                    commands.append("npm test")
                if isinstance(scripts.get("lint"), str):
                    commands.append("npm run lint")

        if (workspace_root / "go.mod").exists():
            commands.append("go test ./...")
        if (workspace_root / "Cargo.toml").exists():
            commands.append("cargo test")
        if self._has_pytest_config(workspace_root):
            commands.append("pytest")
        if (workspace_root / "tests").exists():
            commands.append("python -m unittest discover -s tests -v")

        unique_commands: list[str] = []
        seen: set[str] = set()
        for command in commands:
            normalized = command.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_commands.append(normalized)
        return unique_commands

    @staticmethod
    def _has_pytest_config(workspace_root: Path) -> bool:
        if (workspace_root / "pytest.ini").exists():
            return True

        tox_ini = workspace_root / "tox.ini"
        if tox_ini.exists():
            try:
                if "[pytest]" in tox_ini.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                return False

        pyproject = workspace_root / "pyproject.toml"
        if pyproject.exists():
            try:
                if "[tool.pytest.ini_options]" in pyproject.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                return False

        return False

    def _build_session_report(
        self,
        *,
        command: str,
        final_result: CommandResult,
        success: bool,
        blocked_reason: str | None,
    ) -> SessionReport:
        initial_result = CommandResult(
            command=command,
            return_code=0,
            stdout="Feature request accepted.",
            stderr="",
        )
        return SessionReport(
            command=command,
            initial_result=initial_result,
            final_result=final_result,
            attempts=[],
            success=success,
            blocked_reason=blocked_reason,
        )

    def _emit_visual_summary(
        self,
        *,
        plan: ImplementationPlan,
        report: SessionReport,
        workspace_root: Path,
    ) -> None:
        try:
            mermaid = self.visual_reporter.generate_mermaid_summary(plan, report)
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Failed to generate Mermaid summary: %s", exc)
            return

        logger.info("Mermaid execution summary:\n%s", mermaid)
        self._write_mermaid_file(
            workspace_root=workspace_root,
            feature_name=plan.feature_name,
            mermaid=mermaid,
        )

    def _write_mermaid_file(
        self,
        *,
        workspace_root: Path,
        feature_name: str,
        mermaid: str,
    ) -> None:
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", feature_name).strip("_").lower() or "feature"
        output_path = (workspace_root / f"{safe_stem}.mermaid").resolve()
        if not is_within_workspace(workspace_root, output_path):
            logger.error(
                "Blocked writing Mermaid output outside workspace: workspace=%s target=%s",
                workspace_root,
                output_path,
            )
            return

        try:
            output_path.write_text(f"{mermaid}\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Unable to persist Mermaid summary to %s: %s", output_path, exc)
            return
        logger.info("Saved Mermaid summary: %s", output_path)

    def _run_gatekeeper_review(
        self,
        *,
        plan: ImplementationPlan,
        requirement: str,
        workspace_root: Path,
        validation_commands: tuple[str, ...],
        final_result: CommandResult,
    ) -> tuple[bool, str]:
        if self.reviewer_llm_client is None:
            return True, "Gatekeeper review skipped (no reviewer configured)."

        prompt = self._build_gatekeeper_review_prompt(
            plan=plan,
            requirement=requirement,
            workspace_root=workspace_root,
            validation_commands=validation_commands,
            final_result=final_result,
        )
        try:
            raw_review = self.reviewer_llm_client.generate_fix(prompt)
        except LLMClientError as exc:
            return True, f"Gatekeeper review unavailable due to LLM error: {exc}"
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.exception("Unexpected gatekeeper review failure: %s", exc)
            return True, f"Gatekeeper review unavailable due to unexpected error: {exc}"

        normalized = self._normalize_generated_content(raw_review)
        if not normalized.strip():
            return True, "Gatekeeper returned empty review output."

        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            compact = " ".join(normalized.split())
            return True, compact[:600]

        if not isinstance(payload, dict):
            return True, "Gatekeeper response was not a JSON object."

        status_raw = str(payload.get("status", "pass")).strip().lower()
        summary = str(payload.get("summary", "")).strip()
        findings_raw = payload.get("findings")
        findings: list[str] = []
        if isinstance(findings_raw, list):
            findings = [
                str(item).strip()
                for item in findings_raw
                if isinstance(item, str) and item.strip()
            ]

        note = summary or "No summary provided."
        if findings:
            note = f"{note} Findings: {' | '.join(findings[:5])}"

        if status_raw in {"fail", "failed", "block", "blocked", "reject", "rejected"}:
            return False, note
        return True, note

    @staticmethod
    def _build_new_file_prompt(
        plan: ImplementationPlan,
        relative_target: Path,
        *,
        style_rules: str,
    ) -> str:
        steps = "\n".join(f"- {step}" for step in plan.steps) or "- No explicit steps provided."
        return (
            "Role: Lead Developer.\n"
            "Task: Generate production-ready source code for a NEW file.\n"
            "Output constraints: Return ONLY raw file contents. No markdown fences.\n\n"
            f"Feature: {plan.feature_name}\n"
            f"Summary: {plan.summary}\n"
            f"Design Guidance: {plan.design_guidance}\n"
            f"Inferred Project Style: {style_rules}\n"
            f"Target File: {relative_target}\n"
            "Plan Steps:\n"
            f"{steps}\n"
        )

    @staticmethod
    def _build_modify_file_prompt(
        *,
        plan: ImplementationPlan,
        relative_target: Path,
        current_content: str,
        style_rules: str,
    ) -> str:
        steps = "\n".join(f"- {step}" for step in plan.steps) or "- No explicit steps provided."
        return (
            "Role: Lead Developer.\n"
            "Task: Perform a surgical update of an EXISTING file.\n"
            "Output constraints: Return ONLY the FULL updated file contents. No markdown fences.\n"
            "Do not change unrelated behavior.\n\n"
            f"Feature: {plan.feature_name}\n"
            f"Summary: {plan.summary}\n"
            f"Design Guidance: {plan.design_guidance}\n"
            f"Inferred Project Style: {style_rules}\n"
            f"Target File: {relative_target}\n"
            "Plan Steps:\n"
            f"{steps}\n\n"
            "Current File Content:\n"
            "--- BEGIN CURRENT FILE ---\n"
            f"{current_content}\n"
            "--- END CURRENT FILE ---\n"
        )

    @staticmethod
    def _build_gatekeeper_review_prompt(
        *,
        plan: ImplementationPlan,
        requirement: str,
        workspace_root: Path,
        validation_commands: tuple[str, ...],
        final_result: CommandResult,
    ) -> str:
        validations = "\n".join(f"- {command}" for command in validation_commands)
        if not validations:
            validations = "- No validation commands were provided."
        changed_files = "\n".join(
            f"- {path}" for path in [*plan.new_files, *plan.modified_files]
        ) or "- No file changes were listed in the plan."

        combined_output = final_result.combined_output
        if len(combined_output) > 3000:
            combined_output = f"{combined_output[:3000]}\n...[truncated]"

        return (
            "Role: Chief Architect & Senior Reviewer (Gatekeeper).\n"
            "Task: Audit this implementation result for security, performance, reliability, "
            "and idiomatic consistency.\n"
            "Return ONLY one JSON object with this schema:\n"
            "{\n"
            '  "status": "pass|fail",\n'
            '  "summary": "string",\n'
            '  "findings": ["string"]\n'
            "}\n\n"
            f"Requirement: {requirement}\n"
            f"Workspace: {workspace_root}\n"
            f"Feature: {plan.feature_name}\n"
            f"Summary: {plan.summary}\n\n"
            "Planned changed files:\n"
            f"{changed_files}\n\n"
            "Validation commands:\n"
            f"{validations}\n\n"
            "Final validation result:\n"
            f"- command: {final_result.command}\n"
            f"- return_code: {final_result.return_code}\n"
            f"- output:\n{combined_output}\n"
        )
