from __future__ import annotations

from senior_agent.models import FailureType

_LINT_COMMAND_HINTS = (
    "eslint",
    "ruff",
    "flake8",
    "mypy",
    "pylint",
    "tsc",
    "typecheck",
    "golangci-lint",
)

_TEST_COMMAND_HINTS = (
    "pytest",
    "unittest",
    "go test",
    "cargo test",
    "npm test",
    "pnpm test",
)

_PERF_COMMAND_HINTS = ("benchmark", "perf", "loadtest")

_LINT_OUTPUT_HINTS = (
    "lint",
    "type error",
    "typing error",
    "mypy",
    "is not assignable to type",
)

_TEST_OUTPUT_HINTS = (
    "assertionerror",
    "failures",
    "expected",
    "test failed",
    "collected ",
)

_RUNTIME_OUTPUT_HINTS = (
    "traceback (most recent call last)",
    "exception",
    "segmentation fault",
    "panic:",
    "runtimeerror",
)

_BUILD_OUTPUT_HINTS = (
    "build failed",
    "compilation failed",
    "linker error",
    "cannot find module",
    "undefined reference",
)

_PERF_OUTPUT_HINTS = (
    "timed out",
    "timeout exceeded",
    "performance regression",
    "too slow",
)


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def classify_failure(command: str, stdout: str = "", stderr: str = "") -> FailureType:
    """Classify a failing command into a normalized failure type."""

    command_lower = command.lower()
    output_lower = f"{stdout}\n{stderr}".lower()

    if _contains_any(command_lower, _LINT_COMMAND_HINTS):
        return FailureType.LINT_TYPE_FAILURE
    if _contains_any(command_lower, _TEST_COMMAND_HINTS):
        return FailureType.TEST_FAILURE
    if _contains_any(command_lower, _PERF_COMMAND_HINTS):
        return FailureType.PERF_REGRESSION

    if _contains_any(output_lower, _LINT_OUTPUT_HINTS):
        return FailureType.LINT_TYPE_FAILURE
    if _contains_any(output_lower, _PERF_OUTPUT_HINTS):
        return FailureType.PERF_REGRESSION
    if _contains_any(output_lower, _BUILD_OUTPUT_HINTS):
        return FailureType.BUILD_ERROR
    if _contains_any(output_lower, _RUNTIME_OUTPUT_HINTS):
        return FailureType.RUNTIME_EXCEPTION
    if _contains_any(output_lower, _TEST_OUTPUT_HINTS):
        return FailureType.TEST_FAILURE

    return FailureType.UNKNOWN
