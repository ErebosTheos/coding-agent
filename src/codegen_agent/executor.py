import asyncio
import os
import re
import json
from typing import List
from multiprocessing import cpu_count
from .models import Architecture, ExecutionNode, GeneratedFile, ExecutionResult
from .llm.protocol import LLMClient
from .utils import extract_code_from_markdown, calculate_sha256, ensure_directory, find_json_in_text

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

EXECUTOR_SYSTEM_PROMPT = """You are an expert Senior Software Engineer.
Your goal is to implement the source code for the requested files based on the architecture contract.
Respond with the code for each file. If multiple files are requested, use a JSON format: {"file_path": "content"}.

CRITICAL RULES — violating any of these will break the build:
- Output ONLY raw source code. No explanations, no reasoning, no "I will..." text.
- Do NOT read files, search the workspace, or call any tools. Use only the context provided.
- Do NOT include markdown fences (```python etc.) unless the format explicitly requires JSON.
- Start your response with the first line of code, nothing before it."""

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
