"""Streaming Plan+Architect+Execute pipeline.

Streams the combined LLM response and dispatches Executor._execute_node tasks
for each node as it is parsed — overlapping LLM generation with file writing.

Timeline (8-file project, Gemini ~30s generation, ~15s per node):
  Sequential:  30s (Plan+Arch LLM) + 30s (Bulk Executor LLM) = 60s
  Streaming:   nodes dispatched during the 30s architect stream
               → first nodes finish before architect stream ends
               → total ≈ 35-45s instead of 60s
"""

import json
import os
from typing import Optional

from .models import Architecture, Plan
from .executor import Executor
from .planner_architect import COMBINED_SYSTEM_PROMPT, COMBINED_USER_PROMPT, PlannerArchitect
from .utils import find_json_in_text, extract_code_from_markdown
from .llm.protocol import LLMClient


class _NodeParser:
    """Incrementally parses complete node objects from a streaming JSON response.

    Scans for the ``"nodes"`` array inside the combined Plan+Architect response
    and yields each complete ``{...}`` node dict as it arrives, using
    bracket-depth + JSON string-state tracking so embedded braces in strings
    are handled correctly.

    Critically, ``_pos`` tracks the exact scan position so subsequent ``feed()``
    calls resume without re-processing already-scanned bytes (which would corrupt
    the bracket-depth counter and cause missed nodes).
    """

    def __init__(self) -> None:
        self._buf = ""
        self._pos = 0             # Resume position — never re-scan before this
        self._in_nodes = False    # Found the "nodes": [ marker
        self._depth = 0           # Bracket depth inside the nodes array
        self._node_start = -1     # Start index of the current node in _buf
        self._in_string = False   # Are we inside a JSON string?
        self._escape_next = False # Is the next character escaped?

    def feed(self, chunk: str) -> list[dict]:
        """Feed a text chunk. Returns newly complete node dicts (may be empty)."""
        self._buf += chunk
        results: list[dict] = []

        # ── Phase 1: wait until we see "nodes": [ ──────────────────────────
        if not self._in_nodes:
            idx = self._buf.find('"nodes"')
            if idx == -1:
                # Keep a tail in case the key spans two chunks (7 chars + margin)
                if len(self._buf) > 12:
                    self._buf = self._buf[-12:]
                    self._pos = 0
                return results
            bracket_idx = self._buf.find('[', idx)
            if bracket_idx == -1:
                self._buf = self._buf[idx:]   # keep from "nodes" onward
                self._pos = 0
                return results
            self._in_nodes = True
            self._buf = self._buf[bracket_idx + 1:]
            self._pos = 0
            self._depth = 0
            self._node_start = -1
            self._in_string = False
            self._escape_next = False

        # ── Phase 2: scan from where we left off ────────────────────────────
        i = self._pos
        while i < len(self._buf):
            c = self._buf[i]

            if self._escape_next:
                self._escape_next = False
                i += 1
                continue

            if c == '\\' and self._in_string:
                self._escape_next = True
                i += 1
                continue

            if c == '"':
                self._in_string = not self._in_string
                i += 1
                continue

            if self._in_string:
                i += 1
                continue

            # ── outside strings: track bracket depth ──
            if self._depth == 0 and c == '{':
                self._node_start = i
                self._depth = 1
            elif self._depth > 0:
                if c == '{':
                    self._depth += 1
                elif c == '}':
                    self._depth -= 1
                    if self._depth == 0:
                        node_str = self._buf[self._node_start: i + 1]
                        try:
                            results.append(json.loads(node_str))
                        except json.JSONDecodeError:
                            pass  # malformed fragment — skip
                        # Trim buffer to just after this node; restart scan
                        self._buf = self._buf[i + 1:]
                        i = 0
                        self._node_start = -1
                        continue   # skip i += 1 below
            i += 1

        # ── Save scan position; trim buffer to bound memory ─────────────────
        self._pos = i
        if self._node_start >= 0:
            # Partial node in progress — keep the buffer from its opening brace
            trim = self._node_start
            self._buf = self._buf[trim:]
            self._node_start = 0
            self._pos -= trim
        else:
            # Between nodes — nothing useful left in the buffer
            self._buf = ""
            self._pos = 0

        return results


class StreamingPlanArchExecutor:
    """Overlaps the Plan+Architect LLM stream with individual file generation.

    As the LLM streams its JSON response, each parsed node is immediately
    dispatched as an asyncio Task via ``Executor._execute_node``.
    By default, dependency gating is disabled to maximize parallel throughput.
    Set ``CODEGEN_STREAM_RESPECT_DEPENDENCIES=1`` to restore strict dep waits.
    The full buffered response is parsed at the end to extract the canonical
    ``Plan`` and ``Architecture`` objects.

    Falls back to the normal ``PlannerArchitect`` + ``Executor.execute()``
    pipeline if the LLM client does not implement ``astream()``.
    """

    def __init__(self, llm_client: LLMClient, executor: Executor) -> None:
        self.llm_client = llm_client
        self.executor = executor
        self._respect_dependencies = (
            os.environ.get("CODEGEN_STREAM_RESPECT_DEPENDENCIES", "0").strip() == "1"
        )

    async def run(self, prompt: str) -> tuple[Plan, Architecture, ExecutionResult]:
        # If the client doesn't support streaming, fall back immediately.
        if not hasattr(self.llm_client, "astream"):
            pa = PlannerArchitect(self.llm_client)
            plan, architecture = await pa.plan_and_architect(prompt)
            exec_result = await self.executor.execute(architecture)
            return plan, architecture, exec_result

        # ── Phase 1: Stream Plan + Architecture ─────────────────────────────
        # Buffer the full response; parse plan+arch once the stream is done.
        # No per-node dispatch here — all execution happens in Phase 2 so the
        # LLM sees the complete file tree as context (avoids cross-file mismatches).
        full_buf = ""
        print("  [StreamExecutor] Streaming Plan+Architect response...")
        async for chunk in self.llm_client.astream(
            COMBINED_USER_PROMPT.format(prompt=prompt),
            system_prompt=COMBINED_SYSTEM_PROMPT,
        ):
            full_buf += chunk

        json_blocks = extract_code_from_markdown(full_buf, "json")
        try:
            data = (
                json.loads(json_blocks[0])
                if json_blocks
                else (find_json_in_text(full_buf) or json.loads(full_buf))
            )
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                f"StreamingPlanArchExecutor: failed to parse LLM response: {full_buf[:500]}"
            ) from exc

        plan = PlannerArchitect._parse_plan(data.get("plan", data))
        architecture = PlannerArchitect._parse_architecture(data.get("architecture", data))

        print(
            f"  [StreamExecutor] Plan+Arch complete. "
            f"{len(architecture.nodes)} node(s). Executing (stream-bulk)..."
        )

        # ── Phase 2: Stream-bulk execution ──────────────────────────────────
        # One LLM call for all files; files written as their JSON values arrive.
        # Falls back to wave-based if the stream produces incomplete JSON.
        exec_result = await self.executor._stream_bulk(architecture)

        return plan, architecture, exec_result
