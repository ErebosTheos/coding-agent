"""Streaming Plan+Architect+Execute pipeline.

Streams the combined LLM response and dispatches Executor._execute_node tasks
for each node as it is parsed — overlapping LLM generation with file writing.

Timeline (8-file project, Gemini ~30s generation, ~15s per node):
  Sequential:  30s (Plan+Arch LLM) + 30s (Bulk Executor LLM) = 60s
  Streaming:   nodes dispatched during the 30s architect stream
               → first nodes finish before architect stream ends
               → total ≈ 35-45s instead of 60s
"""

import asyncio
import json
from typing import Optional

from .models import (
    Architecture, Contract, ExecutionNode, ExecutionResult, GeneratedFile, Plan,
)
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
    dispatched as an asyncio Task via ``Executor._execute_node``.  Dependency
    ordering is respected: a node waits for its dep Tasks before executing.
    The full buffered response is parsed at the end to extract the canonical
    ``Plan`` and ``Architecture`` objects.

    Falls back to the normal ``PlannerArchitect`` + ``Executor.execute()``
    pipeline if the LLM client does not implement ``astream()``.
    """

    def __init__(self, llm_client: LLMClient, executor: Executor) -> None:
        self.llm_client = llm_client
        self.executor = executor

    async def run(self, prompt: str) -> tuple[Plan, Architecture, ExecutionResult]:
        # If the client doesn't support streaming, fall back immediately.
        if not hasattr(self.llm_client, "astream"):
            pa = PlannerArchitect(self.llm_client)
            plan, architecture = await pa.plan_and_architect(prompt)
            exec_result = await self.executor.execute(architecture)
            return plan, architecture, exec_result

        full_buf = ""
        node_parser = _NodeParser()

        # node_id → Task[GeneratedFile]
        dispatched: dict[str, asyncio.Task] = {}
        # raw node dicts waiting for their dependencies to be dispatched
        pending_raw: list[dict] = []
        # ordered list of dispatched ExecutionNode objects (for partial arch context)
        seen_nodes: list[ExecutionNode] = []

        # Reuse the executor's concurrency limit so we never spawn more concurrent
        # LLM subprocesses than the executor would for a normal wave-based run.
        # Dep-waiting does NOT hold the semaphore — only the actual LLM call does.
        semaphore = asyncio.Semaphore(self.executor.concurrency)

        def _parse_node(nd: dict) -> ExecutionNode:
            contract_data = nd.get("contract")
            contract = Contract(**contract_data) if isinstance(contract_data, dict) else None
            return ExecutionNode(
                node_id=nd["node_id"],
                file_path=nd["file_path"],
                purpose=nd["purpose"],
                depends_on=nd.get("depends_on", []),
                contract=contract,
            )

        def _dispatch(node: ExecutionNode) -> None:
            """Create a Task for a node, waiting for its dependency Tasks first."""
            seen_nodes.append(node)
            # Snapshot the partial architecture at dispatch time.
            # _execute_node only needs file_tree (prompt context) and nodes
            # (dep file-path lookup) — both are available for dispatched nodes.
            partial_arch = Architecture(
                file_tree=[n.file_path for n in seen_nodes],
                nodes=list(seen_nodes),
                global_validation_commands=[],
            )
            dep_tasks = [dispatched[dep] for dep in node.depends_on if dep in dispatched]

            async def _run() -> GeneratedFile:
                # Await deps outside the semaphore so waiting nodes don't block slots
                if dep_tasks:
                    await asyncio.gather(*dep_tasks, return_exceptions=True)
                async with semaphore:
                    return await self.executor._execute_node(node, partial_arch)

            dispatched[node.node_id] = asyncio.create_task(_run())

        def _flush_pending() -> None:
            """Dispatch any pending nodes whose dependencies are now dispatched."""
            changed = True
            while changed and pending_raw:
                changed = False
                still: list[dict] = []
                for nd in list(pending_raw):
                    node = _parse_node(nd)
                    if all(dep in dispatched for dep in node.depends_on):
                        _dispatch(node)
                        changed = True
                    else:
                        still.append(nd)
                pending_raw[:] = still

        # ── Stream the LLM response ─────────────────────────────────────────
        print("  [StreamExecutor] Streaming Plan+Architect response...")
        async for chunk in self.llm_client.astream(
            COMBINED_USER_PROMPT.format(prompt=prompt),
            system_prompt=COMBINED_SYSTEM_PROMPT,
        ):
            full_buf += chunk
            for nd in node_parser.feed(chunk):
                node = _parse_node(nd)
                if all(dep in dispatched for dep in node.depends_on):
                    _dispatch(node)
                else:
                    pending_raw.append(nd)
            if pending_raw:
                _flush_pending()

        # Final flush — handles any nodes not yet dispatched
        _flush_pending()
        # Force-dispatch nodes with unresolvable deps (broken architecture)
        for nd in list(pending_raw):
            _dispatch(_parse_node(nd))
        pending_raw.clear()

        print(
            f"  [StreamExecutor] Stream complete. "
            f"{len(dispatched)} node task(s) dispatched; awaiting..."
        )

        # ── Await all node Tasks ────────────────────────────────────────────
        generated_files: list[GeneratedFile] = []
        failed_nodes: list[str] = []
        for node_id, task in dispatched.items():
            try:
                generated_files.append(await task)
            except Exception as exc:
                print(f"  [StreamExecutor] Node {node_id} failed: {exc}")
                failed_nodes.append(node_id)

        # ── Parse Plan + Architecture from the full buffered response ───────
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

        return plan, architecture, ExecutionResult(
            generated_files=generated_files,
            failed_nodes=failed_nodes,
        )
