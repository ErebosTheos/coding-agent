import asyncio
import os
import re
import json
from typing import List
from multiprocessing import cpu_count
from .models import Architecture, ExecutionNode, GeneratedFile, ExecutionResult
from .llm.protocol import LLMClient
from .utils import extract_code_from_markdown, calculate_sha256, ensure_directory, find_json_in_text, prune_prompt


class _BulkFileParser:
    """Stream-parses {"file_path": "content", ...} pairs from a chunked JSON response.

    Handles all JSON string escape sequences (\\n, \\t, \\\\, \\", etc.).
    Yields (file_path, content) pairs as each value string completes in the stream.

    Timeline benefit: for a 6-file project the LLM writes files sequentially in the
    JSON object — file 1 is written to disk before the LLM has even started file 3.
    """

    _INIT     = 0  # waiting for opening {
    _KEY_START = 1  # waiting for " to open a key
    _IN_KEY    = 2  # inside a key string
    _COLON     = 3  # waiting for :
    _VAL_START = 4  # waiting for " to open a value
    _IN_VAL    = 5  # inside a value string

    def __init__(self) -> None:
        self._state = self._INIT
        self._key: list[str] = []
        self._val: list[str] = []
        self._esc = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        """Feed a text chunk. Returns newly complete (file_path, content) pairs."""
        results: list[tuple[str, str]] = []
        for c in chunk:
            s = self._state

            if s == self._INIT:
                if c == '{':
                    self._state = self._KEY_START

            elif s == self._KEY_START:
                if c == '"':
                    self._key = []
                    self._esc = False
                    self._state = self._IN_KEY
                # skip whitespace, commas, closing brace

            elif s == self._IN_KEY:
                if self._esc:
                    self._key.append(c)
                    self._esc = False
                elif c == '\\':
                    self._esc = True
                elif c == '"':
                    self._state = self._COLON
                else:
                    self._key.append(c)

            elif s == self._COLON:
                if c == ':':
                    self._state = self._VAL_START

            elif s == self._VAL_START:
                if c == '"':
                    self._val = []
                    self._esc = False
                    self._state = self._IN_VAL

            elif s == self._IN_VAL:
                if self._esc:
                    if   c == 'n':  self._val.append('\n')
                    elif c == 't':  self._val.append('\t')
                    elif c == 'r':  self._val.append('\r')
                    elif c == '\\': self._val.append('\\')
                    elif c == '"':  self._val.append('"')
                    elif c == '/':  self._val.append('/')
                    else:           self._val.append('\\'); self._val.append(c)
                    self._esc = False
                elif c == '\\':
                    self._esc = True
                elif c == '"':
                    results.append((''.join(self._key), ''.join(self._val)))
                    self._key = []
                    self._val = []
                    self._state = self._KEY_START
                else:
                    self._val.append(c)

        return results

# Patterns that indicate the start of actual code (not LLM reasoning prose)
_CODE_START_RE = re.compile(
    r'^(import |from |#|class |def |[a-zA-Z_]\w*\s*=|if |for |while |try:|async |@|\{|\[|<|---)',
    re.MULTILINE,
)
# File extensions whose "code" is config/data, not Python/JS
_DATA_EXTS = {'.ini', '.toml', '.cfg', '.yml', '.yaml', '.json', '.md', '.txt', '.env', '.gitignore'}


def _strip_leading_prose(content: str, file_path: str) -> str:
    """Remove leading prose lines emitted by agentic LLMs (e.g. 'I will read...').

    For code files, skip lines until we hit something that looks like code.
    For data/config files, return the content as-is since there's no reliable
    way to distinguish prose from config syntax.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _DATA_EXTS:
        return content
    match = _CODE_START_RE.search(content)
    if match and match.start() > 0:
        stripped = content[match.start():]
        if len(stripped) > 20:   # only strip if something meaningful remains
            return stripped
    return content


def _is_directory_path(path: str) -> bool:
    return isinstance(path, str) and path.endswith("/")


_JS_EXTENSIONS = {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}


def _fix_relative_imports(file_path: str, content: str) -> str:
    """Convert intra-package absolute imports to relative imports.

    Python (src/main.py):
        from src import crud, models   →   from . import crud, models
        from src.crud import get_task  →   from .crud import get_task

    JS / TS (src/main.ts):
        import x from 'src/crud'       →   import x from './crud'
        import { x } from 'src/crud'   →   import { x } from './crud'
        const x = require('src/crud')  →   const x = require('./crud')

    PHP uses PSR-4 namespaces (use App\\Models\\X) or relative require_once paths;
    LLMs generate these correctly already, so no fix is needed.

    Applies only to files inside a sub-directory (i.e., inside a package/module dir).
    """
    normalized = file_path.replace("\\", "/")
    parts = normalized.split("/")
    if len(parts) < 2:
        return content  # top-level file — no containing package

    pkg = parts[0]  # e.g. "src", "app"
    ext = os.path.splitext(file_path)[1].lower()
    escaped = re.escape(pkg)

    if ext == ".py":
        # `from pkg import X, Y`  →  `from . import X, Y`
        content = re.sub(
            rf"^from {escaped} import ",
            "from . import ",
            content,
            flags=re.MULTILINE,
        )
        # `from pkg.sub import X`  →  `from .sub import X`
        content = re.sub(
            rf"^from {escaped}\.",
            "from .",
            content,
            flags=re.MULTILINE,
        )

    elif ext in _JS_EXTENSIONS:
        for q in ('"', "'"):
            eq = re.escape(q)
            # import ... from 'pkg/sub'  →  import ... from './sub'
            content = re.sub(
                rf"from {eq}{escaped}/",
                f"from {q}./",
                content,
            )
            # require('pkg/sub')  →  require('./sub')
            content = re.sub(
                rf"require\({eq}{escaped}/",
                f"require({q}./",
                content,
            )

    return content

EXECUTOR_SYSTEM_PROMPT = """You are an expert Senior Software Engineer.
Your goal is to implement the source code for the requested files based on the architecture contract.
Respond with the code for each file. If multiple files are requested, use a JSON format: {"file_path": "content"}.

CRITICAL RULES — violating any of these will break the build:
- Output ONLY raw source code. No explanations, no reasoning, no "I will..." text.
- Do NOT read files, search the workspace, or call any tools. Use only the context provided.
- Do NOT include markdown fences (```python etc.) unless the format explicitly requires JSON.
- Start your response with the first line of code, nothing before it.
- For files inside a package directory (e.g. src/main.py inside package 'src'), use RELATIVE
  imports for sibling modules: `from . import crud` not `from src import crud`,
  `from .models import Task` not `from src.models import Task`.
- For web applications (FastAPI, Flask, Express, etc.) that live inside src/, ALWAYS generate
  a top-level run.py (or index.js) at the project root so the server can be started with
  `python run.py` (or `node index.js`) without needing to know the package structure."""

EXECUTOR_USER_PROMPT_TEMPLATE = """Architecture Context:
{architecture_context}

Target File: {file_path}
Purpose: {purpose}
Contract: {contract}

Dependencies: {dependencies}

Implement the code for {file_path}."""

BULK_EXECUTOR_USER_PROMPT_TEMPLATE = """Architecture Context:
{architecture_context}

Files to implement:
{files_to_implement}

Implement all the files listed above. Return a JSON object where keys are file paths and values are the file contents.
Example:
{{
  "src/main.py": "print('hello')",
  "src/utils.py": "def add(a, b): return a + b"
}}
"""

class Executor:
    def __init__(self, llm_client: LLMClient, workspace: str, concurrency: int = -1, max_bulk_files: int = -1):
        self.llm_client = llm_client
        self.workspace = workspace
        env_concurrency = os.environ.get("CODEGEN_EXECUTOR_CONCURRENCY", "").strip()
        env_max_bulk = os.environ.get("CODEGEN_EXECUTOR_MAX_BULK_FILES", "").strip()

        if concurrency <= 0 and env_concurrency.isdigit():
            concurrency = int(env_concurrency)
        if max_bulk_files <= 0 and env_max_bulk.isdigit():
            max_bulk_files = int(env_max_bulk)

        # Adaptive concurrency: use CPU count if not specified
        if concurrency <= 0:
            self.concurrency = max(2, cpu_count() - 1) if cpu_count() else 4
        else:
            self.concurrency = concurrency
        # Adaptive batch sizing: larger batches for faster responses
        if max_bulk_files <= 0:
            self.max_bulk_files = max(15, min(50, cpu_count() * 5)) if cpu_count() else 20
        else:
            self.max_bulk_files = max_bulk_files

    async def execute(self, architecture: Architecture) -> ExecutionResult:
        """Executes the architecture. Uses bulk generation for small projects, wave-based otherwise."""
        executable_nodes = [n for n in architecture.nodes if not _is_directory_path(n.file_path)]
        skipped_nodes = [n.node_id for n in architecture.nodes if _is_directory_path(n.file_path)]
        if not executable_nodes:
            return ExecutionResult(generated_files=[], skipped_nodes=skipped_nodes, failed_nodes=[])

        exec_architecture = Architecture(
            file_tree=architecture.file_tree,
            nodes=executable_nodes,
            global_validation_commands=architecture.global_validation_commands,
        )

        if len(exec_architecture.nodes) <= self.max_bulk_files:
            print(
                f"  [Executor] Small project detected ({len(exec_architecture.nodes)} files, "
                f"bulk limit: {self.max_bulk_files}). Using bulk generation."
            )
            result = await self._execute_bulk(exec_architecture)
            return ExecutionResult(
                generated_files=result.generated_files,
                skipped_nodes=skipped_nodes + result.skipped_nodes,
                failed_nodes=result.failed_nodes,
            )
        
        print(
            f"  [Executor] Large project detected ({len(exec_architecture.nodes)} files, "
            f"concurrency: {self.concurrency}). Using wave-based generation."
        )
        waves = self._calculate_waves(exec_architecture.nodes)
        generated_files = []
        failed_nodes = []
        
        # Use semaphore to limit concurrent LLM calls across all waves
        semaphore = asyncio.Semaphore(self.concurrency)
        
        async def execute_with_limit(node: ExecutionNode) -> tuple[ExecutionNode, GeneratedFile | Exception]:
            async with semaphore:
                result = await self._execute_node(node, exec_architecture)
                return (node, result)
        
        for wave in waves:
            tasks = [execute_with_limit(node) for node in wave]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for item in results:
                if isinstance(item, Exception):
                    print(f"Wave execution error: {item}")
                    continue
                node, result = item
                if isinstance(result, Exception):
                    print(f"Error executing node {node.node_id}: {result}")
                    failed_nodes.append(node.node_id)
                else:
                    generated_files.append(result)
        
        return ExecutionResult(
            generated_files=generated_files,
            skipped_nodes=skipped_nodes,
            failed_nodes=failed_nodes
        )

    async def _execute_bulk(self, architecture: Architecture) -> ExecutionResult:
        """Generates all files in a single LLM call."""
        files_to_implement = []
        for node in architecture.nodes:
            files_to_implement.append({
                "file_path": node.file_path,
                "purpose": node.purpose,
                "contract": node.contract.__dict__ if node.contract else "None",
                "dependencies": [n.file_path for n in architecture.nodes if n.node_id in node.depends_on]
            })

        user_prompt = BULK_EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context="Project file tree: " + ", ".join(architecture.file_tree),
            files_to_implement=json.dumps(files_to_implement, indent=2)
        )
        user_prompt = prune_prompt(user_prompt, max_chars=28_000)

        response = await self.llm_client.generate(
            user_prompt,
            system_prompt=EXECUTOR_SYSTEM_PROMPT
        )

        data = find_json_in_text(response)
        if not data or not isinstance(data, dict):
            print("  [Executor] Bulk generation failed to return valid JSON. Falling back to wave-based.")
            return await self._execute_wave_fallback(architecture)

        node_map = {node.file_path: node for node in architecture.nodes}
        normalized: dict[str, str] = {}
        for file_path, content in data.items():
            if file_path in node_map and isinstance(content, str):
                normalized[file_path] = content

        missing_paths = [node.file_path for node in architecture.nodes if node.file_path not in normalized]
        if missing_paths:
            print(
                "  [Executor] Bulk response omitted planned files. "
                f"Missing: {missing_paths}. Falling back to wave-based generation."
            )
            return await self._execute_wave_fallback(architecture)

        generated_files = []
        for node in architecture.nodes:
            content = normalized[node.file_path]
            content = _fix_relative_imports(node.file_path, content)
            full_path = os.path.join(self.workspace, node.file_path)
            ensure_directory(os.path.dirname(full_path))
            with open(full_path, 'w') as f:
                f.write(content)

            print(f"  [Executor] Created file: {node.file_path}")
            generated_files.append(GeneratedFile(
                file_path=node.file_path,
                content=content,
                node_id=node.node_id,
                sha256=calculate_sha256(content)
            ))

        return ExecutionResult(generated_files=generated_files)

    async def _stream_bulk(self, architecture: Architecture) -> ExecutionResult:
        """Generates all files in one LLM call, writing each file as its JSON value
        arrives in the stream — combines bulk consistency with streaming latency.

        Falls back to _execute_bulk (non-streaming) if the client has no astream().
        Falls back to wave-based if the stream produces incomplete JSON.
        """
        if not hasattr(self.llm_client, "astream"):
            return await self._execute_bulk(architecture)

        # Filter out directory placeholder nodes (file_path ends with "/") —
        # the same guard that execute() applies before calling _execute_bulk.
        # Without this, the LLM may echo "src/" as a JSON key and open() crashes
        # with [Errno 21] Is a directory.
        executable_nodes = [n for n in architecture.nodes if not _is_directory_path(n.file_path)]
        if not executable_nodes:
            return ExecutionResult(generated_files=[])

        files_to_implement = [
            {
                "file_path": node.file_path,
                "purpose": node.purpose,
                "contract": node.contract.__dict__ if node.contract else "None",
                "dependencies": [
                    n.file_path for n in executable_nodes if n.node_id in node.depends_on
                ],
            }
            for node in executable_nodes
        ]
        user_prompt = BULK_EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context="Project file tree: " + ", ".join(architecture.file_tree),
            files_to_implement=json.dumps(files_to_implement, indent=2),
        )
        user_prompt = prune_prompt(user_prompt, max_chars=28_000)

        node_map = {node.file_path: node for node in executable_nodes}
        generated_files: list[GeneratedFile] = []
        generated_paths: set[str] = set()
        parser = _BulkFileParser()

        async for chunk in self.llm_client.astream(user_prompt, system_prompt=EXECUTOR_SYSTEM_PROMPT):
            for file_path, content in parser.feed(chunk):
                if file_path not in node_map:
                    continue  # hallucinated file or directory placeholder — skip
                content = _strip_leading_prose(content, file_path)
                content = _fix_relative_imports(file_path, content)
                full_path = os.path.join(self.workspace, file_path)
                ensure_directory(os.path.dirname(full_path))
                with open(full_path, "w") as f:
                    f.write(content)
                print(f"  [Executor] Created file: {file_path}")
                generated_files.append(GeneratedFile(
                    file_path=file_path,
                    content=content,
                    node_id=node_map[file_path].node_id,
                    sha256=calculate_sha256(content),
                ))
                generated_paths.add(file_path)

        missing = [n for n in executable_nodes if n.file_path not in generated_paths]
        if missing:
            print(
                f"  [Executor] Stream-bulk missing {len(missing)} file(s): "
                f"{[n.file_path for n in missing]}. Filling with wave-based."
            )
            waves = self._calculate_waves(missing)
            fallback = await self._execute_waves(waves, architecture)
            generated_files.extend(fallback.generated_files)

        return ExecutionResult(generated_files=generated_files)

    async def _execute_wave_fallback(self, architecture: Architecture) -> ExecutionResult:
        """Fallback for failed bulk generation."""
        waves = self._calculate_waves(architecture.nodes)
        return await self._execute_waves(waves, architecture)

    async def _execute_waves(self, waves: List[List[ExecutionNode]], architecture: Architecture) -> ExecutionResult:
        generated_files = []
        failed_nodes = []
        for wave in waves:
            tasks = [self._execute_node(node, architecture) for node in wave]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for node, result in zip(wave, results):
                if isinstance(result, Exception):
                    failed_nodes.append(node.node_id)
                else:
                    generated_files.append(result)
        return ExecutionResult(generated_files=generated_files, failed_nodes=failed_nodes)

    async def _execute_node(self, node: ExecutionNode, architecture: Architecture) -> GeneratedFile:
        """Executes a single node (generates a file)."""
        # Build context from dependencies (could be enhanced to include actual content of dependencies)
        dependencies = [n.file_path for n in architecture.nodes if n.node_id in node.depends_on]
        
        user_prompt = EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context="Project file tree: " + ", ".join(architecture.file_tree),
            file_path=node.file_path,
            purpose=node.purpose,
            contract=json.dumps(node.contract.__dict__) if node.contract else "None",
            dependencies=", ".join(dependencies)
        )
        user_prompt = prune_prompt(user_prompt, max_chars=12_000)
        
        content = await self.llm_client.generate(
            user_prompt,
            system_prompt=EXECUTOR_SYSTEM_PROMPT
        )

        # Strip markdown fences if the LLM included them despite instructions
        code_blocks = extract_code_from_markdown(content)
        if code_blocks:
            content = code_blocks[0]
        else:
            # Fallback: strip leading prose lines (agentic LLMs emit "I will..." before code)
            content = _strip_leading_prose(content, node.file_path)

        content = _fix_relative_imports(node.file_path, content)
        full_path = os.path.join(self.workspace, node.file_path)
        ensure_directory(os.path.dirname(full_path))
        
        with open(full_path, 'w') as f:
            f.write(content)
        
        print(f"  [Executor] Created file: {node.file_path}")
        
        return GeneratedFile(
            file_path=node.file_path,
            content=content,
            node_id=node.node_id,
            sha256=calculate_sha256(content)
        )

    def _calculate_waves(self, nodes: List[ExecutionNode]) -> List[List[ExecutionNode]]:
        """Calculates topological waves using Kahn's algorithm — O(N + E)."""
        node_map = {n.node_id: n for n in nodes}
        in_degree = {n.node_id: 0 for n in nodes}
        dependents: dict[str, list[str]] = {n.node_id: [] for n in nodes}

        for n in nodes:
            for dep in n.depends_on:
                if dep in in_degree:
                    in_degree[n.node_id] += 1
                    dependents[dep].append(n.node_id)

        queue = [n for n in nodes if in_degree[n.node_id] == 0]
        waves = []

        while queue:
            waves.append(list(queue))
            next_queue = []
            for node in queue:
                for dep_id in dependents[node.node_id]:
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        next_queue.append(node_map[dep_id])
            queue = next_queue

        total = sum(len(w) for w in waves)
        if total != len(nodes):
            remaining = [n.node_id for n in nodes if in_degree[n.node_id] > 0]
            raise ValueError(f"Cycle detected or missing dependency in architecture. Remaining nodes: {remaining}")

        return waves
