import sys
import tempfile

import pytest

from codegen_agent.workspace_lock import WorkspaceLock


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="fcntl lock semantics unavailable on Windows",
)


def test_acquire_and_release_succeeds():
    with tempfile.TemporaryDirectory() as workspace:
        lock = WorkspaceLock(workspace)
        assert lock.acquire() is True
        lock.release()
        assert lock._file is None


def test_double_acquire_fails():
    with tempfile.TemporaryDirectory() as workspace:
        first = WorkspaceLock(workspace)
        second = WorkspaceLock(workspace)

        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
