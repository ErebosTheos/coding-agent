import subprocess
import hashlib
import re
import os
import json
from typing import Any, List, Optional
from .models import CommandResult

_COMMAND_TIMEOUT = 120  # seconds; prevents hanging GUI/infinite-loop processes


def run_shell_command(command: str, cwd: Optional[str] = None) -> CommandResult:
    """Runs a shell command and returns a CommandResult.

    Enforces a hard timeout so GUI apps, infinite loops, or network-waiting
    processes cannot stall the healing loop indefinitely.
    """
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    try:
        stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return CommandResult(
            command=command,
            exit_code=-1,
            stdout=stdout or "",
            stderr=(stderr or "") + f"\n[Killed: command exceeded {_COMMAND_TIMEOUT}s timeout]",
        )
    return CommandResult(
        command=command,
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )

def batched_shell_commands(commands: List[tuple[str, Optional[str]]], max_workers: int = 4) -> List[CommandResult]:
    """Run multiple shell commands concurrently using a thread pool.

    Args:
        commands: List of (command_str, cwd) tuples
        max_workers: Maximum number of concurrent subprocesses

    Returns:
        List of CommandResult in same order as input
    """
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_shell_command, cmd, cwd) for cmd, cwd in commands]
        return [f.result() for f in futures]

def extract_code_from_markdown(content: str, language: Optional[str] = None) -> List[str]:
    """Extracts code blocks from markdown content."""
    # Use a more flexible regex that doesn't strictly require newlines if the block is small
    pattern = r"```(?:{}|)\s*\n?(.*?)\n?```".format(language if language else r"[a-zA-Z]*")
    return re.findall(pattern, content, re.DOTALL)

def find_json_in_text(text: str) -> Optional[Any]:
    """Find the first valid JSON object/array embedded in arbitrary text.

    The scanner is resilient to stray braces in leading prose: it tries each
    potential JSON start and continues when a candidate fails to decode.
    """
    if not text:
        return None

    decoder = json.JSONDecoder()
    starts = [i for i, char in enumerate(text) if char in "{["]
    for start in starts:
        try:
            obj, _end = decoder.raw_decode(text[start:])
            return obj
        except json.JSONDecodeError:
            continue
    return None

def calculate_sha256(content: str) -> str:
    """Calculates the SHA256 hash of a string."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()

def ensure_directory(path: str):
    """Ensures that a directory exists."""
    os.makedirs(path, exist_ok=True)


def prune_prompt(prompt: str, max_chars: int = 32_000) -> str:
    """Reduce prompt length while preserving the highest-signal sections.

    Strategy (§14.5):
    1. Soft trim: replace <<SOURCE>> / <<FILE>> blocks with signature-only view.
    2. Hard clear: drop <<HISTORY_START>> ... <<HISTORY_END>> blobs.
    3. Preserve: <<LATEST_SOURCE>>, <<TEST_OUTPUT>>, <<CONTRACT>> always kept.
       If still over limit, truncate from the start (preserve the tail).
    """
    if len(prompt) <= max_chars:
        return prompt

    # Step 1: soft trim — replace <<SOURCE>> block content with signatures
    def _signatures_only(block_content: str) -> str:
        lines = block_content.splitlines()
        keep = []
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith(("def ", "async def ", "class ")):
                keep.append(line)
                # include next line if it looks like a docstring opener
                if i + 1 < len(lines) and '"""' in lines[i + 1]:
                    keep.append(lines[i + 1])
        return "\n".join(keep) if keep else block_content[:200]

    prompt = re.sub(
        r"(<<(?:SOURCE|FILE)>>)(.*?)(<<\/(?:SOURCE|FILE)>>)",
        lambda m: m.group(1) + _signatures_only(m.group(2)) + m.group(3),
        prompt,
        flags=re.DOTALL,
    )

    if len(prompt) <= max_chars:
        return prompt

    # Step 2: hard clear — drop history blobs
    prompt = re.sub(
        r"<<HISTORY_START>>.*?<<HISTORY_END>>",
        "",
        prompt,
        flags=re.DOTALL,
    )

    if len(prompt) <= max_chars:
        return prompt

    # Step 3: truncate from start, keep the tail
    return prompt[-max_chars:]
