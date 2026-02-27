from codegen_agent.models import GeneratedFile
from codegen_agent.orchestrator import (
    _collect_python_consistency_issues,
    _tests_need_regeneration,
)


def _gf(path: str, content: str, node_id: str) -> GeneratedFile:
    return GeneratedFile(file_path=path, content=content, node_id=node_id, sha256="x")


def test_collect_python_consistency_issues_detects_missing_symbol_import():
    files = [
        _gf("app/logic.py", "def add(a, b):\n    return a + b\n", "logic"),
        _gf("app/main.py", "from app.logic import calculate\n", "main"),
    ]

    issues = _collect_python_consistency_issues(files)

    assert "app/main.py" in issues
    assert any("missing symbol 'calculate'" in msg for msg in issues["app/main.py"])


def test_collect_python_consistency_issues_detects_missing_module():
    files = [
        _gf("app/main.py", "import app.missing_module\n", "main"),
    ]

    issues = _collect_python_consistency_issues(files)

    assert "app/main.py" in issues
    assert any("missing internal module" in msg for msg in issues["app/main.py"])


def test_tests_need_regeneration_for_hypothetical_mock_tests():
    files = [
        _gf("app/logic.py", "def add(a, b):\n    return a + b\n", "logic"),
        _gf(
            "app/tests/test_logic.py",
            "class MockThing:\n    pass\n# in a real scenario we would do more\n",
            "test",
        ),
    ]

    assert _tests_need_regeneration(files) is True


def test_tests_need_regeneration_false_for_real_module_imports():
    files = [
        _gf("app/logic.py", "def add(a, b):\n    return a + b\n", "logic"),
        _gf(
            "app/tests/test_logic.py",
            "from app.logic import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            "test",
        ),
    ]

    assert _tests_need_regeneration(files) is False
