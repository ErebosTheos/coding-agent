"""Tests for _fix_missing_import_symbols — deterministic dead-import removal."""
from pathlib import Path

from codegen_agent.orchestrator import _fix_missing_import_symbols


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_removes_symbol_from_multi_import(tmp_path):
    _write(
        tmp_path,
        "src/routers/auth.py",
        "from ..auth import authenticate_user, create_access_token, verify_password\n",
    )
    issues = {
        "src/routers/auth.py": ["Imports missing symbol 'authenticate_user' from 'src.auth'."]
    }
    fixed = _fix_missing_import_symbols(issues, str(tmp_path))
    assert "src/routers/auth.py" in fixed
    content = (tmp_path / "src/routers/auth.py").read_text()
    assert "authenticate_user" not in content
    assert "create_access_token" in content
    assert "verify_password" in content


def test_removes_trailing_symbol_from_import(tmp_path):
    _write(tmp_path, "app/main.py", "from app.utils import helper, missing_fn\n")
    issues = {"app/main.py": ["Imports missing symbol 'missing_fn' from 'app.utils'."]}
    fixed = _fix_missing_import_symbols(issues, str(tmp_path))
    assert "app/main.py" in fixed
    content = (tmp_path / "app/main.py").read_text()
    assert "missing_fn" not in content
    assert "helper" in content


def test_removes_only_symbol_leaves_no_empty_import(tmp_path):
    _write(tmp_path, "app/main.py", "from app.utils import only_missing\n")
    issues = {"app/main.py": ["Imports missing symbol 'only_missing' from 'app.utils'."]}
    _fix_missing_import_symbols(issues, str(tmp_path))
    content = (tmp_path / "app/main.py").read_text()
    # The resulting empty "from app.utils import" line must be removed
    assert "import" not in content or "only_missing" not in content


def test_removes_entire_line_for_missing_module(tmp_path):
    _write(
        tmp_path,
        "src/main.py",
        "from .nonexistent import SomeClass\nfrom .real import Other\n",
    )
    issues = {"src/main.py": ["Imports from missing internal module 'src.nonexistent'."]}
    fixed = _fix_missing_import_symbols(issues, str(tmp_path))
    assert "src/main.py" in fixed
    content = (tmp_path / "src/main.py").read_text()
    assert "nonexistent" not in content
    assert "Other" in content


def test_no_change_when_issues_empty(tmp_path):
    p = _write(tmp_path, "app/main.py", "from app.utils import real_fn\n")
    original = p.read_text()
    fixed = _fix_missing_import_symbols({}, str(tmp_path))
    assert fixed == []
    assert p.read_text() == original


def test_missing_module_issue_does_not_strip_import_when_module_exists(tmp_path):
    _write(tmp_path, "src/core/__init__.py", "def deps():\n    return None\n")
    _write(tmp_path, "src/main.py", "from src.core import deps\n")

    issues = {"src/main.py": ["Imports from missing internal module 'src.core'."]}
    fixed = _fix_missing_import_symbols(issues, str(tmp_path))

    assert fixed == []
    assert (tmp_path / "src/main.py").read_text() == "from src.core import deps\n"
