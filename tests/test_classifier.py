import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.classifier import classify_failure
from senior_agent.models import FailureType


class FailureClassifierTests(unittest.TestCase):
    def test_classifies_lint_failure_from_command(self) -> None:
        failure = classify_failure("ruff check .", "", "")
        self.assertEqual(failure, FailureType.LINT_TYPE_FAILURE)

    def test_classifies_test_failure_from_command(self) -> None:
        failure = classify_failure("python -m pytest", "", "")
        self.assertEqual(failure, FailureType.TEST_FAILURE)

    def test_classifies_runtime_exception_from_output(self) -> None:
        failure = classify_failure("python app.py", "", "Traceback (most recent call last)")
        self.assertEqual(failure, FailureType.RUNTIME_EXCEPTION)

    def test_classifies_build_error_from_output(self) -> None:
        failure = classify_failure("make", "", "Compilation failed at step 2")
        self.assertEqual(failure, FailureType.BUILD_ERROR)

    def test_classifies_perf_regression_from_output(self) -> None:
        failure = classify_failure("python benchmark.py", "", "timeout exceeded")
        self.assertEqual(failure, FailureType.PERF_REGRESSION)

    def test_classifies_unknown_when_no_signal(self) -> None:
        failure = classify_failure("custom-check", "all good", "")
        self.assertEqual(failure, FailureType.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
