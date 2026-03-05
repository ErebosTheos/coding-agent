"""Tests for TestIntegrityGuard — assertion-free test detection."""
from codegen_agent.orchestrator import _test_has_no_assertions, _tests_need_regeneration
from codegen_agent.models import GeneratedFile


def _gf(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(file_path=path, content=content, node_id="n", sha256="x")


# ── _test_has_no_assertions ───────────────────────────────────────────────────

def test_returns_true_when_test_has_no_assert():
    code = "def test_foo():\n    x = 1 + 1\n"
    assert _test_has_no_assertions(code) is True


def test_returns_false_when_test_has_assert():
    code = "def test_foo():\n    assert 1 + 1 == 2\n"
    assert _test_has_no_assertions(code) is False


def test_returns_false_for_pytest_raises():
    code = (
        "import pytest\n"
        "def test_err():\n"
        "    with pytest.raises(ValueError):\n"
        "        raise ValueError('boom')\n"
    )
    assert _test_has_no_assertions(code) is False


def test_returns_false_for_raise():
    code = "def test_unconditional_raise():\n    raise AssertionError('nope')\n"
    assert _test_has_no_assertions(code) is False


def test_ignores_helper_functions():
    """Non-test_ functions without assert should not trigger the guard."""
    code = "def helper():\n    x = 1\n"
    assert _test_has_no_assertions(code) is False


def test_returns_false_for_syntax_error():
    """Broken files should not crash the guard."""
    assert _test_has_no_assertions("def test_foo(:\n    pass\n") is False


def test_async_test_with_no_assert():
    code = "async def test_async():\n    await something()\n"
    assert _test_has_no_assertions(code) is True


def test_async_test_with_assert():
    code = "async def test_async():\n    assert await something() == 1\n"
    assert _test_has_no_assertions(code) is False


# ── _tests_need_regeneration integration ─────────────────────────────────────

def test_regeneration_triggered_by_assertion_free_test():
    src = _gf("src/main.py", "def add(a, b): return a + b\n")
    test_f = _gf(
        "tests/test_main.py",
        "from src.main import add\ndef test_add():\n    x = add(1, 2)\n",  # no assert
    )
    assert _tests_need_regeneration([src, test_f]) is True


def test_regeneration_not_triggered_when_asserts_present():
    src = _gf("src/main.py", "def add(a, b): return a + b\n")
    test_f = _gf(
        "tests/test_main.py",
        "from src.main import add\ndef test_add():\n    assert add(1, 2) == 3\n",
    )
    assert _tests_need_regeneration([src, test_f]) is False
