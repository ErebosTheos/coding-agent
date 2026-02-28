import ast
import os
import time
import asyncio
from pathlib import Path
from typing import Optional
from dataclasses import replace
from .models import PipelineReport, TestSuite, StageTrace
from .llm.router import LLMRouter
from .utils import run_shell_command
from .planner_architect import PlannerArchitect
from .stream_executor import StreamingPlanArchExecutor
from .planner import Planner
from .architect import Architect
from .executor import Executor
from .dependency_manager import DependencyManager
from .test_writer import TestWriter
from .healer import Healer
from .qa_auditor import QAAuditor
from .visual_validator import VisualValidator
from .checkpoint import CheckpointManager
from .reporter import Reporter
from .workspace_lock import WorkspaceLock


def _is_test_file(path: str) -> bool:
    name = os.path.basename(path)
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
        or "/tests/" in path
        or path.startswith("tests/")
    )


def _role_provider(router: LLMRouter, role: str) -> tuple[str, Optional[str]]:
    """Return (provider, model) for a role from router config."""
    cfg = router.config.get("roles", {}).get(role, {})
    return cfg.get("provider", "unknown"), cfg.get("model")


def _source_files_for_testing(generated_files) -> dict:
    """Return only source code files the TestWriter should generate tests for.
    Filters out test files (already handled by executor), config files, and
    non-code assets to avoid redundant LLM calls."""
    source_exts = {".py", ".js", ".ts", ".tsx", ".go", ".rs"}
    skip_names = {
        "requirements.txt", "package.json", "go.mod", "Cargo.toml",
        "Makefile", ".gitignore", "README.md", "pyproject.toml",
    }
    result = {}
    for f in generated_files:
        if _is_test_file(f.file_path):
            continue
        if os.path.basename(f.file_path) in skip_names:
            continue
        if os.path.splitext(f.file_path)[1].lower() in source_exts:
            result[f.file_path] = f.content
    return result


def _is_python_file(path: str) -> bool:
    return path.endswith(".py")


def _module_name_for_path(path: str) -> str:
    if not _is_python_file(path):
        return ""
    parts = path[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(p for p in parts if p)


def _is_package_init(path: str) -> bool:
    return path.endswith("/__init__.py") or path == "__init__.py"


def _module_package_parts(module_name: str, is_package: bool) -> list[str]:
    if not module_name:
        return []
    parts = module_name.split(".")
    return parts if is_package else parts[:-1]


def _resolve_relative_module(
    importer_module: str,
    importer_is_package: bool,
    module: Optional[str],
    level: int,
) -> Optional[str]:
    if level <= 0:
        return module
    package_parts = _module_package_parts(importer_module, importer_is_package)
    up_levels = level - 1
    if up_levels > len(package_parts):
        return None
    prefix = package_parts[: len(package_parts) - up_levels] if up_levels else package_parts
    if module:
        return ".".join(prefix + module.split("."))
    return ".".join(prefix)


def _defined_symbols(tree: ast.AST) -> set[str]:
    symbols: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                symbols.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    symbols.add(alias.asname or alias.name)
    return symbols


def _python_imported_modules(content: str, file_path: str) -> set[str]:
    imported: set[str] = set()
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return imported

    importer_module = _module_name_for_path(file_path)
    importer_is_package = _is_package_init(file_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            resolved = node.module
            if node.level:
                resolved = _resolve_relative_module(
                    importer_module,
                    importer_is_package,
                    node.module,
                    node.level,
                )
            if resolved:
                imported.add(resolved)
    return imported


def _tests_need_regeneration(generated_files) -> bool:
    suspicious_phrases = (
        "cannot inspect",
        "hypothetical",
        "in a real scenario",
        "simulate",
    )
    source_modules = {
        _module_name_for_path(f.file_path)
        for f in generated_files
        if _is_python_file(f.file_path) and not _is_test_file(f.file_path)
    }
    source_modules = {m for m in source_modules if m}
    if not source_modules:
        return False

    for f in generated_files:
        if not (_is_test_file(f.file_path) and _is_python_file(f.file_path)):
            continue
        lower = f.content.lower()
        if any(phrase in lower for phrase in suspicious_phrases):
            return True
        imported = _python_imported_modules(f.content, f.file_path)
        if not imported:
            return True
        references_source = any(
            imported_module == source_module or imported_module.startswith(source_module + ".")
            for imported_module in imported
            for source_module in source_modules
        )
        if not references_source:
            return True
    return False


def _collect_python_consistency_issues(generated_files) -> dict[str, list[str]]:
    source_files = [
        f for f in generated_files
        if _is_python_file(f.file_path) and not _is_test_file(f.file_path)
    ]
    module_to_path: dict[str, str] = {}
    module_to_exports: dict[str, set[str]] = {}
    ast_trees: dict[str, ast.AST] = {}
    issues: dict[str, list[str]] = {}

    for f in source_files:
        module_name = _module_name_for_path(f.file_path)
        if module_name:
            module_to_path[module_name] = f.file_path
        try:
            tree = ast.parse(f.content)
        except SyntaxError as exc:
            issues.setdefault(f.file_path, []).append(
                f"Syntax error blocks imports: line {exc.lineno}: {exc.msg}"
            )
            continue
        ast_trees[f.file_path] = tree
        if module_name:
            module_to_exports[module_name] = _defined_symbols(tree)

    package_roots = {module.split(".")[0] for module in module_to_path if module}

    def _looks_internal(module: str) -> bool:
        return module in module_to_path or module.split(".")[0] in package_roots

    for f in source_files:
        tree = ast_trees.get(f.file_path)
        if not tree:
            continue
        importer_module = _module_name_for_path(f.file_path)
        importer_is_package = _is_package_init(f.file_path)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    if _looks_internal(module) and module not in module_to_path:
                        issues.setdefault(f.file_path, []).append(
                            f"Imports missing internal module '{module}'."
                        )
            elif isinstance(node, ast.ImportFrom):
                target_module = node.module
                if node.level:
                    target_module = _resolve_relative_module(
                        importer_module,
                        importer_is_package,
                        node.module,
                        node.level,
                    )
                if not target_module or not _looks_internal(target_module):
                    continue
                if target_module not in module_to_exports:
                    issues.setdefault(f.file_path, []).append(
                        f"Imports from missing internal module '{target_module}'."
                    )
                    continue
                exported = module_to_exports[target_module]
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if alias.name not in exported:
                        issues.setdefault(f.file_path, []).append(
                            f"Imports missing symbol '{alias.name}' from '{target_module}'."
                        )

    for file_path, msgs in list(issues.items()):
        seen: set[str] = set()
        deduped = []
        for msg in msgs:
            if msg not in seen:
                seen.add(msg)
                deduped.append(msg)
        issues[file_path] = deduped
    return issues


async def _test_suite_from_executor_files(test_writer: TestWriter, generated_files) -> TestSuite:
    """Build a TestSuite directly from executor-generated test files — no LLM call needed."""
    existing = {
        f.file_path: f.content
        for f in generated_files
        if _is_test_file(f.file_path)
    }
    commands = test_writer.build_validation_commands(existing.keys())
    framework = test_writer.detect_framework()
    return TestSuite(test_files=existing, validation_commands=commands, framework=framework)


class Orchestrator:
    def __init__(self, workspace: str, config_path: Optional[str] = None):
        self.workspace = workspace
        self.router = LLMRouter(config_path)
        self.checkpoint_manager = CheckpointManager(workspace)
        self.reporter = Reporter(workspace)

    async def run(self, prompt: str, resume: bool = False, max_heals: int = 3) -> PipelineReport:
        """Runs the full pipeline with aggressive parallelism."""
        start_time = time.monotonic()

        report = self.checkpoint_manager.load() if resume else None
        if not report:
            report = PipelineReport(prompt=prompt)
        # Carry forward any traces from a resumed checkpoint; accumulate new ones here
        traces: list[StageTrace] = list(report.stage_traces)
        _lock = WorkspaceLock(self.workspace)
        if not _lock.acquire():
            raise RuntimeError(
                f"Workspace '{self.workspace}' is already locked by another pipeline run. "
                "Wait for it to finish or delete '.codegen_agent/run.lock' if it is stale."
            )

        # Fire checkpoints in background; each _save awaits the previous before firing
        _prev_save: asyncio.Task | None = None

        async def _save(r):
            nonlocal _prev_save
            if _prev_save:
                await _prev_save
            _prev_save = asyncio.create_task(self.checkpoint_manager.asave(r))
        try:
            # ── Stages 1+2+3: PLAN + ARCHITECT + EXECUTE (streamed) ────────────────
            # StreamingPlanArchExecutor overlaps LLM generation with file writing:
            # each node is dispatched the moment its JSON object is parsed from the
            # architect stream, so execution starts before the LLM finishes.
            # If we already have plan+architecture (resume), skip straight to execute.
            if report.plan and report.architecture and not report.execution_result:
                _t0 = time.monotonic()
                _tw0 = time.time()
                print("Stage 3: Executing (resuming from checkpoint)...")
                executor = Executor(self.router.get_client_for_role("executor"), self.workspace)
                report = replace(report, execution_result=await executor.execute(report.architecture))
                await _save(report)
                _t1 = time.monotonic()
                prov, mdl = _role_provider(self.router, "executor")
                traces.append(StageTrace(
                    stage="plan_arch_exec", provider=prov, model=mdl,
                    start_monotonic=_t0, end_monotonic=_t1, duration_seconds=_t1 - _t0,
                    start_unix_ts=_tw0, end_unix_ts=_tw0 + (_t1 - _t0),
                    prompt_chars=0, response_chars=0,
                ))

            elif not report.plan or not report.architecture or not report.execution_result:
                _t0 = time.monotonic()
                _tw0 = time.time()
                print("Stage 1+2+3: Planning, Architecting & Executing (streaming)...")
                stream_exec = StreamingPlanArchExecutor(
                    self.router.get_client_for_role("planner"),
                    Executor(self.router.get_client_for_role("executor"), self.workspace),
                )
                try:
                    plan, architecture, execution_result = await stream_exec.run(prompt)
                    report = replace(report, plan=plan, architecture=architecture,
                                     execution_result=execution_result)
                except Exception as e:
                    print(f"  [StreamExecutor] Failed ({e}), falling back to sequential pipeline.")
                    if not report.plan or not report.architecture:
                        pa = PlannerArchitect(self.router.get_client_for_role("planner"))
                        try:
                            plan, architecture = await pa.plan_and_architect(prompt)
                            report = replace(report, plan=plan, architecture=architecture)
                        except Exception as e2:
                            print(f"  [PlannerArchitect] Failed ({e2}), falling back to two calls.")
                            if not report.plan:
                                planner = Planner(self.router.get_client_for_role("planner"))
                                report = replace(report, plan=await planner.plan(prompt))
                            if not report.architecture:
                                architect = Architect(self.router.get_client_for_role("architect"))
                                report = replace(report, architecture=await architect.architect(report.plan))
                    if not report.execution_result:
                        executor = Executor(self.router.get_client_for_role("executor"), self.workspace)
                        report = replace(report, execution_result=await executor.execute(report.architecture))
                await _save(report)
                _t1 = time.monotonic()
                prov, mdl = _role_provider(self.router, "planner")
                traces.append(StageTrace(
                    stage="plan_arch_exec", provider=prov, model=mdl,
                    start_monotonic=_t0, end_monotonic=_t1, duration_seconds=_t1 - _t0,
                    start_unix_ts=_tw0, end_unix_ts=_tw0 + (_t1 - _t0),
                    prompt_chars=0, response_chars=0,
                ))

            # ── Stages 4+5: DEPENDENCIES + TESTS (parallel) ─────────────────────────
            # Both depend only on execution_result; neither depends on the other.
            if not report.dependency_resolution or not report.test_suite:
                print("Stage 4+5: Dependencies & Tests (parallel)...")

                pending: dict[str, asyncio.Task] = {}

                if not report.dependency_resolution:
                    dep_manager = DependencyManager(
                        self.router.get_client_for_role("executor"), self.workspace
                    )
                    pending["dep"] = asyncio.create_task(
                        dep_manager.resolve_and_install(
                            report.execution_result.generated_files,
                            report.plan,
                            validation_commands=report.architecture.global_validation_commands or [],
                        )
                    )

                if not report.test_suite:
                    test_writer = TestWriter(self.router.get_client_for_role("tester"), self.workspace)
                    source_files = _source_files_for_testing(report.execution_result.generated_files)
                    executor_has_tests = any(
                        _is_test_file(f.file_path)
                        for f in report.execution_result.generated_files
                    )
                    if executor_has_tests and not _tests_need_regeneration(report.execution_result.generated_files):
                        # Executor already wrote tests — don't duplicate with another LLM call
                        print("  [TestWriter] Executor already generated test files. Skipping LLM call.")
                        pending["test"] = asyncio.create_task(
                            _test_suite_from_executor_files(test_writer, report.execution_result.generated_files)
                        )
                    elif executor_has_tests and source_files:
                        print("  [TestWriter] Executor tests look low-signal. Regenerating tests from source files.")
                        pending["test"] = asyncio.create_task(
                            test_writer.generate_test_suite(report.plan, source_files)
                        )
                    elif source_files:
                        pending["test"] = asyncio.create_task(
                            test_writer.generate_test_suite(report.plan, source_files)
                        )

                _t0 = time.monotonic()
                _tw0 = time.time()
                results = await asyncio.gather(*pending.values(), return_exceptions=True)
                _t1 = time.monotonic()
                done = dict(zip(pending.keys(), results))
                if "dep" in pending:
                    prov, mdl = _role_provider(self.router, "executor")
                    traces.append(StageTrace(
                        stage="deps", provider=prov, model=mdl,
                        start_monotonic=_t0, end_monotonic=_t1, duration_seconds=_t1 - _t0,
                        start_unix_ts=_tw0, end_unix_ts=_tw0 + (_t1 - _t0),
                        prompt_chars=0, response_chars=0,
                    ))
                if "test" in pending:
                    prov, mdl = _role_provider(self.router, "tester")
                    traces.append(StageTrace(
                        stage="tests", provider=prov, model=mdl,
                        start_monotonic=_t0, end_monotonic=_t1, duration_seconds=_t1 - _t0,
                        start_unix_ts=_tw0, end_unix_ts=_tw0 + (_t1 - _t0),
                        prompt_chars=0, response_chars=0,
                    ))

                if "dep" in done and not isinstance(done["dep"], Exception):
                    report = replace(report, dependency_resolution=done["dep"])
                if "test" in done and not isinstance(done["test"], Exception):
                    test_suite = done["test"]
                    # Prefer the architect's LLM-generated validation commands over the
                    # heuristic ones from TestWriter — the architect knows the exact stack
                    # (Django, Laravel, Go, Rust, etc.) and already wrote the right commands.
                    arch_cmds = (report.architecture.global_validation_commands or [])
                    if arch_cmds:
                        test_suite = replace(test_suite, validation_commands=arch_cmds)
                        print(f"  [Orchestrator] Using architect-specified validation commands: {arch_cmds}")
                    report = replace(report, test_suite=test_suite)

                # Stage 4 and Stage 5 run in parallel; executor output may not include tests
                # when TestWriter generated them. Re-run conftest bootstrap now with known test paths.
                if report.execution_result and report.test_suite:
                    injected_post_tests = DependencyManager._ensure_conftest(
                        Path(self.workspace).resolve(),
                        report.execution_result.generated_files,
                        extra_test_paths=list(report.test_suite.test_files.keys()),
                    )
                    if injected_post_tests:
                        print(
                            "  [Orchestrator] Wrote conftest.py after test generation "
                            "to stabilize pytest imports."
                        )
                        dep_resolution = report.dependency_resolution
                        dep_payload = dict(dep_resolution) if isinstance(dep_resolution, dict) else {}
                        dep_payload["conftest_injected_post_tests"] = True
                        report = replace(report, dependency_resolution=dep_payload)

                await _save(report)

            # ── Stage 6: HEAL ───────────────────────────────────────────────────────
            if not report.healing_report:
                _t0 = time.monotonic()
                _tw0 = time.time()
                print("Stage 6: Healing...")
                healer = Healer(
                    self.router.get_client_for_role("healer"),
                    self.workspace,
                    max_attempts=max_heals,
                )
                _validation_cmds = (
                    report.test_suite.validation_commands if report.test_suite else []
                )
                static_attempts = []

                # Static consistency fixes burn an LLM call per file — only worthwhile
                # when (a) there is heal budget and (b) tests are actually failing.
                # Pre-flight run costs only a subprocess; avoids wasted LLM calls on
                # already-passing code or when the user set max_heals=0 (benchmark mode).
                if max_heals > 0:
                    _static_needed = True
                    if _validation_cmds:
                        _pre_results = await asyncio.gather(
                            *[asyncio.to_thread(run_shell_command, cmd, cwd=self.workspace)
                              for cmd in _validation_cmds]
                        )
                        if all(r.exit_code == 0 for r in _pre_results):
                            _static_needed = False
                            print(
                                "  [Orchestrator] Pre-heal test run: all tests pass"
                                " — skipping static consistency checks."
                            )
                    if _static_needed:
                        consistency_issues = _collect_python_consistency_issues(
                            report.execution_result.generated_files
                        )
                        if consistency_issues:
                            for fp, msgs in list(consistency_issues.items())[:5]:
                                print(f"  [Orchestrator] Static issue in {fp!r}: {msgs[0]}")
                            if len(consistency_issues) > 5:
                                print(
                                    f"  [Orchestrator] ... and {len(consistency_issues) - 5}"
                                    " more file(s) with issues."
                                )
                            print(
                                "  [Orchestrator] Applying targeted source fixes"
                                " before test healing."
                            )
                            static_attempts = await healer.heal_static_issues(
                                consistency_issues, attempt_number=0
                            )

                healing_report = await healer.heal(_validation_cmds)
                _t1 = time.monotonic()
                if static_attempts:
                    healing_report = replace(
                        healing_report,
                        attempts=static_attempts + healing_report.attempts,
                    )
                report = replace(report, healing_report=healing_report)
                await _save(report)
                prov, mdl = _role_provider(self.router, "healer")
                traces.append(StageTrace(
                    stage="heal", provider=prov, model=mdl,
                    start_monotonic=_t0, end_monotonic=_t1, duration_seconds=_t1 - _t0,
                    start_unix_ts=_tw0, end_unix_ts=_tw0 + (_t1 - _t0),
                    prompt_chars=0, response_chars=0,
                ))

            # ── Stages 7+8: QA + VISUAL (parallel) ──────────────────────────────────
            qa_task = None
            visual_task = None

            if not report.qa_report:
                print("Stage 7: QA Auditing...")
                qa_task = asyncio.create_task(
                    QAAuditor(
                        self.router.get_client_for_role("qa_auditor"),
                        self.workspace,
                    ).audit(report)
                )

            if not report.visual_audit and report.plan and report.plan.entry_point.endswith(".html"):
                print("Stage 8: Visual Validation...")
                visual_task = asyncio.create_task(
                    VisualValidator(self.router.get_client_for_role("executor"), self.workspace)
                    .validate(report.plan.project_name, report.plan.entry_point)
                )

            _t0_qa = time.monotonic()
            _tw0_qa = time.time()
            if qa_task:
                report = replace(report, qa_report=await qa_task)
                _t1_qa = time.monotonic()
                prov, mdl = _role_provider(self.router, "qa_auditor")
                traces.append(StageTrace(
                    stage="qa", provider=prov, model=mdl,
                    start_monotonic=_t0_qa, end_monotonic=_t1_qa, duration_seconds=_t1_qa - _t0_qa,
                    start_unix_ts=_tw0_qa, end_unix_ts=_tw0_qa + (_t1_qa - _t0_qa),
                    prompt_chars=0, response_chars=0,
                ))
                await _save(report)

            _t0_vis = time.monotonic()
            _tw0_vis = time.time()
            if visual_task:
                report = replace(report, visual_audit=await visual_task)
                _t1_vis = time.monotonic()
                prov, mdl = _role_provider(self.router, "executor")
                traces.append(StageTrace(
                    stage="visual", provider=prov, model=mdl,
                    start_monotonic=_t0_vis, end_monotonic=_t1_vis, duration_seconds=_t1_vis - _t0_vis,
                    start_unix_ts=_tw0_vis, end_unix_ts=_tw0_vis + (_t1_vis - _t0_vis),
                    prompt_chars=0, response_chars=0,
                ))
                await _save(report)

            report = replace(
                report,
                wall_clock_seconds=time.monotonic() - start_time,
                stage_traces=traces,
            )
            if _prev_save:
                await _prev_save
            await self.checkpoint_manager.asave(report)
            self.reporter.save_report(report)
            return report
        finally:
            _lock.release()
