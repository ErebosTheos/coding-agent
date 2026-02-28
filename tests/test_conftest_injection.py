import tempfile
from pathlib import Path

from codegen_agent.dependency_manager import DependencyManager
from codegen_agent.models import GeneratedFile


def _f(path: str, content: str = "") -> GeneratedFile:
    return GeneratedFile(file_path=path, content=content, node_id="n", sha256="x")


def test_conftest_written_when_root_module_and_test_subdir():
    """Root-level source + test in subdir -> conftest.py created with sys.path fix."""
    with tempfile.TemporaryDirectory() as d:
        files = [_f("prime.py"), _f("tests/test_prime.py")]
        result = DependencyManager._ensure_conftest(Path(d), files)
        assert result is True
        conftest = Path(d) / "conftest.py"
        assert conftest.exists()
        assert "sys.path.insert" in conftest.read_text()


def test_conftest_not_overwritten_when_already_present():
    """Existing conftest.py must not be touched."""
    with tempfile.TemporaryDirectory() as d:
        existing = "# user conftest"
        (Path(d) / "conftest.py").write_text(existing)
        files = [_f("prime.py"), _f("tests/test_prime.py")]
        result = DependencyManager._ensure_conftest(Path(d), files)
        assert result is False
        assert (Path(d) / "conftest.py").read_text() == existing


def test_conftest_not_written_when_tests_at_root():
    """If test file is at root (not a subdir), skip conftest injection."""
    with tempfile.TemporaryDirectory() as d:
        files = [_f("prime.py"), _f("test_prime.py")]
        result = DependencyManager._ensure_conftest(Path(d), files)
        assert result is False
        assert not (Path(d) / "conftest.py").exists()


def test_conftest_written_when_tests_arrive_later():
    """Support Stage 4/5 race: tests may be known only after TestWriter completes."""
    with tempfile.TemporaryDirectory() as d:
        files = [_f("stack.py")]
        result = DependencyManager._ensure_conftest(
            Path(d),
            files,
            extra_test_paths=["tests/test_stack.py"],
        )
        assert result is True
        content = (Path(d) / "conftest.py").read_text()
        assert "import os" in content
        assert "import sys" in content
        assert "if ROOT not in sys.path" in content
