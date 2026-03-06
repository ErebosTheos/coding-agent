"""Tests for cross-project PatternStore."""
import json
import tempfile
from pathlib import Path

import pytest

from codegen_agent.pattern_store import PatternStore


@pytest.fixture
def store(tmp_path):
    return PatternStore(store_path=tmp_path / "patterns.json")


def test_lookup_returns_none_for_unknown_fingerprint(store):
    assert store.lookup("nonexistent") is None


def test_record_and_lookup_roundtrip(store):
    fp = store.fingerprint("BUILD_ERROR", "ModuleNotFoundError: No module named 'auth'")
    store.record(fp, "Added missing import in auth.py")
    assert store.lookup(fp) == "Added missing import in auth.py"


def test_fingerprint_is_stable(store):
    fp1 = store.fingerprint("TEST_FAILURE", "AssertionError: assert 404 == 200")
    fp2 = store.fingerprint("TEST_FAILURE", "AssertionError: assert 404 == 200")
    assert fp1 == fp2


def test_fingerprint_differs_for_different_errors(store):
    fp1 = store.fingerprint("BUILD_ERROR", "ImportError: cannot import 'foo'")
    fp2 = store.fingerprint("BUILD_ERROR", "ImportError: cannot import 'bar'")
    assert fp1 != fp2


def test_fingerprint_differs_for_different_failure_types(store):
    fp1 = store.fingerprint("BUILD_ERROR", "same message")
    fp2 = store.fingerprint("TEST_FAILURE", "same message")
    assert fp1 != fp2


def test_persists_to_disk(tmp_path):
    path = tmp_path / "patterns.json"
    s1 = PatternStore(store_path=path)
    fp = s1.fingerprint("BUILD_ERROR", "NameError: name 'db' is not defined")
    s1.record(fp, "Fixed missing db import")

    s2 = PatternStore(store_path=path)
    assert s2.lookup(fp) == "Fixed missing db import"


def test_known_patterns_prompt_returns_empty_when_no_matches(store):
    result = store.known_patterns_prompt(["aaa", "bbb"])
    assert result == ""


def test_known_patterns_prompt_returns_section_when_matches_found(store):
    fp = store.fingerprint("BUILD_ERROR", "SessionMaker issue")
    store.record(fp, "Switch to async_sessionmaker")
    result = store.known_patterns_prompt([fp])
    assert "async_sessionmaker" in result
    assert "Known fixes" in result


def test_trims_to_max_patterns(tmp_path):
    from codegen_agent.pattern_store import _MAX_PATTERNS
    path = tmp_path / "patterns.json"
    s = PatternStore(store_path=path)
    # Insert more than the max
    for i in range(_MAX_PATTERNS + 50):
        s.record(f"fp_{i:04d}", f"fix {i}", file_path=f"file_{i}.py")

    s2 = PatternStore(store_path=path)
    assert s2.size() <= _MAX_PATTERNS


def test_record_overwrites_existing_entry(store):
    fp = store.fingerprint("BUILD_ERROR", "duplicate entry test")
    store.record(fp, "first fix")
    store.record(fp, "better fix")
    assert store.lookup(fp) == "better fix"
