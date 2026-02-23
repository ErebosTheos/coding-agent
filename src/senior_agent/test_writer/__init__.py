from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Iterable

from senior_agent.llm_client import LLMClient
from senior_agent.models import ImplementationPlan
from senior_agent.patterns import CODE_FENCE_PATTERN


@dataclass
class TestWriter:
    """Generate test files for newly planned source files."""

    # Prevent pytest from misclassifying this dataclass as a test container.
    __test__: ClassVar[bool] = False

    llm_client: LLMClient
    workspace: str | Path = "."
    default_framework: str = "unittest"

    def generate_test_suite(
        self,
        plan: ImplementationPlan,
        files_content: dict[str, str],
    ) -> dict[str, str]:
        framework = self.detect_framework()
        generated: dict[str, str] = {}

        for raw_source_path in plan.new_files:
            source_path = raw_source_path.strip()
            if not source_path:
                continue

            test_path = self._test_path_for_source(Path(source_path))
            prompt = self._build_test_prompt(
                plan=plan,
                framework=framework,
                source_path=source_path,
                source_content=files_content.get(source_path, ""),
                test_path=test_path,
            )

            response = self.llm_client.generate_fix(prompt)
            normalized = self._normalize_generated_content(response)
            if not normalized.strip():
                raise ValueError(f"LLM returned empty test content for {test_path}.")
            if not normalized.endswith("\n"):
                normalized = f"{normalized}\n"
            generated[test_path] = normalized

        return generated

    def build_validation_commands(self, test_files: Iterable[str]) -> list[str]:
        framework = self.detect_framework()
        unique_files = [
            path.strip()
            for path in dict.fromkeys(test_files)
            if isinstance(path, str) and path.strip()
        ]
        if not unique_files:
            return []

        if framework == "pytest":
            return [f"pytest {shlex.quote(path)}" for path in unique_files]

        if framework == "jest":
            return [f"npx jest {shlex.quote(path)}" for path in unique_files]

        commands: list[str] = []
        for test_file in unique_files:
            test_path = Path(test_file)
            parent = str(test_path.parent) if str(test_path.parent) != "." else "tests"
            commands.append(
                "python -m unittest discover -s "
                f"{shlex.quote(parent)} -p {shlex.quote(test_path.name)}"
            )
        return commands

    def detect_framework(self) -> str:
        workspace_root = Path(self.workspace).resolve()

        if self._has_jest_config(workspace_root):
            return "jest"
        if self._has_pytest_config(workspace_root):
            return "pytest"
        if (workspace_root / "tests").exists():
            return "unittest"
        return self.default_framework

    @staticmethod
    def _test_path_for_source(source_path: Path) -> str:
        suffix = source_path.suffix.lower()
        stem = source_path.stem

        if suffix == ".py":
            return f"tests/test_{stem}.py"
        if suffix in {".ts", ".tsx", ".js", ".jsx"}:
            return f"tests/{stem}.test{suffix}"
        if suffix == ".go":
            return f"tests/{stem}_test.go"
        return f"tests/test_{stem}{suffix or '.txt'}"

    @staticmethod
    def _build_test_prompt(
        *,
        plan: ImplementationPlan,
        framework: str,
        source_path: str,
        source_content: str,
        test_path: str,
    ) -> str:
        steps = "\n".join(f"- {step}" for step in plan.steps) or "- No explicit steps provided."
        guidance = plan.design_guidance or "No additional design guidance provided."
        return (
            "Role: Senior Lead Developer.\n"
            "Task: Generate a production-quality test file for a new feature file.\n"
            "Output constraints: Return ONLY raw test file content. No markdown fences.\n"
            "Quality bar: include one happy-path test and at least two edge-case tests.\n"
            "Use deterministic tests with no network calls.\n\n"
            f"Testing Framework: {framework}\n"
            f"Feature: {plan.feature_name}\n"
            f"Feature Summary: {plan.summary}\n"
            f"Design Guidance: {guidance}\n"
            "Plan Steps:\n"
            f"{steps}\n\n"
            f"Target Source File: {source_path}\n"
            f"Target Test File: {test_path}\n"
            "Current Source Content:\n"
            "--- BEGIN SOURCE ---\n"
            f"{source_content}\n"
            "--- END SOURCE ---\n"
        )

    @staticmethod
    def _normalize_generated_content(raw_output: str) -> str:
        stripped = raw_output.strip()
        match = CODE_FENCE_PATTERN.search(stripped)
        if match:
            return match.group("code").strip()
        return stripped

    @staticmethod
    def _has_jest_config(workspace_root: Path) -> bool:
        jest_markers = (
            "jest.config.js",
            "jest.config.cjs",
            "jest.config.mjs",
            "jest.config.ts",
        )
        if any((workspace_root / marker).exists() for marker in jest_markers):
            return True

        package_json = workspace_root / "package.json"
        if not package_json.exists():
            return False

        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False

        if not isinstance(payload, dict):
            return False
        if "jest" in payload:
            return True
        scripts = payload.get("scripts")
        if isinstance(scripts, dict):
            return any(
                isinstance(value, str) and "jest" in value.lower()
                for value in scripts.values()
            )
        return False

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
                pass

        pyproject = workspace_root / "pyproject.toml"
        if pyproject.exists():
            try:
                if "[tool.pytest.ini_options]" in pyproject.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                pass

        return False


__all__ = ["TestWriter"]
