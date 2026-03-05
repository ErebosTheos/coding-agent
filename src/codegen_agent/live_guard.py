"""LiveGuard — two-tier integrity for codegen execution.

Tier 1: check_file() — sync, deterministic, zero LLM. Call immediately after each file write.
Tier 2: post_execution_guard() — async, runs after all files written (end of Stage 3).
        Deterministic fixes first, then capped LLM micro-heals for remaining issues.
"""
import ast
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .healer import Healer
    from .models import GeneratedFile, HealAttempt


def check_file(file_path: str, content: str) -> list[str]:
    """Tier 1: instant deterministic check on a single file. Returns issue strings."""
    issues: list[str] = []
    if file_path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as exc:
            issues.append(f"SyntaxError line {exc.lineno}: {exc.msg}")
    return issues


async def post_execution_guard(
    generated_files: list["GeneratedFile"],
    workspace: str,
    healer: "Healer",
    max_llm_calls: int = 10,
) -> list["HealAttempt"]:
    """Tier 2: post-execution guard. Call after Stage 3, before Stage 4+5.

    Step 1  — import symbol cleanup (deterministic, free).
    Step 1b — ORM kwarg alias normalization (deterministic, free).
    Step 2  — LLM micro-heal for remaining issues (capped at max_llm_calls).
    """
    from .orchestrator import (
        _collect_python_consistency_issues,
        _fix_missing_import_symbols,
    )
    from .model_kwarg_guard import (
        extract_model_fields,
        auto_fix_aliases,
        scan_issues as kwarg_scan_issues,
        scan_seed_contract_issues,
        scan_schema_drift_issues,
    )

    def _refresh(files, changed_paths: set[str]):
        """Rebuild GeneratedFile list with fresh disk content for changed files."""
        changed = set(changed_paths)
        return [
            _dc_replace(gf, content=Path(workspace, gf.file_path).read_text(encoding="utf-8"))
            if gf.file_path in changed and Path(workspace, gf.file_path).exists()
            else gf
            for gf in files
        ]

    # ── Step 1: import symbol cleanup ────────────────────────────────────────
    issues = _collect_python_consistency_issues(generated_files)
    if issues:
        fixed = _fix_missing_import_symbols(issues, workspace)
        if fixed:
            print(
                f"  [LiveGuard] Deterministic import fix applied to {len(fixed)} file(s): "
                + ", ".join(fixed)
            )
            generated_files = _refresh(generated_files, set(fixed))

    # ── Step 1b: ORM kwarg alias normalization ────────────────────────────────
    # Renames known field aliases (stock_quantity → stock, etc.) in non-model
    # files before tests run. Free — no LLM, no subprocess.
    model_fields = extract_model_fields(generated_files)
    if model_fields:
        kwarg_fixed = auto_fix_aliases(generated_files, workspace, model_fields)
        if kwarg_fixed:
            print(
                f"  [LiveGuard] ORM alias-fix applied to {len(kwarg_fixed)} file(s): "
                + ", ".join(kwarg_fixed)
            )
            generated_files = _refresh(generated_files, set(kwarg_fixed))

    # ── Step 2: re-check and LLM micro-heal remaining issues ─────────────────
    remaining = _collect_python_consistency_issues(generated_files)

    # Merge remaining ORM kwarg issues and seed contract violations so the
    # LLM micro-heal receives all drift in one structured pass.
    if model_fields:
        for fp, msgs in kwarg_scan_issues(generated_files, model_fields).items():
            remaining.setdefault(fp, []).extend(msgs)
        for fp, msgs in scan_seed_contract_issues(generated_files, model_fields).items():
            remaining.setdefault(fp, []).extend(msgs)
        for fp, msgs in scan_schema_drift_issues(generated_files, model_fields).items():
            remaining.setdefault(fp, []).extend(msgs)

    if not remaining or max_llm_calls <= 0:
        return []

    # Prioritise files with the most issues; cap to budget
    prioritised = dict(
        sorted(remaining.items(), key=lambda kv: len(kv[1]), reverse=True)[:max_llm_calls]
    )
    print(
        f"  [LiveGuard] Micro-healing {len(prioritised)} file(s) "
        f"(budget: {max_llm_calls}): {list(prioritised)}"
    )
    return await healer.heal_static_issues(prioritised, attempt_number=0)
