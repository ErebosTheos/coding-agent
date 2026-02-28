"""Tests for StreamingPlanArchExecutor with the bulk+stream execution model.

The executor now:
1. Streams the Plan+Arch LLM response to get the architecture
2. Calls executor._stream_bulk() — one bulk LLM call whose response is also
   streamed, writing each file as its JSON value completes.
"""
import asyncio
import json

from codegen_agent.executor import _BulkFileParser
from codegen_agent.models import ExecutionResult, GeneratedFile
from codegen_agent.stream_executor import StreamingPlanArchExecutor


# ── Fake helpers ──────────────────────────────────────────────────────────────

class _FakeStreamingLLM:
    """Streams a plan+arch JSON payload in two chunks."""
    def __init__(self, payload: dict):
        self._payload = payload

    async def astream(self, prompt: str, system_prompt: str = ""):
        data = json.dumps(self._payload)
        half = max(1, len(data) // 2)
        yield data[:half]
        yield data[half:]


class _FakeExecutor:
    """Executor stub whose _stream_bulk() returns canned files."""
    def __init__(self, files: list[GeneratedFile] | None = None):
        self.concurrency = 4
        self._files = files or []
        self.stream_bulk_called = False

    async def _stream_bulk(self, architecture):
        self.stream_bulk_called = True
        return ExecutionResult(generated_files=self._files)


def _arch_payload(n_nodes: int = 2) -> dict:
    nodes = [
        {
            "node_id": f"n{i}",
            "file_path": f"file{i}.py",
            "purpose": f"File {i}",
            "depends_on": [] if i == 1 else [f"n{i-1}"],
            "contract": {
                "purpose": f"File {i}", "inputs": [], "outputs": [],
                "public_api": [], "invariants": [],
            },
        }
        for i in range(1, n_nodes + 1)
    ]
    return {
        "plan": {
            "project_name": "demo", "tech_stack": "python",
            "features": [{"id": "f1", "title": "A", "description": "B", "priority": 1}],
            "entry_point": "file1.py", "test_strategy": "pytest",
        },
        "architecture": {
            "file_tree": [f"file{i}.py" for i in range(1, n_nodes + 1)],
            "nodes": nodes,
            "global_validation_commands": [],
        },
    }


def _fake_files(n: int) -> list[GeneratedFile]:
    return [
        GeneratedFile(file_path=f"file{i}.py", content=f"# {i}", node_id=f"n{i}", sha256="x")
        for i in range(1, n + 1)
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_stream_executor_calls_stream_bulk():
    """After streaming the arch, the executor delegates to _stream_bulk()."""
    files = _fake_files(2)
    llm = _FakeStreamingLLM(_arch_payload(2))
    executor = _FakeExecutor(files=files)
    stream_exec = StreamingPlanArchExecutor(llm, executor)

    _, _, result = asyncio.run(stream_exec.run("build demo"))

    assert executor.stream_bulk_called, "_stream_bulk() was not called"
    assert len(result.generated_files) == 2


def test_stream_executor_returns_correct_files():
    """Files returned by _stream_bulk() are surfaced in the pipeline result."""
    files = _fake_files(3)
    llm = _FakeStreamingLLM(_arch_payload(3))
    executor = _FakeExecutor(files=files)
    stream_exec = StreamingPlanArchExecutor(llm, executor)

    _, _, result = asyncio.run(stream_exec.run("build demo"))

    paths = {f.file_path for f in result.generated_files}
    assert paths == {"file1.py", "file2.py", "file3.py"}


def test_bulk_file_parser_basic():
    """_BulkFileParser yields (path, content) pairs as values complete."""
    payload = '{"a.py": "content_a", "b.py": "content_b"}'
    parser = _BulkFileParser()
    results = parser.feed(payload)
    assert results == [("a.py", "content_a"), ("b.py", "content_b")]


def test_bulk_file_parser_streaming():
    """_BulkFileParser works when JSON arrives in small chunks."""
    payload = '{"src/main.py": "import os\\nprint(os.getcwd())"}'
    parser = _BulkFileParser()
    results = []
    for char in payload:          # worst case: one char at a time
        results.extend(parser.feed(char))
    assert len(results) == 1
    path, content = results[0]
    assert path == "src/main.py"
    assert content == "import os\nprint(os.getcwd())"


def test_bulk_file_parser_escape_sequences():
    """_BulkFileParser correctly unescapes \\n, \\t, \\\\, \\\"."""
    payload = r'{"f.py": "line1\nline2\t\t\\end\""}'
    parser = _BulkFileParser()
    results = parser.feed(payload)
    assert len(results) == 1
    assert results[0][1] == 'line1\nline2\t\t\\end"'
