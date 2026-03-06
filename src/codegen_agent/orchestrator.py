import ast
import os
import re
import time
import asyncio
from pathlib import Path
from typing import Optional
from dataclasses import replace
from .models import PipelineReport, TestSuite, StageTrace, HealingReport
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
from .live_guard import post_execution_guard
from .context_builder import ProjectContextBuilder
from .startup_guard import detect_entry_point, build_import_check_command


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


def _test_has_no_assertions(content: str) -> bool:
    """True if any test_ function in the file has no assert, raise, or pytest.raises."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        has_signal = any(
            isinstance(n, (ast.Assert, ast.Raise))
            or (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Attribute)
                and n.value.func.attr in {"raises", "fail", "assertRaises", "warns"}
            )
            for n in ast.walk(node)
        )
        if not has_signal:
            return True
    return False


def _infer_validation_commands(generated_files) -> list[str]:
    """Infer reasonable validation commands from file extensions when none are specified."""
    exts: set[str] = set()
    top_level: set[str] = set()
    for f in generated_files:
        exts.add(os.path.splitext(f.file_path)[1].lower())
        if "/" not in f.file_path.replace("\\", "/"):
            top_level.add(f.file_path.lower())

    cmds: list[str] = []
    if ".py" in exts:
        cmds.append("pytest --tb=short -q")
    if exts & {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}:
        if "package.json" in top_level:
            cmds.append("npm test --if-present")
        else:
            cmds.append("node --test")
    if ".go" in exts:
        cmds.append("go test ./...")
    if ".rs" in exts:
        cmds.append("cargo test")
    if ".rb" in exts:
        cmds.append("bundle exec rspec --format progress")
    if ".php" in exts:
        cmds.append("./vendor/bin/phpunit")
    return cmds


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
        # TestIntegrityGuard: reject tests whose functions have no assertions
        if _test_has_no_assertions(f.content):
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
    module_parse_failed: set[str] = set()
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
            if module_name:
                module_parse_failed.add(module_name)
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
                # If the module exists but has syntax errors, avoid cascading false
                # "missing module/symbol" issues that strip valid imports.
                if target_module in module_parse_failed:
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


def _internal_module_exists(workspace: str, module_name: str) -> bool:
    if not module_name:
        return False
    module_rel = Path(*module_name.split("."))
    py_file = (Path(workspace) / module_rel).with_suffix(".py")
    pkg_init = Path(workspace) / module_rel / "__init__.py"
    return py_file.exists() or pkg_init.exists()


def _fix_missing_import_symbols(
    issues: dict[str, list[str]],
    workspace: str,
) -> list[str]:
    """Deterministically remove missing imported symbols from Python files.

    Handles two issue patterns produced by _collect_python_consistency_issues:
      - "Imports missing symbol 'X' from 'Y'" → remove X from the from-import line
      - "Imports from missing internal module 'Y'" → remove the entire import line

    Requires no LLM call — safe to run at max_heals=0. Returns fixed file paths.
    """
    fixed: list[str] = []
    for file_path, msgs in issues.items():
        full_path = Path(workspace) / file_path
        if not full_path.exists():
            continue

        # Collect what to remove for this file
        bad_symbols: set[str] = set()       # individual symbol names
        bad_mod_parts: set[str] = set()     # last component of missing module paths

        for msg in msgs:
            m = re.match(r"Imports missing symbol '([^']+)' from '", msg)
            if m:
                bad_symbols.add(m.group(1))
                continue
            m = re.match(r"Imports from missing internal module '([^']+)'", msg)
            if m:
                missing_module = m.group(1)
                # Safety: if a module file exists on disk, don't strip imports.
                # It may simply be temporarily unparsable and will be healed later.
                if _internal_module_exists(workspace, missing_module):
                    continue
                # Use the last dotted component so both absolute and relative
                # import forms can be matched (e.g. 'src.auth' → 'auth').
                bad_mod_parts.add(missing_module.split(".")[-1])

        if not bad_symbols and not bad_mod_parts:
            continue

        lines = full_path.read_text(encoding="utf-8").split("\n")
        new_lines: list[str] = []

        for line in lines:
            from_m = re.match(r'^(from\s+(\S+)\s+import\s+)(.+)$', line)
            if not from_m:
                new_lines.append(line)
                continue

            import_prefix = from_m.group(1)   # "from ..auth import "
            from_module   = from_m.group(2)   # "..auth" or "src.auth"
            names_str     = from_m.group(3)   # "authenticate_user, create_access_token"

            # Check if the whole import line should be dropped (missing module)
            mod_components = set(re.split(r'[\.\s]+', from_module))
            if mod_components & bad_mod_parts:
                continue  # drop the entire line

            # Remove individual bad symbols from the name list
            names = [n.strip() for n in names_str.split(",") if n.strip()]
            kept  = [n for n in names if n not in bad_symbols]

            if not kept:
                continue  # all names were bad → drop the line
            if len(kept) == len(names):
                new_lines.append(line)  # nothing changed
            else:
                new_lines.append(import_prefix + ", ".join(kept))

        new_content = "\n".join(new_lines)
        original    = "\n".join(lines)
        if new_content != original:
            full_path.write_text(new_content, encoding="utf-8")
            fixed.append(file_path)

    return fixed


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
        self.skip_qa: bool = False  # set by dashboard to skip internal QA

    async def run(self, prompt: str, resume: bool = False, max_heals: int = 3) -> PipelineReport:
        """Runs the full pipeline with aggressive parallelism."""
        start_time = time.monotonic()

        report = self.checkpoint_manager.load() if resume else None
        if not report:
            report = PipelineReport(prompt=prompt)
        # Carry forward any traces from a resumed checkpoint; accumulate new ones here
        traces: list[StageTrace] = list(report.stage_traces)
        _micro_heal_attempts: list = []
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
                _pcb_resume = ProjectContextBuilder(self.workspace)
                _pcb_resume.build_from_architecture(report.architecture)
                executor = Executor(self.router.get_client_for_role("executor"), self.workspace, tier_clients=self.router.get_tier_clients("executor"))
                _exec_result = await executor.execute(report.architecture)
                _pcb_resume.build_from_generated_files(_exec_result.generated_files)
                report = replace(report, execution_result=_exec_result)
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
                    Executor(self.router.get_client_for_role("executor"), self.workspace,
                             tier_clients=self.router.get_tier_clients("executor")),
                )
                try:
                    plan, architecture, execution_result = await stream_exec.run(prompt)
                    _pcb = ProjectContextBuilder(self.workspace)
                    _pcb.build_from_architecture(architecture)
                    _pcb.build_from_generated_files(execution_result.generated_files)
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
                        _pcb_fb = ProjectContextBuilder(self.workspace)
                        if report.architecture:
                            _pcb_fb.build_from_architecture(report.architecture)
                        executor = Executor(self.router.get_client_for_role("executor"), self.workspace, tier_clients=self.router.get_tier_clients("executor"))
                        _fb_result = await executor.execute(report.architecture)
                        _pcb_fb.build_from_generated_files(_fb_result.generated_files)
                        report = replace(report, execution_result=_fb_result)
                await _save(report)
                _t1 = time.monotonic()
                prov, mdl = _role_provider(self.router, "planner")
                traces.append(StageTrace(
                    stage="plan_arch_exec", provider=prov, model=mdl,
                    start_monotonic=_t0, end_monotonic=_t1, duration_seconds=_t1 - _t0,
                    start_unix_ts=_tw0, end_unix_ts=_tw0 + (_t1 - _t0),
                    prompt_chars=0, response_chars=0,
                ))

            # ── LiveGuard Tier 2: Post-execution micro-heal ─────────────────────────
            # Fires after all files are written, before deps/tests. Deterministic-first,
            # then capped LLM micro-heals. Keeps Stage 6 as the final safety net.
            # Guard: skip on any resume where Stage 6 has already run to avoid
            # re-editing healed files and introducing nondeterministic behaviour.
            if (
                report.execution_result
                and report.execution_result.generated_files
                and not report.healing_report
            ):
                _t0_mg = time.monotonic()
                print("LiveGuard: Post-execution integrity check...")
                _healer_mg = Healer(
                    self.router.get_client_for_role("healer"),
                    self.workspace,
                    max_attempts=1,  # micro-heal only — Stage 6 does full healing
                )
                _lg_max_env = os.environ.get("CODEGEN_LIVE_GUARD_MAX_LLM", "10").strip()
                _lg_max_llm = int(_lg_max_env) if _lg_max_env.lstrip("-").isdigit() else 10
                _micro_heal_attempts = await post_execution_guard(
                    report.execution_result.generated_files,
                    self.workspace,
                    _healer_mg,
                    max_llm_calls=_lg_max_llm,
                )
                print(
                    f"  [LiveGuard] Done in {time.monotonic() - _t0_mg:.1f}s "
                    f"({len(_micro_heal_attempts)} LLM micro-heal(s))"
                )
                # Refresh in-memory GeneratedFile.content for any file that LiveGuard
                # modified on disk (deterministic or LLM), so downstream stages
                # (test-gen, Stage 6 consistency checks) see the updated source.
                _refreshed: list = []
                _any_refreshed = False
                for _gf in report.execution_result.generated_files:
                    _disk = Path(self.workspace) / _gf.file_path
                    if _disk.exists():
                        _new_content = _disk.read_text(encoding="utf-8")
                        if _new_content != _gf.content:
                            _refreshed.append(replace(_gf, content=_new_content))
                            _any_refreshed = True
                            continue
                    _refreshed.append(_gf)
                if _any_refreshed:
                    report = replace(
                        report,
                        execution_result=replace(
                            report.execution_result, generated_files=_refreshed
                        ),
                    )

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
                _validation_cmds = list(
                    report.test_suite.validation_commands if report.test_suite else []
                )
                # ── ValidationCommandGuard: infer commands when none specified ────
                # Prevents silent skip of Stage 6 on projects where the architect
                # omitted validation commands and TestWriter produced no suite.
                if not _validation_cmds and report.execution_result:
                    _inferred = _infer_validation_commands(
                        report.execution_result.generated_files
                    )
                    if _inferred:
                        print(
                            f"  [ValidationCommandGuard] No validation commands found."
                            f" Inferred from stack: {_inferred}"
                        )
                        _validation_cmds = _inferred
                # ── StartupLifespanGuard: inject import smoke-check ──────────────
                # Runs `python -c "import <entry_point>"` before pytest so import
                # errors and module-level TypeErrors surface as concrete healer
                # input rather than being buried in a pytest collection failure.
                _entry_point = detect_entry_point(
                    report.execution_result.generated_files, self.workspace
                )
                if _entry_point:
                    _import_cmd = build_import_check_command(_entry_point, workspace=self.workspace)
                    _validation_cmds = [_import_cmd] + _validation_cmds
                    print(f"  [StartupGuard] Import smoke-check: {_import_cmd}")
                static_attempts = []

                # ── Step 1: Deterministic import cleanup (no LLM, always runs) ──────
                # Removes dead/missing symbol imports found by static analysis.
                # Free to run even at max_heals=0 — no LLM cost, instant, safe.
                _det_issues = _collect_python_consistency_issues(
                    report.execution_result.generated_files
                )
                if _det_issues:
                    _det_fixed = _fix_missing_import_symbols(_det_issues, self.workspace)
                    if _det_fixed:
                        print(
                            f"  [Orchestrator] Deterministic import fix applied to"
                            f" {len(_det_fixed)} file(s): {', '.join(_det_fixed)}"
                        )

                # ── Step 2: Pre-flight + LLM static fixes ───────────────────────────
                # Pre-flight always runs when validation commands exist — regardless
                # of max_heals. This gives accurate success reporting even at
                # max_heals=0 (previously always returned success=False) and enables
                # the short-circuit for every caller, not just max_heals > 0.
                _preflight_all_passed = False
                _pre_results: list = []
                if _validation_cmds:
                    _pre_results = await asyncio.gather(
                        *[asyncio.to_thread(run_shell_command, cmd, cwd=self.workspace)
                          for cmd in _validation_cmds]
                    )
                    if all(r.exit_code == 0 for r in _pre_results):
                        _preflight_all_passed = True
                        print(
                            "  [Orchestrator] Pre-heal test run: all tests pass"
                            " — skipping heal loop."
                        )

                # LLM static fixes: only when there is budget AND tests failed.
                if max_heals > 0 and not _preflight_all_passed:
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

                # Short-circuit: pre-flight confirmed all tests pass — build a
                # success report directly so healer.heal() doesn't re-run the
                # test suite. Applies at any max_heals value.
                if _preflight_all_passed:
                    healing_report = HealingReport(
                        success=True,
                        attempts=[],
                        final_command_result=_pre_results[-1] if _pre_results else None,
                    )
                    report = replace(report, first_pass_success=True)
                else:
                    healing_report = await healer.heal(_validation_cmds)
                _t1 = time.monotonic()
                all_pre_attempts = _micro_heal_attempts + static_attempts
                if all_pre_attempts:
                    healing_report = replace(
                        healing_report,
                        attempts=all_pre_attempts + healing_report.attempts,
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

            if not report.qa_report and not self.skip_qa:
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

            _QA_TIMEOUT = int(os.environ.get("CODEGEN_QA_TIMEOUT", "120"))
            _t0_qa = time.monotonic()
            _tw0_qa = time.time()
            if qa_task:
                try:
                    qa_result = await asyncio.wait_for(qa_task, timeout=_QA_TIMEOUT)
                except asyncio.TimeoutError:
                    print(f"  [QA] Timed out after {_QA_TIMEOUT}s — skipping QA report.")
                    qa_result = None
                if qa_result is not None:
                    report = replace(report, qa_report=qa_result)
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
