"""V2 QA Auditor — scores the generated project against the manifest."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from ..codegen_agent.llm.protocol import LLMClient
from ..codegen_agent.utils import prune_prompt
from .models import GeneratedFile, ProjectManifest

AUDITOR_SYSTEM_PROMPT = """You are a senior code reviewer. Score the generated project from 0-100.

Evaluate:
1. Route coverage (30 pts): do generated routers implement all routes in the manifest?
2. Model correctness (20 pts): do ORM models match manifest columns exactly?
3. Schema correctness (15 pts): do Pydantic schemas match manifest fields?
4. Auth consistency (15 pts): is the JWT sub_field used consistently everywhere?
5. Completeness (20 pts): are all resources fully implemented (no stubs, no TODOs)?

Return JSON only:
{"score": 85, "issues": ["description of issue 1", "description of issue 2"]}"""


class QAAuditorV2:
    def __init__(self, llm: LLMClient, workspace: str) -> None:
        self.llm = llm
        self.workspace = workspace

    async def audit(
        self,
        files: list[GeneratedFile],
        manifest: ProjectManifest,
        validation_commands: list[str],
    ) -> tuple[float, list[str]]:
        """Returns (score 0-100, list of issues)."""
        score = 0.0
        all_issues: list[str] = []

        # 1. Run validation commands
        test_score, test_issues = self._run_tests(validation_commands)
        score += test_score * 0.4  # 40% weight on tests
        all_issues.extend(test_issues)

        # 2. Deterministic checks (60%)
        det_score, det_issues = self._deterministic_checks(files, manifest)
        score += det_score * 0.6
        all_issues.extend(det_issues)

        return round(min(100.0, score), 1), all_issues

    def _run_tests(self, commands: list[str]) -> tuple[float, list[str]]:
        """Run validation commands, return (score, issues)."""
        if not commands:
            return 50.0, []

        # Prefer the project venv's pytest so installed deps are available
        venv_python = os.path.join(self.workspace, ".venv", "bin", "python")
        python_bin  = venv_python if os.path.exists(venv_python) else sys.executable

        issues: list[str] = []
        passed = 0
        for cmd in commands[:2]:
            # Replace bare `pytest` with the venv python -m pytest (quoted for spaces in path)
            quoted_python = f'"{python_bin}"'
            resolved_cmd = cmd.replace("pytest ", f"{quoted_python} -m pytest ", 1)
            if resolved_cmd == cmd and cmd.strip() == "pytest":
                resolved_cmd = f"{quoted_python} -m pytest"
            try:
                result = subprocess.run(
                    resolved_cmd, shell=True, cwd=self.workspace,
                    capture_output=True, text=True, timeout=60,
                    env=dict(os.environ, PYTHONPATH=self.workspace),
                )
                if result.returncode == 0:
                    passed += 1
                else:
                    # Extract first error line
                    stderr = (result.stdout + result.stderr).strip()
                    first_error = next(
                        (l for l in stderr.splitlines() if "ERROR" in l or "FAILED" in l or "error" in l),
                        stderr[:200] if stderr else "command failed"
                    )
                    issues.append(f"Test failure: {first_error}")
            except subprocess.TimeoutExpired:
                issues.append(f"Command timed out: {cmd}")
            except Exception as exc:
                issues.append(f"Command error: {exc}")
        return (passed / max(len(commands), 1)) * 100, issues

    def _deterministic_checks(
        self,
        files: list[GeneratedFile],
        manifest: ProjectManifest,
    ) -> tuple[float, list[str]]:
        """Check manifest compliance without LLM."""
        issues: list[str] = []
        total_checks = 0
        passed_checks = 0

        py_files = {f.file_path: f.content for f in files if f.file_path.endswith(".py")}

        # Check model columns
        for cls_name, model in manifest.models.items():
            total_checks += len(model.columns)
            model_files = {
                p: c for p, c in py_files.items()
                if "model" in p.lower() and cls_name.lower() in c.lower()
            }
            for col_name in model.columns:
                found = any(col_name in c for c in model_files.values())
                if found:
                    passed_checks += 1
                else:
                    issues.append(f"Model {cls_name}: column '{col_name}' not found in any model file")

        # Check route paths
        all_content = "\n".join(py_files.values())
        for route in manifest.routes:
            total_checks += 1
            # Strip api_prefix for matching (routers use sub-paths)
            path_suffix = route.path.replace(manifest.api_prefix, "").strip("/")
            segments = [s for s in path_suffix.split("/") if s and not s.startswith("{")]
            if segments and any(seg in all_content for seg in segments):
                passed_checks += 1
            else:
                issues.append(f"Route {route.method} {route.path} may not be implemented")

        # Check auth sub_field consistency
        auth_files = {p: c for p, c in py_files.items() if "auth" in p.lower() or "security" in p.lower()}
        sub_field = manifest.auth.sub_field
        total_checks += 1
        auth_content = "\n".join(auth_files.values())
        if sub_field in auth_content:
            passed_checks += 1
        else:
            issues.append(f"Auth sub_field '{sub_field}' not found in auth/security files")

        # Check for stub functions in router files
        router_files = {p: c for p, c in py_files.items() if "router" in p.lower() or "api" in p.lower()}
        stub_count = 0
        for path, content in router_files.items():
            try:
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
                        if body and len(body) == 1 and isinstance(body[0], ast.Pass):
                            stub_count += 1
            except SyntaxError:
                issues.append(f"Syntax error in {path}")
        if stub_count > 0:
            issues.append(f"{stub_count} stub endpoint(s) found (pass body) in router files")

        score = (passed_checks / max(total_checks, 1)) * 100
        return score, issues
