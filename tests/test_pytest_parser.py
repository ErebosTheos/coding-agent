"""Tests for structured pytest report parser."""
import json
import tempfile
from pathlib import Path

import pytest

from codegen_agent.pytest_parser import (
    _inject_json_report,
    _is_pytest_command,
    _parse_report_json,
    format_structured_failures_for_prompt,
    PytestReport,
    TestFailure,
)


# ── _is_pytest_command ────────────────────────────────────────────────────────

def test_is_pytest_command_recognises_bare_pytest():
    assert _is_pytest_command("pytest tests/")

def test_is_pytest_command_recognises_python_m_pytest():
    assert _is_pytest_command("python -m pytest tests/")

def test_is_pytest_command_rejects_ruff():
    assert not _is_pytest_command("ruff check .")

def test_is_pytest_command_rejects_go_test():
    assert not _is_pytest_command("go test ./...")


# ── _inject_json_report ───────────────────────────────────────────────────────

def test_inject_json_report_adds_flags():
    cmd = _inject_json_report("pytest tests/", "/tmp/report.json")
    assert "--json-report" in cmd
    assert "/tmp/report.json" in cmd

def test_inject_json_report_skips_if_already_present():
    cmd = "pytest --json-report tests/"
    result = _inject_json_report(cmd, "/tmp/r.json")
    assert result.count("--json-report") == 1

def test_inject_json_report_handles_python_m_pytest():
    cmd = _inject_json_report("python -m pytest tests/", "/tmp/r.json")
    assert "--json-report" in cmd


# ── _parse_report_json ────────────────────────────────────────────────────────

def _make_report(tests: list[dict]) -> dict:
    return {
        "summary": {"passed": 0, "failed": len(tests), "errors": 0},
        "tests": tests,
    }


def test_parse_report_extracts_failed_test():
    data = _make_report([{
        "nodeid": "tests/test_auth.py::test_login",
        "outcome": "failed",
        "call": {
            "crash": {"message": "AssertionError: assert 404 == 200"},
            "traceback": [
                {"path": "tests/test_auth.py", "lineno": 23},
                {"path": "src/auth.py", "lineno": 45},
            ],
        },
    }])
    report = _parse_report_json(data, workspace="/workspace")
    assert report.failed == 1
    assert len(report.failures) == 1
    tf = report.failures[0]
    assert tf.test_id == "tests/test_auth.py::test_login"
    assert "AssertionError" in tf.short_repr


def test_parse_report_puts_source_file_in_broken_source_files():
    data = _make_report([{
        "nodeid": "tests/test_auth.py::test_login",
        "outcome": "failed",
        "call": {
            "crash": {"message": "AssertionError"},
            "traceback": [
                {"path": "tests/test_auth.py", "lineno": 10},
                {"path": "src/auth.py", "lineno": 50},
            ],
        },
    }])
    report = _parse_report_json(data, workspace="/workspace")
    assert "src/auth.py" in report.broken_source_files
    assert "tests/test_auth.py" not in report.broken_source_files


def test_parse_report_skips_passed_tests():
    data = {
        "summary": {"passed": 2, "failed": 0, "errors": 0},
        "tests": [
            {"nodeid": "tests/test_a.py::test_x", "outcome": "passed"},
            {"nodeid": "tests/test_b.py::test_y", "outcome": "passed"},
        ],
    }
    report = _parse_report_json(data, workspace="/workspace")
    assert report.failures == []
    assert report.broken_source_files == {}


def test_parse_report_handles_missing_traceback():
    data = _make_report([{
        "nodeid": "tests/test_foo.py::test_bar",
        "outcome": "error",
        "call": {"crash": {"message": "ImportError"}, "traceback": []},
    }])
    report = _parse_report_json(data, workspace="/workspace")
    assert len(report.failures) == 1
    assert report.broken_source_files == {}


# ── format_structured_failures_for_prompt ────────────────────────────────────

def test_format_returns_empty_for_no_failures():
    report = PytestReport()
    assert format_structured_failures_for_prompt(report) == ""


def test_format_includes_test_id_and_failure():
    tf = TestFailure(
        test_id="tests/test_auth.py::test_login",
        test_file="tests/test_auth.py",
        outcome="failed",
        short_repr="AssertionError: assert 404 == 200",
        long_repr="AssertionError: assert 404 == 200\n  where 404 = resp.status_code",
        source_files=["src/auth.py"],
    )
    report = PytestReport(failed=1, failures=[tf])
    text = format_structured_failures_for_prompt(report)
    assert "test_login" in text
    assert "AssertionError" in text
    assert "src/auth.py" in text


def test_format_caps_at_max_failures():
    failures = [
        TestFailure(
            test_id=f"tests/test_{i}.py::test_x",
            test_file=f"tests/test_{i}.py",
            outcome="failed",
            short_repr=f"error {i}",
            long_repr=f"error {i}",
        )
        for i in range(10)
    ]
    report = PytestReport(failed=10, failures=failures)
    text = format_structured_failures_for_prompt(report, max_failures=3)
    assert "and 7 more" in text
