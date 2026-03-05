import asyncio
import os
import re
import json
from typing import List
from multiprocessing import cpu_count
from .models import Architecture, ExecutionNode, GeneratedFile, ExecutionResult
from .llm.protocol import LLMClient
from .utils import extract_code_from_markdown, calculate_sha256, ensure_directory, find_json_in_text, prune_prompt
from .live_guard import check_file


# ── EncodingGuard ─────────────────────────────────────────────────────────────
_INVISIBLE_ALL_FILES = ("\ufeff", "\u200b", "\u200c", "\u200d")


def _normalize_encoding(content: str) -> str:
    """Strip BOM, zero-width chars, and normalize CRLF→LF. Applies to all files."""
    for ch in _INVISIBLE_ALL_FILES:
        content = content.replace(ch, "")
    return content.replace("\r\n", "\n").replace("\r", "\n")


# ── RuntimePathGuard ──────────────────────────────────────────────────────────

def _validate_write_path(workspace: str, file_path: str) -> str | None:
    """Return an error string if file_path is unsafe to write inside workspace, else None."""
    if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
        return f"[RuntimePathGuard] Invalid file path: {file_path!r}"
    if os.path.isabs(file_path):
        return f"[RuntimePathGuard] Rejected absolute path: {file_path!r}"
    workspace_abs = os.path.abspath(workspace)
    full = os.path.normpath(os.path.join(workspace_abs, file_path))
    if not (full == workspace_abs or full.startswith(workspace_abs + os.sep)):
        return f"[RuntimePathGuard] Rejected path traversal: {file_path!r}"
    return None


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
_ASYNC_SESSIONMAKER_CALL_RE = re.compile(
    r"async_sessionmaker\((?P<args>.*?)\)",
    re.DOTALL,
)
_INVISIBLE_PY_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff")


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


def _inject_expire_on_commit_arg(args: str) -> str:
    """Ensure async_sessionmaker() calls disable attribute expiry after commit.

    This prevents common async SQLAlchemy response-serialization failures where
    Pydantic accesses expired ORM attributes outside greenlet context.
    """
    if "expire_on_commit" in args:
        return args

    stripped = args.rstrip()
    if not stripped:
        return "expire_on_commit=False"

    if "\n" in args:
        indent = "    "
        for line in args.splitlines():
            if line.strip():
                indent_match = re.match(r"\s*", line)
                indent = indent_match.group(0) if indent_match else indent
                break
        suffix = "" if stripped.endswith(",") else ","
        return f"{stripped}{suffix}\n{indent}expire_on_commit=False\n"

    suffix = "" if stripped.endswith(",") else ", "
    return f"{stripped}{suffix}expire_on_commit=False"


def _ensure_async_sessionmaker_guardrail(file_path: str, content: str) -> str:
    if not file_path.endswith(".py") or "async_sessionmaker(" not in content:
        return content

    def _replace(match: re.Match[str]) -> str:
        args = match.group("args")
        return f"async_sessionmaker({_inject_expire_on_commit_arg(args)})"

    return _ASYNC_SESSIONMAKER_CALL_RE.sub(_replace, content)


def _fix_utcnow(file_path: str, content: str) -> str:
    """Replace deprecated datetime.utcnow() with timezone-aware equivalent.

    datetime.utcnow() is deprecated in Python 3.12 and returns a naive datetime,
    which causes subtle bugs when compared to timezone-aware datetimes (e.g. JWT exp).
    """
    if not file_path.endswith(".py") or "datetime.utcnow" not in content:
        return content
    content = content.replace("datetime.utcnow()", "datetime.now(timezone.utc)")
    # Ensure `timezone` is present in the datetime import line
    if "timezone" not in content and "from datetime import" in content:
        content = re.sub(
            r"(from datetime import )([^\n]+)",
            lambda m: m.group(1) + m.group(2).rstrip() + ", timezone",
            content,
            count=1,
        )
    return content


def _fix_httpx_async_transport(file_path: str, content: str) -> str:
    """Fix deprecated httpx.AsyncClient(app=...) to use ASGITransport.

    httpx >= 0.23 removed the `app=` shortcut; tests must pass an explicit
    transport=ASGITransport(app=app) instead.
    """
    if not file_path.endswith(".py"):
        return content
    if "ASGITransport" in content or "AsyncClient(app=" not in content:
        return content
    # Add ASGITransport to existing httpx import line
    if "from httpx import" in content:
        content = re.sub(
            r"(from httpx import )([^\n]+)",
            lambda m: m.group(1) + m.group(2).rstrip() + ", ASGITransport"
                      if "ASGITransport" not in m.group(2) else m.group(0),
            content,
            count=1,
        )
    # Rewrite AsyncClient(app=X, base_url=...) → AsyncClient(transport=ASGITransport(app=X), base_url=...)
    content = re.sub(
        r"AsyncClient\(\s*app=([^,)]+),",
        r"AsyncClient(transport=ASGITransport(app=\1),",
        content,
    )
    return content


def _fix_orm_sessionmaker(file_path: str, content: str) -> str:
    """Replace sqlalchemy.orm.sessionmaker with async_sessionmaker when AsyncSession is used.

    The orm.sessionmaker is not async-aware; using it with AsyncSession is deprecated
    and causes warnings. async_sessionmaker (SQLAlchemy 2.0+) is the correct replacement.
    """
    if not file_path.endswith(".py"):
        return content
    if "async_sessionmaker" in content or "AsyncSession" not in content:
        return content
    if "sessionmaker" not in content or "from sqlalchemy.orm import" not in content:
        return content
    # Remove sessionmaker from the orm import line
    content = re.sub(r",\s*sessionmaker\b", "", content)
    content = re.sub(r"\bsessionmaker\s*,\s*", "", content)
    content = re.sub(r"from sqlalchemy\.orm import sessionmaker\n", "", content)
    # Add async_sessionmaker to the existing ext.asyncio import if present
    if "from sqlalchemy.ext.asyncio import" in content:
        content = re.sub(
            r"(from sqlalchemy\.ext\.asyncio import )([^\n]+)",
            lambda m: m.group(1) + m.group(2).rstrip() + ", async_sessionmaker"
                      if "async_sessionmaker" not in m.group(2) else m.group(0),
            content,
            count=1,
        )
    elif "async_sessionmaker" not in content:
        # No ext.asyncio import yet — prepend one before the first sqlalchemy import
        content = re.sub(
            r"(from sqlalchemy\b)",
            "from sqlalchemy.ext.asyncio import async_sessionmaker\n\\1",
            content,
            count=1,
        )
    # Fix all call sites: sessionmaker(...) → async_sessionmaker(...)
    content = re.sub(r"\bsessionmaker\(", "async_sessionmaker(", content)
    return content


def _sanitize_source_text(file_path: str, content: str) -> str:
    """Strip invisible characters that commonly break Python parsing."""
    if not file_path.endswith(".py"):
        return content
    cleaned = content
    for ch in _INVISIBLE_PY_CHARS:
        cleaned = cleaned.replace(ch, "")
    return cleaned


def _ensure_language_boilerplate(workspace: str, generated_files: list) -> list:
    """Deterministically create language-specific boilerplate that the LLM reliably omits.

    Runs after every execution path — zero LLM cost, safe to call unconditionally.
    Returns relative paths of all files created.

    Handled per language
    ─────────────────────────────────────────────────────────────────────────────
    Python    : __init__.py for every package directory (empty — always needed)
    Node / TS : minimal package.json if no .json manifest exists in the project
    Go        : minimal go.mod derived from workspace name
    Rust      : minimal Cargo.toml derived from workspace name
    Ruby      : minimal Gemfile (just the rubygems.org source line)
    PHP       : minimal composer.json (PSR-4 autoload for App\\ namespace)
    """
    created: list[str] = []

    # Gather extension + top-level manifest presence in one pass
    exts: set[str] = set()
    manifest_names: set[str] = set()
    for gf in generated_files:
        fp = gf.file_path.replace("\\", "/")
        ext = os.path.splitext(fp)[1].lower()
        exts.add(ext)
        if "/" not in fp:                         # top-level files only
            manifest_names.add(fp)

    app_name = os.path.basename(os.path.abspath(workspace)).lower().replace(" ", "-") or "app"

    # ── Python: __init__.py for every package directory ───────────────────────
    if ".py" in exts:
        py_dirs: set[str] = set()
        for gf in generated_files:
            fp = gf.file_path.replace("\\", "/")
            if fp.endswith(".py") and not fp.endswith("__init__.py"):
                parent = "/".join(fp.split("/")[:-1])
                if parent:
                    py_dirs.add(parent)
                    parts = parent.split("/")
                    for i in range(1, len(parts)):
                        py_dirs.add("/".join(parts[:i]))
        for pkg_dir in sorted(py_dirs):
            init_rel = f"{pkg_dir}/__init__.py"
            init_full = os.path.join(workspace, init_rel)
            if not os.path.exists(init_full):
                ensure_directory(os.path.dirname(init_full))
                open(init_full, "w").close()
                print(f"  [Executor] Created missing __init__.py: {init_rel}")
                created.append(init_rel)

    # ── Node / TypeScript: minimal package.json ───────────────────────────────
    _JS = {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}
    if exts & _JS and "package.json" not in manifest_names:
        pkg_path = os.path.join(workspace, "package.json")
        if not os.path.exists(pkg_path):
            content = json.dumps({"name": app_name, "version": "1.0.0", "private": True}, indent=2) + "\n"
            with open(pkg_path, "w") as f:
                f.write(content)
            print(f"  [Executor] Created missing package.json (minimal fallback)")
            created.append("package.json")

    # ── Go: minimal go.mod ────────────────────────────────────────────────────
    if ".go" in exts and "go.mod" not in manifest_names:
        gomod_path = os.path.join(workspace, "go.mod")
        if not os.path.exists(gomod_path):
            mod_name = app_name.replace("-", "_")
            with open(gomod_path, "w") as f:
                f.write(f"module {mod_name}\n\ngo 1.21\n")
            print(f"  [Executor] Created missing go.mod (minimal fallback)")
            created.append("go.mod")

    # ── Rust: minimal Cargo.toml ──────────────────────────────────────────────
    if ".rs" in exts and "Cargo.toml" not in manifest_names:
        cargo_path = os.path.join(workspace, "Cargo.toml")
        if not os.path.exists(cargo_path):
            with open(cargo_path, "w") as f:
                f.write(f"[package]\nname = \"{app_name}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n")
            print(f"  [Executor] Created missing Cargo.toml (minimal fallback)")
            created.append("Cargo.toml")

    # ── Ruby: minimal Gemfile ─────────────────────────────────────────────────
    if ".rb" in exts and "Gemfile" not in manifest_names:
        gemfile_path = os.path.join(workspace, "Gemfile")
        if not os.path.exists(gemfile_path):
            with open(gemfile_path, "w") as f:
                f.write("source 'https://rubygems.org'\n")
            print(f"  [Executor] Created missing Gemfile (minimal fallback)")
            created.append("Gemfile")

    # ── PHP: minimal composer.json ────────────────────────────────────────────
    if ".php" in exts and "composer.json" not in manifest_names:
        composer_path = os.path.join(workspace, "composer.json")
        if not os.path.exists(composer_path):
            content = json.dumps({
                "name": f"{app_name}/{app_name}",
                "autoload": {"psr-4": {"App\\": "src/"}},
                "require": {"php": ">=8.0"},
            }, indent=2) + "\n"
            with open(composer_path, "w") as f:
                f.write(content)
            print(f"  [Executor] Created missing composer.json (minimal fallback)")
            created.append("composer.json")

    return created


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

def _build_architecture_context(architecture: Architecture) -> str:
    """Build a full project export map so every file knows what's importable from every other file."""
    lines = ["Project file export map (every file and what it exports):"]
    for node in architecture.nodes:
        exports = node.contract.public_api if node.contract and node.contract.public_api else []
        if exports:
            lines.append(f"  {node.file_path}: {exports}")
        else:
            lines.append(f"  {node.file_path}")
    return "\n".join(lines)


EXECUTOR_SYSTEM_PROMPT = """You are an expert Senior Software Engineer.
Your goal is to implement the source code for the requested files based on the architecture contract.
Respond with the code for each file. If multiple files are requested, use a JSON format: {"file_path": "content"}.

CRITICAL RULES — violating any of these will break the build:
- Output ONLY raw source code. No explanations, no reasoning, no "I will..." text.
- Do NOT read files, search the workspace, or call any tools. Use only the context provided.
- Do NOT include markdown fences (```python etc.) unless the format explicitly requires JSON.
- Start your response with the first line of code, nothing before it.
- IMPORTS: Each dependency entry includes an "imports_available" list of names exported by that
  file. Import ONLY names that appear in that list. Do not invent or guess import names.
  Example dependency: {"file_path": "src/models.py", "imports_available": ["User", "Task", "Base"]}
  Correct import: `from .models import User, Task`  (not `from .models import Session` — not listed)
- For files inside a package directory (e.g. src/main.py inside package 'src'), use RELATIVE
  imports for sibling modules: `from . import crud` not `from src import crud`,
  `from .models import Task` not `from src.models import Task`.
- For web applications (FastAPI, Flask, Express, etc.) that live inside src/, ALWAYS generate
  a top-level run.py (or index.js) at the project root so the server can be started with
  `python run.py` (or `node index.js`) without needing to know the package structure.
- FastAPI + SQLAlchemy async projects:
  - Configure sessions with `async_sessionmaker(..., expire_on_commit=False)`.
  - When API responses include related ORM fields, eager-load relations (`selectinload`)
    before returning objects; do not rely on lazy loads during serialization.
  - Avoid code that triggers `sqlalchemy.exc.MissingGreenlet` at runtime.
- FastAPI-specific rules (violating these causes crashes or security holes):
  - main.py MUST import every router module it references:
    if you write `app.include_router(auth.router)` you must have `from .routers import auth`.
  - Every router file MUST import every function it calls:
    if you call `verify_password(...)` you must import `verify_password` at the top.
  - Never use `@app.on_event("startup"/"shutdown")` — it is deprecated. Always use the
    lifespan context manager: `@asynccontextmanager async def lifespan(app): ...startup...; yield; ...shutdown...`
    and pass it to `FastAPI(lifespan=lifespan)`.
  - Never hardcode `directory="src/static"` or `directory="src/templates"` in StaticFiles
    or Jinja2Templates. Always resolve relative to `__file__`:
    `StaticFiles(directory=str(Path(__file__).parent / "static"))`.
    Add `from pathlib import Path` at the top.
  - When a dependency function like `get_current_user` can return None (optional auth),
    ALWAYS check `if user is None: raise HTTPException(status_code=401)` before accessing
    any attribute of the returned user object.
  - ALWAYS set `response_model=` on endpoints that return ORM objects containing
    sensitive fields (passwords, tokens, secrets). Use a Pydantic schema that excludes them.
  - NEVER provide a fallback default for security-critical env vars. Use:
    `SECRET_KEY = os.getenv("SECRET_KEY")` then `if not SECRET_KEY: raise RuntimeError(...)`.
  - When reading a token from a cookie that was stored as `"Bearer <jwt>"`, strip the
    prefix before decoding: `if token.startswith("Bearer "): token = token[7:]`."""

EXECUTOR_USER_PROMPT_TEMPLATE = """Project Export Map (all files and what they export — use this to find what to import):
{architecture_context}

Target File: {file_path}
Purpose: {purpose}
Contract: {contract}

Direct Dependencies (files this file explicitly depends on, with exports confirmed):
{dependencies}

Implement the code for {file_path}. Import every name you use — consult the export map above
if a needed name is not in the direct dependencies list."""

BULK_EXECUTOR_USER_PROMPT_TEMPLATE = """Project Export Map (all files and what they export — use this to find what to import):
{architecture_context}

Files to implement:
{files_to_implement}

Each file entry has a "dependencies" list with "file_path" and "imports_available" fields.
These are the confirmed exports of that file. Import every name you use — consult the export
map above if a needed name is not in a file's direct dependencies list. Do not leave any
import unresolved.

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
        if max_bulk_files < 0 and env_max_bulk.lstrip("-").isdigit():
            max_bulk_files = int(env_max_bulk)

        # Adaptive concurrency: use CPU count if not specified
        if concurrency <= 0:
            self.concurrency = max(2, cpu_count() - 1) if cpu_count() else 4
        else:
            self.concurrency = concurrency
        # Adaptive batch sizing: larger batches for faster responses.
        # max_bulk_files semantics:
        #   < 0 : auto (adaptive default)
        #   = 0 : disable bulk generation (always wave-based)
        #   > 0 : bulk for projects with <= N files
        if max_bulk_files < 0:
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

        if self.max_bulk_files > 0 and len(exec_architecture.nodes) <= self.max_bulk_files:
            print(
                f"  [Executor] Small project detected ({len(exec_architecture.nodes)} files, "
                f"bulk limit: {self.max_bulk_files}). Using bulk generation."
            )
            result = await self._execute_bulk(exec_architecture)
            _ensure_language_boilerplate(self.workspace, result.generated_files)
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

        _ensure_language_boilerplate(self.workspace, generated_files)
        return ExecutionResult(
            generated_files=generated_files,
            skipped_nodes=skipped_nodes,
            failed_nodes=failed_nodes
        )

    async def _execute_bulk(self, architecture: Architecture) -> ExecutionResult:
        """Generates all files in a single LLM call."""
        node_map = {n.node_id: n for n in architecture.nodes}
        files_to_implement = []
        for node in architecture.nodes:
            dep_nodes = [node_map[d] for d in node.depends_on if d in node_map]
            files_to_implement.append({
                "file_path": node.file_path,
                "purpose": node.purpose,
                "contract": node.contract.__dict__ if node.contract else "None",
                "dependencies": [
                    {
                        "file_path": d.file_path,
                        "imports_available": d.contract.public_api if d.contract else [],
                    }
                    for d in dep_nodes
                ],
            })

        user_prompt = BULK_EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context=_build_architecture_context(architecture),
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
            _rp_err = _validate_write_path(self.workspace, node.file_path)
            if _rp_err:
                print(f"  {_rp_err} — skipping file")
                continue
            content = normalized[node.file_path]
            content = _normalize_encoding(content)
            content = _fix_relative_imports(node.file_path, content)
            content = _ensure_async_sessionmaker_guardrail(node.file_path, content)
            content = _fix_utcnow(node.file_path, content)
            content = _fix_httpx_async_transport(node.file_path, content)
            content = _fix_orm_sessionmaker(node.file_path, content)
            content = _sanitize_source_text(node.file_path, content)
            full_path = os.path.join(self.workspace, node.file_path)
            ensure_directory(os.path.dirname(full_path))
            with open(full_path, 'w') as f:
                f.write(content)
            _issues = check_file(node.file_path, content)
            if _issues:
                print(f"  [LiveGuard] {node.file_path}: {_issues[0]}")

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

        node_id_map = {n.node_id: n for n in executable_nodes}
        files_to_implement = [
            {
                "file_path": node.file_path,
                "purpose": node.purpose,
                "contract": node.contract.__dict__ if node.contract else "None",
                "dependencies": [
                    {
                        "file_path": node_id_map[d].file_path,
                        "imports_available": node_id_map[d].contract.public_api if node_id_map[d].contract else [],
                    }
                    for d in node.depends_on if d in node_id_map
                ],
            }
            for node in executable_nodes
        ]
        user_prompt = BULK_EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context=_build_architecture_context(architecture),
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
                _rp_err = _validate_write_path(self.workspace, file_path)
                if _rp_err:
                    print(f"  {_rp_err} — skipping file")
                    continue
                content = _strip_leading_prose(content, file_path)
                content = _normalize_encoding(content)
                content = _fix_relative_imports(file_path, content)
                content = _ensure_async_sessionmaker_guardrail(file_path, content)
                content = _fix_utcnow(file_path, content)
                content = _fix_httpx_async_transport(file_path, content)
                content = _fix_orm_sessionmaker(file_path, content)
                content = _sanitize_source_text(file_path, content)
                full_path = os.path.join(self.workspace, file_path)
                ensure_directory(os.path.dirname(full_path))
                with open(full_path, "w") as f:
                    f.write(content)
                _issues = check_file(file_path, content)
                if _issues:
                    print(f"  [LiveGuard] {file_path}: {_issues[0]}")
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
        node_map = {n.node_id: n for n in architecture.nodes}
        dep_nodes = [node_map[d] for d in node.depends_on if d in node_map]
        dependencies = [
            {
                "file_path": d.file_path,
                "imports_available": d.contract.public_api if d.contract else [],
            }
            for d in dep_nodes
        ]

        user_prompt = EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context=_build_architecture_context(architecture),
            file_path=node.file_path,
            purpose=node.purpose,
            contract=json.dumps(node.contract.__dict__) if node.contract else "None",
            dependencies=json.dumps(dependencies, indent=2) if dependencies else "[]"
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

        _rp_err = _validate_write_path(self.workspace, node.file_path)
        if _rp_err:
            raise ValueError(_rp_err)
        content = _normalize_encoding(content)
        content = _fix_relative_imports(node.file_path, content)
        content = _ensure_async_sessionmaker_guardrail(node.file_path, content)
        content = _fix_utcnow(node.file_path, content)
        content = _fix_httpx_async_transport(node.file_path, content)
        content = _fix_orm_sessionmaker(node.file_path, content)
        content = _sanitize_source_text(node.file_path, content)
        full_path = os.path.join(self.workspace, node.file_path)
        ensure_directory(os.path.dirname(full_path))

        with open(full_path, 'w') as f:
            f.write(content)
        _issues = check_file(node.file_path, content)
        if _issues:
            print(f"  [LiveGuard] {node.file_path}: {_issues[0]}")

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
            if os.environ.get("CODEGEN_STRICT_DEP_GRAPH", "0").strip() == "1":
                raise ValueError(
                    "Cycle detected or missing dependency in architecture. "
                    f"Remaining nodes: {remaining}"
                )

            # Degrade gracefully: execute unresolved nodes in deterministic fallback waves.
            # The dependency graph from LLM output is advisory for scheduling/context;
            # failing hard here aborts otherwise runnable projects.
            remaining_nodes = [node_map[nid] for nid in remaining if nid in node_map]

            def _priority(node: ExecutionNode) -> tuple[int, str]:
                path = node.file_path.lower()
                if path in {"requirements.txt", "pyproject.toml", "package.json"}:
                    rank = 0
                elif path.endswith("__init__.py"):
                    rank = 1
                elif "/tests/" in path or path.startswith("tests/"):
                    rank = 3
                else:
                    rank = 2
                return rank, path

            remaining_nodes.sort(key=_priority)
            chunk = max(1, self.concurrency)
            fallback_waves = [
                remaining_nodes[i:i + chunk]
                for i in range(0, len(remaining_nodes), chunk)
            ]
            print(
                "  [Executor] Warning: dependency cycle/missing edges detected; "
                f"falling back to {len(fallback_waves)} deterministic wave(s) "
                f"for {len(remaining_nodes)} unresolved node(s)."
            )
            waves.extend(fallback_waves)

        return waves
