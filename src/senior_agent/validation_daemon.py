from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

_DEFAULT_TIMEOUT_SECONDS = 300.0
_MIN_TIMEOUT_SECONDS = 0.1
_REAPER_GRACE_SECONDS = 1.0


def _coerce_timeout(raw_value: object) -> float | None:
    if raw_value is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return None
    return max(_MIN_TIMEOUT_SECONDS, value)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass

    try:
        process.wait(timeout=_REAPER_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=_REAPER_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _execute_shell_command(
    *,
    command: str,
    cwd: Path,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    if not command.strip():
        return {
            "return_code": 1,
            "stdout": "",
            "stderr": "Validation daemon received an empty command.",
        }

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return {
            "return_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        timeout_note = (
            f"Validation command timed out after {timeout_seconds:.1f}s "
            "(watchdog reaper hard-killed process tree)."
        )
        merged_stderr = timeout_note if not stderr else f"{stderr.rstrip()}\n{timeout_note}"
        return {
            "return_code": 124,
            "stdout": stdout,
            "stderr": merged_stderr,
        }


def _respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for raw_line in sys.stdin:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError:
            _respond(
                {
                    "request_id": "",
                    "return_code": 1,
                    "stdout": "",
                    "stderr": "Validation daemon received malformed JSON payload.",
                }
            )
            continue

        if not isinstance(envelope, dict):
            _respond(
                {
                    "request_id": "",
                    "return_code": 1,
                    "stdout": "",
                    "stderr": "Validation daemon payload must be a JSON object.",
                }
            )
            continue

        request_id = str(envelope.get("request_id", "")).strip()
        action = str(envelope.get("action", "run")).strip().lower()

        if action == "ping":
            _respond(
                {
                    "request_id": request_id,
                    "status": "ok",
                    "return_code": 0,
                    "stdout": "",
                    "stderr": "",
                }
            )
            continue
        if action == "shutdown":
            _respond(
                {
                    "request_id": request_id,
                    "status": "shutting_down",
                    "return_code": 0,
                    "stdout": "",
                    "stderr": "",
                }
            )
            return 0

        command = str(envelope.get("command", ""))
        cwd_raw = str(envelope.get("cwd", ".")).strip() or "."
        cwd = Path(cwd_raw).resolve()
        timeout_seconds = _coerce_timeout(envelope.get("timeout_seconds"))

        if not cwd.exists() or not cwd.is_dir():
            _respond(
                {
                    "request_id": request_id,
                    "return_code": 1,
                    "stdout": "",
                    "stderr": f"Validation daemon cwd does not exist: {cwd}",
                }
            )
            continue

        result = _execute_shell_command(
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        result["request_id"] = request_id
        _respond(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
