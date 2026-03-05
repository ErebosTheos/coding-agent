"""Tests for EncodingGuard — BOM, zero-width chars, and CRLF normalization."""
import os
from codegen_agent.executor import _normalize_encoding, _validate_write_path


# ── _normalize_encoding ───────────────────────────────────────────────────────

def test_strips_bom():
    assert _normalize_encoding("\ufeffhello") == "hello"


def test_strips_zero_width_space():
    assert _normalize_encoding("hel\u200blo") == "hello"


def test_strips_zwnj_and_zwj():
    assert _normalize_encoding("a\u200cb\u200dc") == "abc"


def test_normalizes_crlf():
    assert _normalize_encoding("line1\r\nline2") == "line1\nline2"


def test_normalizes_lone_cr():
    assert _normalize_encoding("line1\rline2") == "line1\nline2"


def test_passthrough_clean_content():
    text = "def foo():\n    return 1\n"
    assert _normalize_encoding(text) == text


def test_applies_to_non_python_files():
    """EncodingGuard is file-type agnostic — should clean JS, YAML, etc."""
    content = "\ufeff{\"key\": \"value\"\r\n}"
    assert _normalize_encoding(content) == "{\"key\": \"value\"\n}"


# ── _validate_write_path ──────────────────────────────────────────────────────

def test_accepts_valid_relative_path(tmp_path):
    assert _validate_write_path(str(tmp_path), "src/main.py") is None


def test_rejects_absolute_path(tmp_path):
    err = _validate_write_path(str(tmp_path), "/etc/passwd")
    assert err is not None
    assert "absolute" in err.lower()


def test_rejects_path_traversal(tmp_path):
    err = _validate_write_path(str(tmp_path), "../outside.py")
    assert err is not None
    assert "traversal" in err.lower()


def test_rejects_null_byte(tmp_path):
    err = _validate_write_path(str(tmp_path), "src/ma\x00in.py")
    assert err is not None


def test_rejects_empty_path(tmp_path):
    err = _validate_write_path(str(tmp_path), "")
    assert err is not None


def test_accepts_nested_valid_path(tmp_path):
    assert _validate_write_path(str(tmp_path), "a/b/c/deep.py") is None


def test_rejects_double_dot_in_middle(tmp_path):
    err = _validate_write_path(str(tmp_path), "src/../../etc/passwd")
    assert err is not None
