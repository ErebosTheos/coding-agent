from __future__ import annotations
import json
import shlex
import os
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Iterable, Dict, List
from .models import Plan, TestSuite
from .llm.protocol import LLMClient
from .utils import extract_code_from_markdown, prune_prompt
from .executor import _BulkFileParser, _fix_utcnow, _fix_httpx_async_transport, _fix_orm_sessionmaker

@dataclass
class TestWriter:
    """Generate test files for newly planned source files."""
    __test__: ClassVar[bool] = False

    llm_client: LLMClient
    workspace: str | Path = "."
    default_framework: str = "pytest"
    max_batch_size: int = 20  # Increased from 5: less LLM calls, faster generation

    async def generate_test_suite(
        self,
        plan: Plan,
        files_content: Dict[str, str],
    ) -> TestSuite:
        framework = self.detect_framework()
        n = len(files_content)

        # Stream-bulk: one LLM call for all test files, written as JSON values arrive.
        # Gives all source files as shared context → consistent imports and fixtures.
        # Falls back to regular bulk (no streaming) or parallel single-file calls.
        if hasattr(self.llm_client, "astream"):
            print(f"  [TestWriter] Generating {n} test file(s) via stream-bulk.")
            generated_files = await self._generate_stream_bulk(plan, framework, files_content)
        elif n <= self.max_batch_size:
            print(f"  [TestWriter] Small project ({n} files). Using bulk test generation.")
            generated_files = await self._generate_bulk(plan, framework, files_content)
        else:
            print(f"  [TestWriter] Large project ({n} files). Generating tests in parallel.")
            tasks = [
                self._generate_single_test(plan, framework, sp, c)
                for sp, c in files_content.items()
            ]
            results = await asyncio.gather(*tasks)
            generated_files = {tp: tc for tp, tc in results if tc}

        validation_commands = self.build_validation_commands(generated_files.keys())
        return TestSuite(
            test_files=generated_files,
            validation_commands=validation_commands,
            framework=framework,
        )

    async def _generate_stream_bulk(
        self, plan: Plan, framework: str, files_content: Dict[str, str]
    ) -> Dict[str, str]:
        """One streaming LLM call for all test files — writes each file as its JSON value
        arrives in the stream. Falls back to _generate_bulk if stream produces incomplete JSON."""
        prompt = self._build_bulk_test_prompt(plan, framework, files_content)
        prompt = prune_prompt(prompt, max_chars=28_000)

        parser = _BulkFileParser()
        generated_files: Dict[str, str] = {}

        async for chunk in self.llm_client.astream(prompt):
            for test_path, content in parser.feed(chunk):
                if not content.strip():
                    continue
                content = self._apply_test_guardrails(test_path, content)
                full_path = os.path.join(self.workspace, test_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w") as f:
                    f.write(content)
                print(f"  [TestWriter] Created test file: {test_path}")
                generated_files[test_path] = content

        if not generated_files:
            print("  [TestWriter] Stream-bulk produced no files. Falling back to bulk.")
            return await self._generate_bulk(plan, framework, files_content)

        return generated_files

    async def _generate_bulk(self, plan: Plan, framework: str, files_content: Dict[str, str]) -> Dict[str, str]:
        """Generates all tests in a single LLM call."""
        prompt = self._build_bulk_test_prompt(plan, framework, files_content)
        
        response = await self.llm_client.generate(prompt)
        
        json_blocks = extract_code_from_markdown(response, "json")
        try:
            if json_blocks:
                data = json.loads(json_blocks[0])
            else:
                data = json.loads(response)
        except json.JSONDecodeError:
            print("  [TestWriter] Bulk test generation failed to return valid JSON. Falling back to parallel.")
            # Simple parallel fallback
            tasks = [self._generate_single_test(plan, framework, sp, c) for sp, c in files_content.items()]
            results = await asyncio.gather(*tasks)
            return {tp: tc for tp, tc in results if tc}

        generated_files = {}
        for test_path, content in data.items():
            content = self._apply_test_guardrails(test_path, content)
            full_test_path = os.path.join(self.workspace, test_path)
            os.makedirs(os.path.dirname(full_test_path), exist_ok=True)
            with open(full_test_path, 'w') as f:
                f.write(content)
            print(f"  [TestWriter] Created test file: {test_path}")
            generated_files[test_path] = content

        return generated_files

    async def _generate_single_test(self, plan: Plan, framework: str, source_path: str, source_content: str) -> tuple[str, str | None]:
        test_path = self._test_path_for_source(Path(source_path))
        prompt = self._build_test_prompt(
            plan=plan,
            framework=framework,
            source_path=source_path,
            source_content=source_content,
            test_path=test_path,
        )

        response = await self.llm_client.generate(prompt)
        normalized = self._normalize_generated_content(response)
        if not normalized.strip():
            return test_path, None

        normalized = self._apply_test_guardrails(test_path, normalized)
        if not normalized.endswith("\n"):
            normalized = f"{normalized}\n"

        full_test_path = os.path.join(self.workspace, test_path)
        os.makedirs(os.path.dirname(full_test_path), exist_ok=True)
        with open(full_test_path, 'w') as f:
            f.write(normalized)
        
        print(f"  [TestWriter] Created test file: {test_path}")
        return test_path, normalized

    def build_validation_commands(self, test_files: Iterable[str]) -> List[str]:
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

        # Generic fallback — still better than `python3 file.py` which skips test discovery
        return [f"python3 -m pytest {shlex.quote(path)}" for path in unique_files]

    def detect_framework(self) -> str:
        if not hasattr(self, '_framework_cache'):
            workspace_root = Path(self.workspace).resolve()
            if self._has_jest_config(workspace_root):
                self._framework_cache = "jest"
            elif self._has_pytest_config(workspace_root):
                self._framework_cache = "pytest"
            else:
                self._framework_cache = self.default_framework
        return self._framework_cache

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
    def _extract_api_surface(content: str, file_path: str) -> str:
        """Extract public API surface (signatures, class defs) instead of full source.
        Reduces LLM prompt size by 60-80% while preserving test-relevant information."""
        ext = os.path.splitext(file_path)[1].lower()
        lines = content.split("\n")
        surface = []

        if ext == ".py":
            for line in lines:
                s = line.strip()
                if (s.startswith("def ") or s.startswith("async def ")
                        or s.startswith("class ") or s.startswith("@")
                        or s.startswith('"""') or s.startswith("'''")):
                    surface.append(line)
        elif ext in {".js", ".ts", ".jsx", ".tsx"}:
            for line in lines:
                s = line.strip()
                if (s.startswith("export ") or s.startswith("function ")
                        or s.startswith("async function") or s.startswith("class ")
                        or s.startswith("const ") or s.startswith("interface ")
                        or s.startswith("type ")):
                    surface.append(line)
        else:
            # Unknown language: take up to 60 lines as-is
            surface = lines[:60]

        return "\n".join(surface[:80]) if surface else content[:2000]

    @staticmethod
    def _build_test_prompt(
        *,
        plan: Plan,
        framework: str,
        source_path: str,
        source_content: str,
        test_path: str,
    ) -> str:
        features = "\n".join(f"- {f.title}: {f.description}" for f in plan.features)
        api_surface = TestWriter._extract_api_surface(source_content, source_path)
        return (
            "Role: Senior Lead Developer.\n"
            "Task: Generate a production-quality test file for a new feature file.\n"
            "Output constraints: Return ONLY raw test file content. No markdown fences.\n"
            "Quality bar: include one happy-path test and at least two edge-case tests.\n"
            "The test MUST exercise the real target module/API; do not invent hypothetical wrappers.\n"
            "Do not write placeholder commentary like 'cannot inspect' or 'in a real scenario'.\n"
            "Use deterministic tests with no network calls.\n\n"
            "CRITICAL test infrastructure rules — violating these breaks the test suite:\n"
            "- SQLAlchemy async fixtures: use `async_sessionmaker` from `sqlalchemy.ext.asyncio`,\n"
            "  NOT `sessionmaker` from `sqlalchemy.orm`. Example:\n"
            "  `from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker`\n"
            "  `SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)`\n"
            "- httpx AsyncClient: NEVER use `AsyncClient(app=app, base_url=...)`. Always use:\n"
            "  `from httpx import AsyncClient, ASGITransport`\n"
            "  `AsyncClient(transport=ASGITransport(app=app), base_url='http://test')`\n"
            "- conftest.py: if the app reads env vars at import time (e.g. SECRET_KEY),\n"
            "  set them with `os.environ.setdefault(...)` BEFORE any `from src.xxx import` line.\n\n"
            f"Testing Framework: {framework}\n"
            f"Project: {plan.project_name}\n"
            f"Features:\n{features}\n"
            f"Tech Stack: {plan.tech_stack}\n"
            f"Target Source File: {source_path}\n"
            f"Target Test File: {test_path}\n"
            "Public API Surface:\n"
            "--- BEGIN API ---\n"
            f"{api_surface}\n"
            "--- END API ---\n"
        )

    @staticmethod
    def _build_bulk_test_prompt(plan: Plan, framework: str, files_content: Dict[str, str]) -> str:
        features = "\n".join(f"- {f.title}: {f.description}" for f in plan.features)
        files_info = []
        for path, content in files_content.items():
            surface = TestWriter._extract_api_surface(content, path)
            files_info.append(f"File: {path}\nAPI Surface:\n{surface}")

        return (
            "Role: Senior Lead Developer.\n"
            "Task: Generate production-quality test files for the following source files.\n"
            "Output format: Return a JSON object where keys are test file paths and values are the test contents.\n"
            "Quality bar: Each test file should include one happy-path and at least two edge-case tests.\n\n"
            "Each test file must import and exercise the real corresponding source module.\n"
            "Do not create fake UI harnesses or hypothetical app classes as substitutes.\n\n"
            "CRITICAL test infrastructure rules — violating these breaks the test suite:\n"
            "- SQLAlchemy async fixtures: use `async_sessionmaker` from `sqlalchemy.ext.asyncio`,\n"
            "  NOT `sessionmaker` from `sqlalchemy.orm`. Example:\n"
            "  `from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker`\n"
            "  `SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)`\n"
            "- httpx AsyncClient: NEVER use `AsyncClient(app=app, base_url=...)`. Always use:\n"
            "  `from httpx import AsyncClient, ASGITransport`\n"
            "  `AsyncClient(transport=ASGITransport(app=app), base_url='http://test')`\n"
            "- conftest.py: if the app reads env vars at import time (e.g. SECRET_KEY),\n"
            "  set them with `os.environ.setdefault(...)` BEFORE any `from src.xxx import` line.\n\n"
            f"Testing Framework: {framework}\n"
            f"Project: {plan.project_name}\n"
            f"Features:\n{features}\n"
            f"Tech Stack: {plan.tech_stack}\n\n"
            "Source Files:\n" + "\n\n".join(files_info)
        )

    @staticmethod
    def _apply_test_guardrails(test_path: str, content: str) -> str:
        """Apply deterministic fixes to generated test files."""
        content = _fix_utcnow(test_path, content)
        content = _fix_httpx_async_transport(test_path, content)
        content = _fix_orm_sessionmaker(test_path, content)
        return content

    @staticmethod
    def _normalize_generated_content(raw_output: str) -> str:
        code_blocks = extract_code_from_markdown(raw_output)
        if code_blocks:
            return code_blocks[0].strip()
        return raw_output.strip()

    @staticmethod
    def _has_jest_config(workspace_root: Path) -> bool:
        jest_markers = ("jest.config.js", "jest.config.cjs", "jest.config.mjs", "jest.config.ts")
        if any((workspace_root / marker).exists() for marker in jest_markers):
            return True
        package_json = workspace_root / "package.json"
        if not package_json.exists():
            return False
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
            if not isinstance(payload, dict): return False
            if "jest" in payload: return True
            scripts = payload.get("scripts")
            if isinstance(scripts, dict):
                return any(isinstance(v, str) and "jest" in v.lower() for v in scripts.values())
        except Exception:
            pass
        return False

    @staticmethod
    def _has_pytest_config(workspace_root: Path) -> bool:
        if (workspace_root / "pytest.ini").exists(): return True
        tox_ini = workspace_root / "tox.ini"
        if tox_ini.exists():
            try:
                if "[pytest]" in tox_ini.read_text(encoding="utf-8"): return True
            except Exception:
                pass
        pyproject = workspace_root / "pyproject.toml"
        if pyproject.exists():
            try:
                if "[tool.pytest.ini_options]" in pyproject.read_text(encoding="utf-8"): return True
            except Exception:
                pass
        return False
