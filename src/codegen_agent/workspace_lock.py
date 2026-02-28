from pathlib import Path
from typing import Optional


class WorkspaceLock:
    """Exclusive file lock on <workspace>/.codegen_agent/run.lock.

    Usage:
        lock = WorkspaceLock(workspace)
        if not lock.acquire():
            raise RuntimeError("Workspace already in use.")
        try:
            ...
        finally:
            lock.release()

    On platforms without fcntl (Windows), acquire() always returns True
    and release() is a no-op — the warning is suppressed for CI portability.
    """

    def __init__(self, workspace: str):
        lock_dir = Path(workspace) / ".codegen_agent"
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._path = lock_dir / "run.lock"
        self._file: Optional[object] = None

    def acquire(self) -> bool:
        """Try to acquire an exclusive non-blocking lock.
        Returns True on success, False if workspace is already locked.
        """
        try:
            import fcntl
            self._file = open(self._path, "w")
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except ImportError:
            return True   # fcntl unavailable — skip locking
        except OSError:
            if self._file:
                self._file.close()
                self._file = None
            return False

    def release(self) -> None:
        if self._file is None:
            return
        try:
            import fcntl
            fcntl.flock(self._file, fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            self._file.close()
            self._file = None
