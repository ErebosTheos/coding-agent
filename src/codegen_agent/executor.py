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

# ── TruncationGuard + SizeGuard ───────────────────────────────────────────────
# Detects LLM output that was cut off mid-word (e.g. last line is `    to_enc`)
# or contains the [...] placeholder inserted by some models when truncating.
# Matches any "[... ...]" style truncation placeholder on its own line, including:
#   [...] / [... 331 chars omitted ...] / [... rest of code ...] / [... truncated ...]
_TRUNC_BRACKET_RE = re.compile(r'^\s*\[\.{2,}[^\]\n]*\]\s*$', re.MULTILINE)
# Lone indented identifier at EOF — potential mid-word truncation
_MIDWORD_TRUNCATION_RE = re.compile(r'\n([ \t]+)([a-z_][a-z0-9_]*)\s*$')
# Partial assignment value at EOF, e.g. `    default=F` (False cut to F)
_MIDASSIGN_TRUNCATION_RE = re.compile(r'\n[ \t]+\w+\s*=\s*[A-Z]\w{0,4}\s*$')
# Python keywords/builtins that are valid as the last line in a block
_VALID_LAST_WORDS = frozenset({
    'pass', 'return', 'break', 'continue', 'else', 'finally', 'raise', 'yield',
    'true', 'false', 'none', 'and', 'or', 'not', 'in', 'is',
})
# How many files per stream-bulk batch (prevents LLM context overflow on large projects)
_STREAM_CHUNK_SIZE = int(os.environ.get("CODEGEN_STREAM_CHUNK_SIZE", "10"))
# Minimum line counts by extension; files below this are re-generated individually
_MIN_LINES: dict[str, int] = {
    ".html": 40, ".css": 60, ".js": 30, ".ts": 25, ".tsx": 25,
    ".py": 12, ".go": 8, ".rs": 8,
}
_SKIP_SIZE_CHECK = frozenset({
    "__init__.py", "requirements.txt", "go.mod", "Cargo.toml",
    "package.json", "Gemfile", "composer.json", ".gitignore",
    "README.md", "conftest.py", "pyproject.toml",
})


def _is_content_truncated(content: str) -> bool:
    """True if content appears LLM-truncated (mid-word cut or [...] placeholder).

    Checks three patterns:
    1. [...] placeholder left by the LLM
    2. File ends with `    partial_ident` that isn't a valid Python statement keyword
    3. File ends with `    var=X` where X looks like a truncated value (e.g. `default=F`)
    """
    if bool(_TRUNC_BRACKET_RE.search(content)):
        return True
    if bool(_MIDASSIGN_TRUNCATION_RE.search(content)):
        return True
    m = _MIDWORD_TRUNCATION_RE.search(content)
    if m:
        word = m.group(2).lower()
        if word not in _VALID_LAST_WORDS:
            return True
    return False


def _is_content_too_short(file_path: str, content: str) -> bool:
    """True if a file has fewer lines than expected for its type — likely a stub."""
    if os.path.basename(file_path) in _SKIP_SIZE_CHECK:
        return False
    ext = os.path.splitext(file_path)[1].lower()
    min_lines = _MIN_LINES.get(ext, 0)
    return min_lines > 0 and content.count('\n') < min_lines


def _has_stub_functions(file_path: str, content: str) -> list[str]:
    """Return names of functions/methods whose body is just pass, ..., or a TODO comment.

    These are semantic stubs that pass the line-count check but have no real implementation.
    Only checks Python files outside of __init__.py and test files (where pass is valid).
    """
    if not file_path.endswith(".py"):
        return []
    basename = os.path.basename(file_path)
    if basename in ("__init__.py",) or basename.startswith("test_"):
        return []
    import ast as _ast
    try:
        tree = _ast.parse(content)
    except SyntaxError:
        return []
    stubs = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        body = node.body
        # Strip docstring
        if body and isinstance(body[0], _ast.Expr) and isinstance(body[0].value, _ast.Constant):
            body = body[1:]
        if not body:
            stubs.append(node.name)
            continue
        # Body is only pass / ... / raise NotImplementedError / TODO comment
        if len(body) == 1:
            stmt = body[0]
            if isinstance(stmt, _ast.Pass):
                stubs.append(node.name)
            elif isinstance(stmt, _ast.Expr) and isinstance(stmt.value, _ast.Constant) and stmt.value.value is ...:
                stubs.append(node.name)
            elif isinstance(stmt, _ast.Raise):
                exc = stmt.exc
                if exc and isinstance(exc, _ast.Call):
                    func = exc.func
                    name = func.id if isinstance(func, _ast.Name) else (func.attr if isinstance(func, _ast.Attribute) else "")
                    if name in ("NotImplementedError", "NotImplemented"):
                        stubs.append(node.name)
    return stubs


# ── InjectionGuard ────────────────────────────────────────────────────────────
# Strings that only appear in the agent's own source — never in generated projects.
# If found in a generated file, the LLM confused its training data with project code.
_AGENT_INJECTION_MARKERS: frozenset[str] = frozenset({
    "CachingLLMClient",
    "LLMRouter",
    "get_client_for_role",
    "HEALER_SYSTEM_PROMPT",
    "EXECUTOR_SYSTEM_PROMPT",
    "PlannerArchitect",
    "StreamingPlanArchExecutor",
    "from .caching_client import",
    "codegen_agent.llm",
    "_BulkFileParser",
    "HealingReport",
    "ExecutionNode",
})


def _has_agent_code_injection(file_path: str, content: str) -> bool:
    """True if the file contains the agent's own internal source code.

    The LLM occasionally injects its training data (agent internals) into
    generated project files when confused by 'router' or 'llm' filename hints.
    Such files are completely broken and must be regenerated.
    """
    if not file_path.endswith(".py"):
        return False
    return any(marker in content for marker in _AGENT_INJECTION_MARKERS)


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
    # Ensure `timezone` is importable after the substitution
    if "timezone" not in content:
        return content  # shouldn't happen, but be safe
    if "from datetime import" in content:
        # Append timezone to the existing from-import if not already present
        if re.search(r'from datetime import[^\n]*\btimezone\b', content) is None:
            content = re.sub(
                r"(from datetime import )([^\n]+)",
                lambda m: m.group(1) + m.group(2).rstrip() + ", timezone",
                content,
                count=1,
            )
    elif "import datetime" in content:
        # Only bare `import datetime` — add a separate from-import after it
        content = re.sub(
            r"(import datetime\b[^\n]*\n)",
            r"\1from datetime import timezone\n",
            content,
            count=1,
        )
    else:
        # No datetime import at all — prepend one
        content = "from datetime import timezone\n" + content
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


_CORS_WILDCARD_RE = re.compile(r'allow_origins\s*=\s*\[\s*["\']?\*["\']?\s*\]')


def _fix_cors_wildcard(file_path: str, content: str) -> str:
    """Replace insecure CORS allow_origins=['*'] with env-var-controlled origins.

    Hardcoded wildcard CORS is a security issue in production. Replacing with
    os.getenv allows deployment to lock it down without code changes.
    """
    if not file_path.endswith(".py") or "CORSMiddleware" not in content:
        return content
    if not _CORS_WILDCARD_RE.search(content):
        return content
    content = _CORS_WILDCARD_RE.sub('allow_origins=os.getenv("CORS_ORIGINS", "*").split(",")', content)
    # Ensure `import os` is present
    if "import os" not in content:
        content = "import os\n" + content
    return content


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

    # ── RouterGuard: register any unregistered FastAPI routers ───────────────
    # Must run after all __init__.py creation above so the package tree is complete.
    router_fixes = _fix_unregistered_routers(workspace, generated_files)
    created.extend(router_fixes)

    # ── StaticGuard: create static/ dir if StaticFiles is mounted ────────────
    _STATIC_DIR_RE = re.compile(r'StaticFiles\s*\(\s*directory\s*=\s*["\'](\w+)["\']')
    for gf in generated_files:
        if not gf.file_path.endswith(".py") or "StaticFiles" not in (gf.content or ""):
            continue
        for m in _STATIC_DIR_RE.finditer(gf.content):
            dir_name = m.group(1)
            for base in (os.path.dirname(os.path.join(workspace, gf.file_path)), workspace):
                static_dir = os.path.join(base, dir_name)
                index_html = os.path.join(static_dir, "index.html")
                if not os.path.exists(static_dir):
                    os.makedirs(static_dir, exist_ok=True)
                    print(f"  [StaticGuard] Created missing directory: {os.path.relpath(static_dir, workspace)}")
                if not os.path.exists(index_html):
                    with open(index_html, "w", encoding="utf-8") as f:
                        f.write(
                            '<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="UTF-8">'
                            '<title>App</title></head>\n<body><h1>Welcome</h1></body>\n</html>\n'
                        )
                    rel = os.path.relpath(index_html, workspace)
                    print(f"  [StaticGuard] Created stub: {rel}")
                    created.append(rel)
                break  # only create once

    return created


def _fix_unregistered_routers(workspace: str, generated_files: list) -> list[str]:
    """Detect FastAPI router files that exist on disk but are never registered in main.py.

    For every `<pkg>/routers/<name>.py` that defines `router = APIRouter()` and is NOT
    imported/included in the main entry point, this function injects the correct
    `from .<pkg>.routers import <name>` + `app.include_router(<name>.router)` lines.

    Returns list of fixed file paths.
    """
    _ROUTER_DEF_RE = re.compile(r'\brouter\s*=\s*APIRouter\s*\(')
    _INCLUDE_RE = re.compile(r'app\.include_router\(\s*(\w+)\.router')
    _IMPORT_RE = re.compile(r'from\s+[\.\w]+\s+import\s+(\w+)')

    # Find the main entry point file
    main_candidates = ["main.py", "app.py", "run.py"]
    main_path: str | None = None
    main_pkg: str | None = None
    for gf in generated_files:
        bn = os.path.basename(gf.file_path)
        if bn in main_candidates and gf.file_path.endswith(".py"):
            disk = os.path.join(workspace, gf.file_path)
            if os.path.exists(disk):
                main_path = disk
                # package prefix: "src" if file is src/main.py, "" if top-level
                parts = gf.file_path.replace("\\", "/").split("/")
                main_pkg = parts[0] if len(parts) > 1 else ""
                break
    if not main_path:
        return []

    main_content = open(main_path, encoding="utf-8", errors="replace").read()
    # Skip non-FastAPI files
    if "FastAPI" not in main_content and "APIRouter" not in main_content:
        return []

    # Names already registered
    registered = set(_INCLUDE_RE.findall(main_content))
    imported = set(_IMPORT_RE.findall(main_content))

    # Find all router files not yet registered
    to_add: list[tuple[str, str]] = []  # (module_name, import_line)
    for gf in generated_files:
        fp = gf.file_path.replace("\\", "/")
        if not fp.endswith(".py") or "router" not in fp.lower():
            continue
        if "test" in fp.lower() or "__init__" in fp:
            continue
        mod_name = os.path.splitext(os.path.basename(fp))[0]  # e.g. "admin"
        if mod_name in registered or mod_name in imported:
            continue
        # Verify it actually defines a router
        disk = os.path.join(workspace, fp)
        if not os.path.exists(disk):
            continue
        try:
            file_content = open(disk, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if not _ROUTER_DEF_RE.search(file_content):
            continue
        # Build the import path relative to main.py's package
        fp_parts = fp.split("/")
        if main_pkg and fp_parts[0] == main_pkg:
            # Same package: from .routers.admin import router → from .routers import admin
            rel_parts = fp_parts[1:-1]   # e.g. ["routers"]
            import_path = "." + ".".join(rel_parts) if rel_parts else "."
        else:
            # Different package or top-level
            import_path = "." + ".".join(fp_parts[:-1]) if len(fp_parts) > 1 else "."
        to_add.append((mod_name, f"from {import_path} import {mod_name}"))

    if not to_add:
        return []

    # Inject imports after the last existing import block
    lines = main_content.splitlines()
    last_import_idx = 0
    for i, line in enumerate(lines):
        if line.startswith(("import ", "from ")) or (line.strip().startswith("from ") and "import" in line):
            last_import_idx = i

    # Build injection: imports first, then include_router calls near app definition
    import_lines = [imp for _, imp in to_add]
    include_lines = [f"app.include_router({mod}.router)" for mod, _ in to_add]

    # Find where to inject include_router (after last include_router or after app= definition)
    last_include_idx = 0
    for i, line in enumerate(lines):
        if "include_router" in line or "FastAPI(" in line:
            last_include_idx = i

    new_lines = list(lines)
    # Insert in reverse order so indices stay valid
    for inc in reversed(include_lines):
        new_lines.insert(last_include_idx + 1, inc)
    for imp in reversed(import_lines):
        new_lines.insert(last_import_idx + 1, imp)

    new_content = "\n".join(new_lines)
    if new_content != main_content:
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        fixed_rel = os.path.relpath(main_path, workspace).replace("\\", "/")
        print(f"  [RouterGuard] Registered {len(to_add)} missing router(s) in {fixed_rel}: {[m for m, _ in to_add]}")
        return [fixed_rel]
    return []


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

def _load_project_context(workspace: str) -> str:
    """Load project_context.json if it exists — built by ProjectContextBuilder from the architecture."""
    ctx_file = os.path.join(workspace, "project_context.json")
    if not os.path.exists(ctx_file):
        return ""
    try:
        import json as _json
        data = _json.loads(open(ctx_file, encoding="utf-8").read())
        lines = ["=== PROJECT STRUCTURE (source of truth for imports/exports) ===\n"]
        for rel, info in data.items():
            exports = info.get("exports", [])
            import_from = info.get("import_from", {})
            purpose = info.get("purpose", "")
            routes = info.get("routes", [])
            lines.append(f"FILE: {rel}")
            if purpose:
                lines.append(f"  purpose: {purpose}")
            if exports:
                lines.append(f"  exports: {', '.join(exports)}")
            if import_from:
                for src, names in import_from.items():
                    lines.append(f"  import from {src}: {', '.join(names)}")
            if routes:
                lines.append(f"  routes: {', '.join(routes)}")
            lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""


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


def _extract_api_endpoints(backend_files: list[GeneratedFile]) -> str:
    """Scan generated backend files for route decorators and return a summary string."""
    _ROUTE_RE = re.compile(
        r'@(?:app|router)\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']',
        re.MULTILINE,
    )
    endpoints: list[str] = []
    for gf in backend_files:
        if not gf.file_path.endswith(".py"):
            continue
        for m in _ROUTE_RE.finditer(gf.content):
            endpoints.append(f"{m.group(1).upper()} {m.group(2)}")
    if not endpoints:
        return "No explicit endpoints found — infer from project brief."
    return "\n".join(endpoints[:60])  # cap at 60 to avoid bloating the prompt


def _verify_contract_exports(file_path: str, content: str, public_api: list[str]) -> list[str]:
    """Return names declared in public_api but not found as top-level definitions in the file.

    Catches cases where the LLM forgot to implement a planned export — the healer can then
    prioritise patching the file before its dependents are generated.
    Supports Python, JS/TS. Other languages: skips check and returns [].
    """
    if not public_api or not content:
        return []
    ext = os.path.splitext(file_path)[1].lower()
    missing: list[str] = []

    if ext == ".py":
        for name in public_api:
            pat = re.compile(
                rf"^(def|class|async\s+def)\s+{re.escape(name)}\b"
                rf"|^{re.escape(name)}\s*[=:(]",
                re.MULTILINE,
            )
            if not pat.search(content):
                missing.append(name)

    elif ext in {".js", ".ts", ".tsx", ".jsx"}:
        for name in public_api:
            pat = re.compile(
                rf"export\s+(default\s+)?(function|class|const|let|var|async\s+function)\s+{re.escape(name)}\b"
                rf"|export\s*\{{[^}}]*\b{re.escape(name)}\b",
                re.MULTILINE,
            )
            if not pat.search(content):
                missing.append(name)

    return missing


def _extract_dep_api_surface(file_path: str, content: str, max_lines: int = 60) -> str:
    """Extract just the public signature lines from a generated dependency file.

    Keeps the prompt compact while giving the dependent LLM the exact names,
    signatures, and types it needs to write correct import statements.
    """
    ext = os.path.splitext(file_path)[1].lower()
    lines = content.splitlines()
    surface: list[str] = []

    if ext == ".py":
        for line in lines:
            s = line.strip()
            if (
                s.startswith("def ")
                or s.startswith("async def ")
                or s.startswith("class ")
                or s.startswith("@")
                or (s and not s.startswith("#") and "=" in s and not s.startswith(" "))
                or s.startswith("from ")
                or s.startswith("import ")
            ):
                surface.append(line)
    elif ext in {".js", ".ts", ".tsx", ".jsx"}:
        for line in lines:
            s = line.strip()
            if s.startswith("export ") or s.startswith("import ") or s.startswith("interface ") or s.startswith("type "):
                surface.append(line)
    else:
        surface = lines[:max_lines]

    return "\n".join(surface[:max_lines]) if surface else content[:1200]


def _node_complexity_tier(node: "ExecutionNode") -> str:  # type: ignore[name-defined]
    """Classify a node as 'simple', 'standard', or 'complex' for model tier selection.

    complex  → auth/security/database/entrypoint files, or nodes with ≥5 dependencies
    simple   → config/schema/enum/init files, or leaf nodes with no dependencies
    standard → everything else
    """
    path = node.file_path.lower()
    num_deps = len(node.depends_on)

    _COMPLEX_HINTS = ("auth", "security", "database", "main.py", "app.py",
                      "middleware", "jwt", "oauth", "permission", "encryption")
    _SIMPLE_HINTS  = ("__init__", "config", "constants", "enums", "enum",
                      "types", "schemas", "schema", "settings", "migrations")

    if any(h in path for h in _COMPLEX_HINTS) or num_deps >= 5:
        return "complex"
    if any(h in path for h in _SIMPLE_HINTS) or num_deps == 0:
        return "simple"
    return "standard"


EXECUTOR_SYSTEM_PROMPT = """You are an expert Senior Software Engineer.
Your goal is to implement the source code for the requested files based on the architecture contract.
Respond with the code for each file. If multiple files are requested, use a JSON format: {"file_path": "content"}.

ANTI-TRUNCATION MANDATE — enforced by an automated validator that REJECTS short output:
- Write EVERY file completely from first line to last line. No exceptions.
- If you are low on output tokens, FINISH the current file before starting the next key.
  An incomplete file causes cascading import errors — worse than omitting it entirely.
- NEVER end a file mid-function, mid-class, mid-string, or with a partial line.
- Minimum line requirements (files shorter than this are auto-rejected and re-requested):
    HTML: 80 lines  |  CSS: 200 lines  |  JS: 80 lines  |  Python: 20 lines (non-init)
- Write every function body in full. `pass` is forbidden in production code.
- For CSS: write ALL component styles (navbar, hero, cards, buttons, forms, footer, responsive).
- For JS: implement EVERY function body shown in the outline — not stubs.
- For HTML: include the complete document — navbar, all sections, footer, all forms.

CRITICAL RULES — violating any of these will break the build:
- Output ONLY raw source code. No explanations, no reasoning, no "I will..." text.
- Do NOT read files, search the workspace, or call any tools. Use only the context provided.
- Do NOT include markdown fences (```python etc.) unless the format explicitly requires JSON.
- Start your response with the first line of code, nothing before it.
- IMPORTS: Each dependency entry includes an "imports_available" list of names exported by that
  file. Import ONLY names that appear in that list. Do not invent or guess import names.
  Example dependency: {"file_path": "src/models.py", "imports_available": ["User", "Task", "Base"]}
  Correct import: `from .models import User, Task`  (not `from .models import Session` — not listed)
- IMPORT STYLE: always use relative imports within a package. Derive the relative path by
  stripping the shared package prefix. Examples (all files are inside the 'src' package):
    src/main.py importing src/api/routers/auth.py    → `from .api.routers import auth`
    src/api/routers/auth.py importing src/database.py → `from ...database import get_db`
    src/api/routers/auth.py importing src/models/user.py → `from ...models.user import User`
    src/services/auth_service.py importing src/models/user.py → `from ..models.user import User`
  Count the dots: one dot per directory level you climb before descending to the target.
  NEVER use absolute imports like `from src.models import User` inside the src package.
- For web applications (FastAPI, Flask, Express, etc.) that live inside src/, ALWAYS generate
  a top-level run.py (or index.js) at the project root so the server can be started with
  `python run.py` (or `node index.js`) without needing to know the package structure.
- FastAPI + SQLAlchemy async projects:
  - Configure sessions with `async_sessionmaker(..., expire_on_commit=False)`.
  - When API responses include related ORM fields, eager-load relations (`selectinload`)
    before returning objects; do not rely on lazy loads during serialization.
  - Avoid code that triggers `sqlalchemy.exc.MissingGreenlet` at runtime.
  - get_db MUST NOT auto-commit. The correct pattern is:
    ```python
    async def get_db() -> AsyncGenerator[AsyncSession, None]:
        async with AsyncSessionLocal() as session:
            yield session
            # NO commit here — endpoints commit explicitly after mutations
    ```
    Endpoints that mutate data call `await session.commit()` themselves. get_db never commits.
  - If the architecture includes an AuditLog model, EVERY create/update/delete endpoint MUST
    write an AuditLog entry after committing the main change:
    `db.add(AuditLog(user_id=current_user.id, action="created_course", resource_id=obj.id))`
    `await db.commit()`
    Do not generate read-only audit endpoints without the corresponding write calls.
- FastAPI-specific rules (violating these causes crashes or security holes):
  - IMPORT PATH DERIVATION RULE (most common source of ImportError):
    Always derive the Python import path directly from the file's path in the file_tree.
    Convert the file path to a dotted module path by replacing "/" with "." and dropping ".py".
    Examples:
      src/api/routers/auth.py     → `from src.api.routers import auth`  (absolute)
                                    OR `from .api.routers import auth`   (relative from src/)
      src/api/routers/courses.py  → `from src.api.routers import courses`
      src/routers/auth.py         → `from src.routers import auth`
      src/services/auth_service.py → `from src.services import auth_service`
    NEVER use `from .routers import auth` if the file is actually at src/api/routers/auth.py.
    NEVER guess — look at the file_tree and derive mechanically.
  - main.py MUST import every router module it references before calling app.include_router().
    Define `app = FastAPI(...)` BEFORE any `app.include_router(...)` call.
  - run.py MUST import `app` from main before using it. Always put all imports at the top.
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
  - For SECRET_KEY, always provide a dev fallback so the app starts in development without env setup:
    `SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")`
    Log a warning if running with the default: `if SECRET_KEY == "dev-secret-change-in-production": print("WARNING: Using default SECRET_KEY")`
  - When reading a token from a cookie that was stored as `"Bearer <jwt>"`, strip the
    prefix before decoding: `if token.startswith("Bearer "): token = token[7:]`.
  - Alembic + async SQLAlchemy rules (violating these breaks migrations):
    - alembic/env.py MUST use AsyncEngine + run_async_migrations pattern:
      ```python
      from logging.config import fileConfig
      from sqlalchemy.ext.asyncio import async_engine_from_config
      from sqlalchemy import pool
      from alembic import context
      from src.database import Base
      import src.models  # noqa: F401 — import all models so metadata is populated

      config = context.config
      fileConfig(config.config_file_name)
      target_metadata = Base.metadata

      def run_migrations_offline(): ...  # standard alembic offline

      def do_run_migrations(connection):
          context.configure(connection=connection, target_metadata=target_metadata)
          with context.begin_transaction():
              context.run_migrations()

      async def run_migrations_online():
          connectable = async_engine_from_config(
              config.get_section(config.config_ini_section),
              prefix="sqlalchemy.", poolclass=pool.NullPool)
          async with connectable.connect() as connection:
              await connection.run_sync(do_run_migrations)
          await connectable.dispose()

      if context.is_offline_mode():
          run_migrations_offline()
      else:
          import asyncio; asyncio.run(run_migrations_online())
      ```
    - alembic.ini sqlalchemy.url must use the same env var as the app:
      `sqlalchemy.url = %(DATABASE_URL)s` and read it via `config.set_main_option`
      in env.py: `config.set_main_option("sqlalchemy.url", os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./app.db"))`
    - Every model module MUST be imported in env.py before `Base.metadata` is used.
    - Generate an initial migration: `migrations/versions/0001_initial.py` with
      `upgrade()` calling `op.create_table(...)` for every entity.
    - The seed script MUST use the same async session as the app and insert in FK-safe order.
  - ALWAYS disable Swagger UI and ReDoc in the FastAPI constructor unless explicitly
    asked to expose them. Use:
    `docs_url="/docs" if os.getenv("ENABLE_DOCS") else None, redoc_url=None`
    This prevents accidental API exposure in production deployments.
  - EVERY auth system MUST implement forgot-password and reset-password endpoints:
    POST /api/v1/auth/forgot-password (accepts email, returns reset token in dev)
    POST /api/v1/auth/reset-password (accepts token + new_password, updates hash, clears token)
    IMPORTANT: Only add reset_token and reset_token_expiry columns to User if they are already
    defined in the User model in the architecture. NEVER invent columns not in the model schema.
    If the model does not have these columns, skip the reset flow and return 501 Not Implemented.
  - NEVER use passlib for password hashing — it is broken on Python 3.13+.
    Always use bcrypt directly:
    `import bcrypt`
    `bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()`
    `bcrypt.checkpw(plain.encode(), hashed.encode())`
    Add `bcrypt` (not `passlib[bcrypt]`) to requirements.txt.

REQUIREMENTS.TXT PACKAGE VERSION RULES:
- ALWAYS use minimum-version pins (>=) not exact pins (==) so pip can resolve compatible wheels.
- Use ONLY these minimum versions — older versions lack pre-built wheels for Python 3.12+:
    fastapi>=0.115.0
    uvicorn[standard]>=0.30.0
    sqlalchemy>=2.0.0
    alembic>=1.13.0
    pydantic>=2.10.0
    pydantic-settings>=2.5.0
    python-jose[cryptography]>=3.3.0
    python-multipart>=0.0.12
    httpx>=0.27.0
    bcrypt>=4.1.0
    pytest>=8.0.0
    pytest-asyncio>=0.23.0
    anyio>=4.4.0
- Do NOT pin to pydantic==1.x or pydantic==2.0..2.9 — those have no Python 3.13/3.14 wheels.
- Do NOT use exact pins for packages that need compiled extensions (pydantic, cryptography, bcrypt).

FRONTEND QUALITY RULES (applies to every .html, .css, .js file):
- Write production-quality, visually stunning UI. Aim for the polish level of Stripe, Linear,
  or Vercel. Every page should look like a real product, not a tutorial demo.
- HTML rules:
  - Write COMPLETE documents — all sections must have real, meaningful copy and real data.
  - NEVER write placeholder text ("Content goes here", "Lorem ipsum", "TODO"). Write actual content.
  - Every page needs: sticky responsive navbar, hero/banner, main content sections, footer.
  - Use semantic HTML5 elements: <header>, <nav>, <main>, <section>, <article>, <footer>.
  - Include ARIA labels on interactive elements. Add skip-to-content link.
  - Link Google Fonts in <head>: <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
- CSS rules (style.css must be comprehensive — 300+ lines minimum):
  - Use CSS custom properties at :root for a full design system:
    --bg: #09090b;  --surface: #18181b;  --surface-2: #27272a;  --border: #3f3f46;
    --primary: #6366f1;  --primary-hover: #4f46e5;  --primary-glow: rgba(99,102,241,0.3);
    --accent: #8b5cf6;  --success: #22c55e;  --warning: #f59e0b;  --danger: #ef4444;
    --text: #fafafa;  --text-muted: #a1a1aa;  --text-subtle: #52525b;
    --radius: 12px;  --radius-sm: 8px;  --radius-lg: 16px;
    --shadow: 0 4px 24px rgba(0,0,0,0.5);  --shadow-glow: 0 0 40px rgba(99,102,241,0.2);
    --font: 'Inter', system-ui, -apple-system, sans-serif;
    --transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  - Typography scale: hero h1 = 56px/700, section h2 = 36px/700, card h3 = 20px/600,
    body = 16px/400, small = 13px/400. Use clamp() for fluid sizing.
  - Navbar: sticky top-0, backdrop-filter: blur(20px), background: rgba(9,9,11,0.85),
    border-bottom: 1px solid var(--border). Logo bold, nav links spaced 32px.
  - Hero section: min-height 90vh, animated gradient background or mesh gradient,
    large headline with gradient text (background-clip: text), subheading, 2 CTA buttons.
    Add a subtle animated background: radial-gradient pulsing or floating blobs.
  - Cards: background var(--surface), border 1px solid var(--border), border-radius var(--radius),
    padding 24px, hover: border-color var(--primary), box-shadow var(--shadow-glow),
    transform: translateY(-2px). Transition all 0.2s.
  - Buttons: primary = gradient(135deg, var(--primary), var(--accent)), color #fff,
    padding 12px 24px, border-radius var(--radius-sm), font-weight 600,
    hover: translateY(-1px) + brighter gradient, active: translateY(0).
    Outline variant: transparent bg, border 1.5px solid var(--primary), color var(--primary).
  - Form inputs: bg var(--surface-2), border 1px solid var(--border), border-radius var(--radius-sm),
    padding 12px 16px, color var(--text), focus: border-color var(--primary),
    box-shadow 0 0 0 3px var(--primary-glow). Floating labels or clean labeled inputs.
  - Responsive grid for cards: display grid, grid-template-columns repeat(auto-fit,minmax(280px,1fr)), gap 24px.
  - Animations: @keyframes fadeInUp (opacity 0→1, translateY 20px→0, 0.5s ease),
    @keyframes pulse (scale 1→1.05→1), @keyframes shimmer for loading skeletons.
    Apply .animate-fade-in-up with staggered animation-delay to card sections.
  - Mobile-first: all layouts must work on 320px screens. Use media queries at 640px, 768px, 1024px.
  - Glassmorphism panels where appropriate: background rgba(255,255,255,0.03),
    backdrop-filter blur(12px), border 1px solid rgba(255,255,255,0.08).
- JavaScript rules (app.js must be comprehensive — real interactivity, not stubs):
  - JWT auth flow: login form → POST /api/v1/auth/login (form-encoded) → store token in localStorage
    → redirect to role-specific dashboard. Logout clears localStorage, redirects to /login.html.
  - On page load: check localStorage for token → if present and on login page, redirect to dashboard.
  - All API calls: include Authorization: Bearer <token> header, handle 401 by logging out.
  - Loading states: show spinner/skeleton while fetch is in-flight. Use CSS class toggling.
  - Toast notification system: success (green), error (red), info (blue) — auto-dismiss after 3s.
    Position: fixed bottom-right, stack multiple toasts.
  - NEVER use setTimeout to simulate authentication or API calls. ALWAYS call the real backend.
    Bad: `setTimeout(() => { window.location.href = '/dashboard.html'; }, 800)`
    Good: `const data = await fetch('/api/v1/auth/login', {...}); localStorage.setItem('token', data.access_token);`
  - Forgot password flow: "Forgot password?" link opens a modal. Modal POSTs to
    /api/v1/auth/forgot-password, displays the returned reset_token, then lets user enter
    new password and POST to /api/v1/auth/reset-password.
  - If the project brief mentions accessibility, visual impairment, or screen readers:
    Include a persistent floating accessibility toolbar with buttons: +A (font up), −A (font down),
    Contrast (toggle high-contrast class on body), Dyslexia (toggle dyslexia-mode class on body).
    Store all preferences in localStorage and restore on page load.
    Position: fixed bottom-center, z-index 9999. Apply to EVERY html page.
  - Smooth section transitions: fade-out current section, fade-in new section.
  - Mobile hamburger menu that toggles nav links.
  - Any data tables: sortable columns, search/filter input, pagination controls.
  - Error handling: try/catch every fetch, show friendly messages not raw stack traces.
- dashboard.html: real authenticated UI — sidebar nav with icons, stat cards (with numbers and
  trend arrows), a data table or list view, quick-action buttons. NOT a blank page.
- NEVER submit incomplete, stub, or placeholder frontend code. Write EVERYTHING."""

FRONTEND_SYSTEM_PROMPT = """You are a Senior Frontend Engineer. Your only job is to write complete, \
production-quality HTML, CSS, and JavaScript for a web application.
Think: Stripe, Linear, Vercel level of polish. Every page must feel like a real product.

DESIGN SYSTEM — use these exact CSS custom properties in style.css and all HTML files:
:root {
  --bg: #09090b;  --surface: #18181b;  --surface-2: #27272a;  --border: #3f3f46;
  --primary: #6366f1;  --primary-hover: #4f46e5;  --primary-glow: rgba(99,102,241,0.3);
  --accent: #8b5cf6;  --success: #22c55e;  --warning: #f59e0b;  --danger: #ef4444;
  --text: #fafafa;  --text-muted: #a1a1aa;  --text-subtle: #52525b;
  --radius: 12px;  --radius-sm: 8px;  --shadow: 0 4px 24px rgba(0,0,0,0.5);
  --shadow-glow: 0 0 40px rgba(99,102,241,0.2);
  --font: 'Inter', system-ui, -apple-system, sans-serif;
  --transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

HTML RULES:
- Every HTML file: Google Fonts Inter import, skip-to-content link, ARIA labels, semantic HTML5.
- NEVER write placeholder text. All content must be real and specific to the project.
- All pages: sticky navbar with logo + nav links + accessibility buttons (+A/-A, contrast toggle), footer.
- index.html must have: hero (min-height 90vh, animated gradient), stats bar, feature cards
  relevant to the project, testimonials or highlights section, CTA section, contact form, footer.
- login.html: centered card, relevant credential fields, gradient login button, error area.
  Include a role selector only if the project has multiple user roles.
- Dashboard pages: sidebar layout, stat cards relevant to the project, real data tables/panels.
  sidebar: dark background, nav items with icons, active state. main: scrollable content area.
- Admin/manager dashboards: include summary charts (SVG donut or CSS bar) for key metrics.
- All forms: labelled inputs, focus glow ring, never outline:none anywhere.
- Focus indicators on ALL interactive elements: outline: 3px solid var(--primary); outline-offset: 2px.
- High contrast: [data-theme="high-contrast"] overrides (bg:#000, text:#fff, links:#ffff00).
- Font size: [data-fontsize="large"] bumps root font-size. Dyslexia: [data-font="dyslexia"].
- ARIA live region: <div aria-live="polite" id="announcer" class="sr-only"></div> in every page.

CSS RULES (style.css must be 300+ lines):
- Full :root design system above + Google Fonts @import.
- CSS reset: *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
- Navbar: sticky top:0, backdrop-filter:blur(20px), background:rgba(9,9,11,0.85), z-index:100.
- Hero: animated gradient @keyframes gradientShift. Gradient text via background-clip:text.
- Cards: hover translateY(-2px) + var(--shadow-glow) transition.
- Buttons: primary=gradient(135deg,var(--primary),var(--accent)). Outline variant.
- Sidebar: width 260px, var(--surface), full height, icon+label nav items.
- Progress bars, toasts (slideIn), modals (backdrop blur), skeleton shimmer animation.
- Responsive: 768px sidebar hidden, hamburger menu. 1024px full layout.
- All @keyframes: fadeInUp, slideIn, gradientShift, shimmer, pulse.
- .sr-only utility class for screen reader only content.

JS RULES (app.js must be 200+ lines):
- Auth: login()→POST /api/v1/auth/login→store token. fetchWithAuth()→Bearer header, 401→logout.
- On load: restore accessibility prefs (theme, font size, dyslexia) from localStorage.
- Toast: showToast(msg,type) auto-dismiss 3s, stacks.
- Accessibility helpers: announceToScreenReader, toggleHighContrast, updateFontSize, toggleDyslexiaFont.
- Dashboard loaders that call fetchWithAuth() and render data into DOM.
- Test engine: startTest, renderQuestion, autoSubmitOnTimeout.
- Charts: renderBarChart (CSS flex), renderDonutChart (SVG stroke-dasharray). No external libs.

ANTI-TRUNCATION (enforced — output is validated by line count):
- Write EVERY file completely. style.css MUST be ≥300 lines. app.js MUST be ≥200 lines.
  Each .html page MUST be ≥80 lines. Files shorter than this are auto-rejected and re-requested.
- Complete each file's content string before starting the next JSON key.
- An incomplete file is immediately detected and penalises the overall build score.

OUTPUT: JSON object {"file_path": "complete content", ...}. No markdown fences. No explanation."""

FRONTEND_USER_PROMPT_TEMPLATE = """Project: {project_name}
Brief: {brief}

Backend API endpoints (use these exact paths in fetchWithAuth calls):
{api_endpoints}

Files to implement:
{files_to_implement}

Return JSON: {{"static/index.html": "<full content>", "static/style.css": "<full content>", ...}}
Write every file completely."""

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

Implement ALL the files listed above. Return a JSON object where keys are file paths and values are the COMPLETE file contents.

COMPLETENESS REQUIREMENTS:
- Every file must be COMPLETE — no placeholder comments, no truncation, no "rest of code here".
- HTML files: minimum 80 lines. CSS files: minimum 200 lines. JS files: minimum 80 lines.
- Python files: every function must have a real body, not just `pass`.
- Write all files in the JSON object. Do not omit any file from the list.
- If you are low on output tokens, complete the current file before starting the next JSON key.

Example format:
{{
  "src/main.py": "from fastapi import FastAPI\\n...complete file...",
  "src/models.py": "from sqlalchemy import...\\n...complete file..."
}}
"""

class Executor:
    def __init__(
        self,
        llm_client: LLMClient,
        workspace: str,
        concurrency: int = -1,
        max_bulk_files: int = -1,
        tier_clients: "dict[str, LLMClient] | None" = None,
    ):
        self.llm_client = llm_client
        self.workspace = workspace
        # Optional per-tier clients: {"simple": <haiku>, "complex": <opus>}
        # Falls back to llm_client for any missing tier.
        self._tier_clients: dict[str, LLMClient] = tier_clients or {}
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

    @staticmethod
    def _is_frontend_node(node: ExecutionNode) -> bool:
        """Returns True if this node is a frontend file (HTML/CSS/JS in static/)."""
        p = node.file_path.replace("\\", "/").lower()
        return (
            p.startswith("static/") or
            (("static" in p or "public" in p) and p.endswith((".html", ".css", ".js")))
        )

    async def execute(self, architecture: Architecture) -> ExecutionResult:
        """Two-phase execution: backend first, then frontend with a dedicated UI prompt."""
        executable_nodes = [n for n in architecture.nodes if not _is_directory_path(n.file_path)]
        skipped_nodes = [n.node_id for n in architecture.nodes if _is_directory_path(n.file_path)]
        if not executable_nodes:
            return ExecutionResult(generated_files=[], skipped_nodes=skipped_nodes, failed_nodes=[])

        backend_nodes = [n for n in executable_nodes if not self._is_frontend_node(n)]
        frontend_nodes = [n for n in executable_nodes if self._is_frontend_node(n)]

        all_generated: list[GeneratedFile] = []
        all_failed: list[str] = []
        all_skipped: list[str] = list(skipped_nodes)

        # ── Phase 1: Backend ────────────────────────────────────────────────
        if backend_nodes:
            backend_arch = Architecture(
                file_tree=architecture.file_tree,
                nodes=backend_nodes,
                global_validation_commands=architecture.global_validation_commands,
            )
            print(f"  [Executor] Phase 1 — Backend: {len(backend_nodes)} files")
            # _stream_bulk auto-chunks into batches of _STREAM_CHUNK_SIZE (15)
            # keeping each prompt under Gemini CLI's 32k char transport limit.
            # _execute_bulk sends all files in one prompt and blows past the limit.
            if self.max_bulk_files > 0 and len(backend_nodes) <= _STREAM_CHUNK_SIZE:
                result = await self._execute_bulk(backend_arch)
            else:
                result = await self._stream_bulk(backend_arch)
            all_generated.extend(result.generated_files)
            all_failed.extend(result.failed_nodes)
            all_skipped.extend(result.skipped_nodes)

        # ── Phase 2: Frontend ───────────────────────────────────────────────
        if frontend_nodes:
            print(f"  [Executor] Phase 2 — Frontend: {len(frontend_nodes)} files")
            frontend_arch = Architecture(
                file_tree=architecture.file_tree,
                nodes=frontend_nodes,
                global_validation_commands=architecture.global_validation_commands,
            )
            result = await self._execute_frontend(frontend_arch, all_generated)
            all_generated.extend(result.generated_files)
            all_failed.extend(result.failed_nodes)
            all_skipped.extend(result.skipped_nodes)

        _ensure_language_boilerplate(self.workspace, all_generated)
        return ExecutionResult(
            generated_files=all_generated,
            skipped_nodes=all_skipped,
            failed_nodes=all_failed,
        )

    async def _execute_waves(self, architecture: Architecture) -> ExecutionResult:
        """Wave-based execution for large backend projects."""
        waves = self._calculate_waves(architecture.nodes)
        generated_files = []
        failed_nodes = []
        semaphore = asyncio.Semaphore(self.concurrency)

        async def execute_with_limit(node: ExecutionNode) -> tuple[ExecutionNode, GeneratedFile | Exception]:
            async with semaphore:
                result = await self._execute_node(node, architecture)
                return (node, result)

        for wave in waves:
            results = await asyncio.gather(*[execute_with_limit(n) for n in wave], return_exceptions=True)
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

        return ExecutionResult(generated_files=generated_files, failed_nodes=failed_nodes)

    async def _execute_frontend(
        self, architecture: Architecture, backend_files: list[GeneratedFile]
    ) -> ExecutionResult:
        """Generates all frontend files in one focused LLM call with FRONTEND_SYSTEM_PROMPT."""
        # Extract API endpoints from generated backend files for context
        api_endpoints = _extract_api_endpoints(backend_files)

        # Build project name/brief from workspace path
        project_name = os.path.basename(self.workspace)

        files_to_implement = [
            {"file_path": n.file_path, "purpose": n.purpose,
             "contract": n.contract.__dict__ if n.contract else {}}
            for n in architecture.nodes
        ]

        project_context = _load_project_context(self.workspace)
        user_prompt = FRONTEND_USER_PROMPT_TEMPLATE.format(
            project_name=project_name,
            brief=(project_context or _build_architecture_context(architecture))[:4000],
            api_endpoints=api_endpoints,
            files_to_implement=json.dumps(files_to_implement, indent=2),
        )

        try:
            response = await self.llm_client.generate(user_prompt, system_prompt=FRONTEND_SYSTEM_PROMPT)
        except Exception as exc:
            print(f"  [Executor] Frontend phase LLM error: {exc} — falling back to backend executor")
            return await self._execute_bulk(architecture)

        data = find_json_in_text(response)
        if not data or not isinstance(data, dict):
            print("  [Executor] Frontend phase returned invalid JSON — falling back to bulk")
            return await self._execute_bulk(architecture)

        node_map = {n.file_path: n for n in architecture.nodes}
        generated_files = []
        for file_path, content in data.items():
            if not isinstance(content, str) or not content.strip():
                continue
            _rp_err = _validate_write_path(self.workspace, file_path)
            if _rp_err:
                print(f"  {_rp_err} — skipping file")
                continue
            content = _normalize_encoding(content)
            full_path = os.path.join(self.workspace, file_path)
            ensure_directory(os.path.dirname(full_path))
            with open(full_path, "w") as f:
                f.write(content)
            node = node_map.get(file_path)
            print(f"  [Executor] Created frontend file: {file_path} ({content.count(chr(10))} lines)")
            generated_files.append(GeneratedFile(
                file_path=file_path,
                content=content,
                node_id=node.node_id if node else file_path,
                sha256=calculate_sha256(content),
            ))

        # If the LLM missed any planned files, fall back individually
        generated_paths = {gf.file_path for gf in generated_files}
        missed = [n for n in architecture.nodes if n.file_path not in generated_paths]
        if missed:
            print(f"  [Executor] Frontend phase missed {len(missed)} files — filling via bulk")
            missed_arch = Architecture(
                file_tree=architecture.file_tree,
                nodes=missed,
                global_validation_commands=[],
            )
            fallback = await self._execute_bulk(missed_arch)
            generated_files.extend(fallback.generated_files)

        return ExecutionResult(generated_files=generated_files)

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
            architecture_context=(_load_project_context(self.workspace) or _build_architecture_context(architecture)),
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
        truncated_bulk_nodes = []
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
            content = _fix_cors_wildcard(node.file_path, content)
            content = _fix_orm_sessionmaker(node.file_path, content)
            content = _sanitize_source_text(node.file_path, content)

            # InjectionGuard + TruncationGuard + SizeGuard
            if _has_agent_code_injection(node.file_path, content):
                print(f"  [InjectionGuard] {node.file_path} contains agent internal code — queuing retry")
                truncated_bulk_nodes.append(node)
                continue
            if _is_content_truncated(content) or _is_content_too_short(node.file_path, content):
                print(f"  [TruncationGuard] {node.file_path} is short/truncated ({content.count(chr(10))} lines) — queuing retry")
                truncated_bulk_nodes.append(node)
                continue
            stub_fns = _has_stub_functions(node.file_path, content)
            if stub_fns:
                print(f"  [StubGuard] {node.file_path} has unimplemented functions: {stub_fns} — queuing retry")
                truncated_bulk_nodes.append(node)
                continue

            _issues = check_file(node.file_path, content)
            if _issues:
                print(f"  [LiveGuard] {node.file_path}: {_issues[0]} — queuing retry")
                truncated_bulk_nodes.append(node)
                continue

            full_path = os.path.join(self.workspace, node.file_path)
            ensure_directory(os.path.dirname(full_path))
            with open(full_path, 'w') as f:
                f.write(content)

            if node.contract and node.contract.public_api:
                _missing = _verify_contract_exports(node.file_path, content, node.contract.public_api)
                if _missing:
                    print(f"  [ContractGuard] {node.file_path}: missing exports {_missing}")

            print(f"  [Executor] Created file: {node.file_path}")
            generated_files.append(GeneratedFile(
                file_path=node.file_path,
                content=content,
                node_id=node.node_id,
                sha256=calculate_sha256(content)
            ))

        # Retry truncated/short/injected files individually
        if truncated_bulk_nodes:
            print(f"  [TruncationGuard] Retrying {len(truncated_bulk_nodes)} bulk file(s) individually...")
            for tnode in truncated_bulk_nodes:
                try:
                    retry_gf = await self._execute_node(tnode, architecture)
                    retry_ok = (
                        not _is_content_truncated(retry_gf.content)
                        and not _is_content_too_short(tnode.file_path, retry_gf.content)
                        and not _has_agent_code_injection(tnode.file_path, retry_gf.content)
                    )
                    if retry_ok:
                        generated_files.append(retry_gf)
                        print(f"  [TruncationGuard] Retry OK: {tnode.file_path}")
                    else:
                        print(f"  [TruncationGuard] Retry still bad for {tnode.file_path} — skipping (healer will fix)")
                        bad_path = os.path.join(self.workspace, tnode.file_path)
                        if os.path.exists(bad_path):
                            os.remove(bad_path)
                except Exception as e:
                    print(f"  [TruncationGuard] Retry failed for {tnode.file_path}: {e}")

        return ExecutionResult(generated_files=generated_files)

    async def _stream_bulk(self, architecture: Architecture, system_prompt: str | None = None) -> ExecutionResult:
        """Generates all files in one LLM call, writing each file as its JSON value
        arrives in the stream — combines bulk consistency with streaming latency.

        For large projects (> _STREAM_CHUNK_SIZE nodes), automatically splits into
        sequential batches to prevent LLM context overflow and file truncation.
        Falls back to _execute_bulk (non-streaming) if the client has no astream().
        """
        if system_prompt is None:
            system_prompt = EXECUTOR_SYSTEM_PROMPT
        if not hasattr(self.llm_client, "astream"):
            return await self._execute_bulk(architecture)

        executable_nodes = [n for n in architecture.nodes if not _is_directory_path(n.file_path)]
        if not executable_nodes:
            return ExecutionResult(generated_files=[])

        # ── Chunk large projects to avoid context overflow ────────────────────
        if len(executable_nodes) > _STREAM_CHUNK_SIZE:
            return await self._stream_bulk_chunked(executable_nodes, architecture, system_prompt)

        return await self._stream_bulk_single(executable_nodes, architecture, system_prompt)

    async def _stream_bulk_chunked(
        self,
        nodes: list,
        architecture: "Architecture",
        system_prompt: str,
    ) -> "ExecutionResult":
        """Split nodes into sequential batches of _STREAM_CHUNK_SIZE.

        Processes batches in dependency-topological order so that when batch N
        runs, batch N-1's files are already on disk and available as dep context.
        After each batch, extracts ground-truth contracts (ORM columns, Pydantic
        fields, auth sub claim) and injects them into the next batch's prompt to
        prevent cross-batch contract drift.
        """
        waves = self._calculate_waves(nodes)
        # Flatten waves into a topologically ordered list, then rechunk
        ordered = [n for wave in waves for n in wave]
        chunks = [ordered[i : i + _STREAM_CHUNK_SIZE] for i in range(0, len(ordered), _STREAM_CHUNK_SIZE)]
        print(f"  [Executor] Splitting {len(nodes)} files into {len(chunks)} batch(es) of ≤{_STREAM_CHUNK_SIZE}")

        all_generated: list["GeneratedFile"] = []
        all_paths: set[str] = set()
        prior_contract: str = ""

        for idx, chunk in enumerate(chunks):
            print(f"  [Executor] Batch {idx + 1}/{len(chunks)}: {[n.file_path for n in chunk]}")
            chunk_arch = Architecture(
                file_tree=architecture.file_tree,
                nodes=chunk,
                global_validation_commands=[],
            )
            result = await self._stream_bulk_single(chunk, chunk_arch, system_prompt, prior_contract=prior_contract)
            batch_files = []
            for gf in result.generated_files:
                if gf.file_path not in all_paths:
                    all_generated.append(gf)
                    all_paths.add(gf.file_path)
                    batch_files.append(gf)
            # Extract contracts from this batch so the next batch can't drift
            prior_contract = self._extract_batch_contract(all_generated)

        return ExecutionResult(generated_files=all_generated)

    async def _stream_bulk_single(
        self,
        executable_nodes: list,
        architecture: "Architecture",
        system_prompt: str,
        prior_contract: str = "",
    ) -> "ExecutionResult":
        """Core streaming bulk generation for a single batch of nodes.

        Writes files as they arrive in the stream. After the stream, any file
        that is truncated or suspiciously short is individually retried via
        _execute_node — preventing stubs from propagating to the healer.

        prior_contract: ground-truth contract block extracted from previous batches.
        Injected at the top of the prompt so the LLM cannot drift from already-written
        ORM columns, schema fields, or auth conventions.
        """
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
        base_prompt = BULK_EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context=(_load_project_context(self.workspace) or _build_architecture_context(architecture)),
            files_to_implement=json.dumps(files_to_implement, indent=2),
        )
        user_prompt = (prior_contract + base_prompt) if prior_contract else base_prompt
        user_prompt = prune_prompt(user_prompt, max_chars=28_000)

        node_map = {node.file_path: node for node in executable_nodes}
        generated_files: list[GeneratedFile] = []
        generated_paths: set[str] = set()
        truncated_nodes: list = []   # files that need individual retry
        parser = _BulkFileParser()

        async for chunk in self.llm_client.astream(user_prompt, system_prompt=system_prompt):
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
                content = _fix_cors_wildcard(file_path, content)
                content = _fix_orm_sessionmaker(file_path, content)
                content = _sanitize_source_text(file_path, content)

                # ── InjectionGuard + TruncationGuard + SizeGuard ─────────────
                _node = node_map[file_path]
                if _has_agent_code_injection(file_path, content):
                    print(f"  [InjectionGuard] {file_path} contains agent internal code — queuing for retry")
                    truncated_nodes.append(_node)
                    generated_paths.add(file_path)
                    continue
                if _is_content_truncated(content) or _is_content_too_short(file_path, content):
                    line_count = content.count('\n')
                    print(
                        f"  [TruncationGuard] {file_path} is truncated/short "
                        f"({line_count} lines) — queuing for individual retry"
                    )
                    truncated_nodes.append(_node)
                    generated_paths.add(file_path)   # mark as "seen" to skip wave fallback
                    continue
                stub_fns = _has_stub_functions(file_path, content)
                if stub_fns:
                    print(f"  [StubGuard] {file_path} has unimplemented functions: {stub_fns} — queuing for individual retry")
                    truncated_nodes.append(_node)
                    generated_paths.add(file_path)
                    continue

                _issues = check_file(file_path, content)
                if _issues:
                    print(f"  [LiveGuard] {file_path}: {_issues[0]} — queuing for individual retry")
                    truncated_nodes.append(_node)
                    generated_paths.add(file_path)
                    continue

                full_path = os.path.join(self.workspace, file_path)
                ensure_directory(os.path.dirname(full_path))
                with open(full_path, "w") as f:
                    f.write(content)
                lines = content.count('\n')
                print(f"  ✓ {file_path} ({lines} lines)")
                if _node.contract and _node.contract.public_api:
                    _missing = _verify_contract_exports(file_path, content, _node.contract.public_api)
                    if _missing:
                        print(f"  [ContractGuard] {file_path}: missing exports {_missing}")
                print(f"  [Executor] Created file: {file_path} ({content.count(chr(10))} lines)")
                generated_files.append(GeneratedFile(
                    file_path=file_path,
                    content=content,
                    node_id=_node.node_id,
                    sha256=calculate_sha256(content),
                ))
                generated_paths.add(file_path)

        # ── Retry truncated / short / injected files individually ───────────────
        if truncated_nodes:
            print(f"  [TruncationGuard] Retrying {len(truncated_nodes)} file(s) individually...")
            for tnode in truncated_nodes:
                try:
                    retry_gf = await self._execute_node(tnode, architecture)
                    retry_ok = (
                        not _is_content_truncated(retry_gf.content)
                        and not _is_content_too_short(tnode.file_path, retry_gf.content)
                        and not _has_agent_code_injection(tnode.file_path, retry_gf.content)
                    )
                    if retry_ok:
                        generated_files.append(retry_gf)
                        print(f"  [TruncationGuard] Retry OK: {tnode.file_path} ({retry_gf.content.count(chr(10))} lines)")
                    else:
                        print(f"  [TruncationGuard] Retry still bad for {tnode.file_path} — skipping (healer will fix)")
                        # Remove the bad file from disk so healer doesn't try to patch corrupt content
                        bad_path = os.path.join(self.workspace, tnode.file_path)
                        if os.path.exists(bad_path):
                            os.remove(bad_path)
                except Exception as e:
                    print(f"  [TruncationGuard] Retry failed for {tnode.file_path}: {e}")

        # ── Wave-based fallback for files the stream never emitted ──────────────
        missing = [n for n in executable_nodes if n.file_path not in generated_paths]
        if missing:
            print(
                f"  [Executor] Stream-bulk missing {len(missing)} file(s): "
                f"{[n.file_path for n in missing]}. Filling with wave-based."
            )
            missing_arch = Architecture(
                file_tree=architecture.file_tree,
                nodes=missing,
                global_validation_commands=[],
            )
            fallback = await self._execute_wave_fallback(missing_arch)
            generated_files.extend(fallback.generated_files)

        return ExecutionResult(generated_files=generated_files)

    async def _execute_wave_fallback(self, architecture: Architecture) -> ExecutionResult:
        """Fallback for failed bulk generation."""
        waves = self._calculate_waves(architecture.nodes)
        return await self._execute_waves_from_list(waves, architecture)

    async def _execute_waves_from_list(self, waves: List[List[ExecutionNode]], architecture: Architecture) -> ExecutionResult:
        """Execute pre-computed waves (used by the bulk-fallback path)."""
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

    def _select_client(self, node: ExecutionNode) -> LLMClient:
        """Return the appropriate LLM client for this node's complexity tier."""
        if not self._tier_clients:
            return self.llm_client
        tier = _node_complexity_tier(node)
        return self._tier_clients.get(tier, self.llm_client)

    def _build_dep_context(self, dep_nodes: list[ExecutionNode]) -> list[dict]:
        """Build dependency context, enriching with actual on-disk API surface when available."""
        deps: list[dict] = []
        for d in dep_nodes:
            entry: dict = {
                "file_path": d.file_path,
                "imports_available": d.contract.public_api if d.contract else [],
            }
            # Read actual generated content from disk — only available in wave mode
            # where dependencies were generated in a prior wave.
            disk_path = os.path.join(self.workspace, d.file_path)
            if os.path.exists(disk_path):
                try:
                    actual = open(disk_path, encoding="utf-8", errors="replace").read()
                    surface = _extract_dep_api_surface(d.file_path, actual)
                    if surface.strip():
                        entry["actual_api_surface"] = surface
                except OSError:
                    pass
            deps.append(entry)
        return deps

    async def _execute_node(self, node: ExecutionNode, architecture: Architecture) -> GeneratedFile:
        """Executes a single node (generates a file)."""
        node_map = {n.node_id: n for n in architecture.nodes}
        dep_nodes = [node_map[d] for d in node.depends_on if d in node_map]
        dependencies = self._build_dep_context(dep_nodes)

        user_prompt = EXECUTOR_USER_PROMPT_TEMPLATE.format(
            architecture_context=(_load_project_context(self.workspace) or _build_architecture_context(architecture)),
            file_path=node.file_path,
            purpose=node.purpose,
            contract=json.dumps(node.contract.__dict__) if node.contract else "None",
            dependencies=json.dumps(dependencies, indent=2) if dependencies else "[]"
        )
        user_prompt = prune_prompt(user_prompt, max_chars=14_000)

        client = self._select_client(node)
        content = await client.generate(
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

        # InjectionGuard: if agent source code leaked into output, retry once with explicit warning
        if _has_agent_code_injection(node.file_path, content):
            print(f"  [InjectionGuard] _execute_node: agent code in {node.file_path} — retrying with injection warning")
            injection_prompt = (
                f"CRITICAL: Your previous output for {node.file_path} contained internal agent source code "
                f"(e.g. CachingLLMClient, LLMRouter, HealingReport). This is wrong — you must ONLY write "
                f"the actual application code for this file. Do not include any agent internals.\n\n"
            ) + user_prompt
            content = await client.generate(injection_prompt, system_prompt=EXECUTOR_SYSTEM_PROMPT)
            code_blocks = extract_code_from_markdown(content)
            if code_blocks:
                content = code_blocks[0]
            else:
                content = _strip_leading_prose(content, node.file_path)
            if _has_agent_code_injection(node.file_path, content):
                print(f"  [InjectionGuard] Retry still injected for {node.file_path} — raising to skip write")
                raise ValueError(f"InjectionGuard: {node.file_path} still contains agent code after retry")

        # Log a warning if the individual file output looks truncated — the healer will catch it
        if _is_content_truncated(content):
            print(f"  [TruncationGuard] Warning: _execute_node output for {node.file_path} may be truncated")
        elif _is_content_too_short(node.file_path, content):
            print(f"  [TruncationGuard] Warning: {node.file_path} is short ({content.count(chr(10))} lines)")

        _rp_err = _validate_write_path(self.workspace, node.file_path)
        if _rp_err:
            raise ValueError(_rp_err)
        content = _normalize_encoding(content)
        content = _fix_relative_imports(node.file_path, content)
        content = _ensure_async_sessionmaker_guardrail(node.file_path, content)
        content = _fix_utcnow(node.file_path, content)
        content = _fix_httpx_async_transport(node.file_path, content)
        content = _fix_cors_wildcard(node.file_path, content)
        content = _fix_orm_sessionmaker(node.file_path, content)
        content = _sanitize_source_text(node.file_path, content)
        full_path = os.path.join(self.workspace, node.file_path)
        ensure_directory(os.path.dirname(full_path))

        with open(full_path, 'w') as f:
            f.write(content)
        _issues = check_file(node.file_path, content)
        if _issues:
            print(f"  [LiveGuard] {node.file_path}: {_issues[0]}")

        # Contract verification: warn if planned exports are missing from the generated file
        if node.contract and node.contract.public_api:
            _missing = _verify_contract_exports(node.file_path, content, node.contract.public_api)
            if _missing:
                print(f"  [ContractGuard] {node.file_path}: missing exports {_missing}")

        tier = _node_complexity_tier(node)
        print(f"  [Executor] Created file: {node.file_path} (tier={tier})")

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

    def _extract_batch_contract(self, generated_files: list) -> str:
        """AST-scan files written so far and return a ground-truth contract block.

        Extracts:
        - SQLAlchemy ORM model columns (class name → col_name: type)
        - Pydantic schema fields (class name → field: type)
        - JWT sub claim convention (which user field is used as the sub value)

        The returned string is prepended to the next batch's prompt so the LLM
        cannot invent field names, types, or auth conventions that contradict what
        is already on disk.
        """
        import ast as _ast

        orm_models: dict[str, dict[str, str]] = {}
        pydantic_schemas: dict[str, dict[str, str]] = {}
        auth_sub_field: str | None = None

        for gf in generated_files:
            if not gf.file_path.endswith(".py"):
                continue
            try:
                tree = _ast.parse(gf.content)
            except SyntaxError:
                continue

            for node in _ast.walk(tree):
                if not isinstance(node, _ast.ClassDef):
                    continue

                is_orm = any(
                    isinstance(s, _ast.Assign)
                    and any(isinstance(t, _ast.Name) and t.id == "__tablename__" for t in s.targets)
                    for s in node.body
                )
                is_pydantic = any(
                    (isinstance(b, _ast.Name) and b.id == "BaseModel")
                    or (isinstance(b, _ast.Attribute) and b.attr == "BaseModel")
                    for b in node.bases
                )

                if is_orm:
                    cols: dict[str, str] = {}
                    for stmt in node.body:
                        if not isinstance(stmt, _ast.Assign):
                            continue
                        for target in stmt.targets:
                            if not (isinstance(target, _ast.Name) and not target.id.startswith("_")):
                                continue
                            if not isinstance(stmt.value, _ast.Call):
                                continue
                            func = stmt.value.func
                            func_name = (
                                func.id if isinstance(func, _ast.Name)
                                else func.attr if isinstance(func, _ast.Attribute)
                                else ""
                            )
                            if func_name in ("Column", "mapped_column") and stmt.value.args:
                                col_type = _ast.unparse(stmt.value.args[0])
                                cols[target.id] = col_type
                    if cols:
                        orm_models[node.name] = cols

                if is_pydantic:
                    fields: dict[str, str] = {}
                    for stmt in node.body:
                        if (
                            isinstance(stmt, _ast.AnnAssign)
                            and isinstance(stmt.target, _ast.Name)
                            and not stmt.target.id.startswith("_")
                            and stmt.target.id != "model_config"
                        ):
                            fields[stmt.target.id] = _ast.unparse(stmt.annotation)
                    if fields:
                        pydantic_schemas[node.name] = fields

            # Detect JWT sub claim from auth/security files
            if auth_sub_field is None and any(
                kw in gf.file_path for kw in ("auth", "security", "token")
            ):
                try:
                    for node in _ast.walk(_ast.parse(gf.content)):
                        if isinstance(node, _ast.Dict):
                            for k, v in zip(node.keys, node.values):
                                if isinstance(k, _ast.Constant) and k.value == "sub":
                                    auth_sub_field = _ast.unparse(v)
                                    break
                        if auth_sub_field:
                            break
                except Exception:
                    pass

        if not orm_models and not pydantic_schemas and not auth_sub_field:
            return ""

        lines = [
            "=" * 70,
            "CROSS-FILE CONTRACT — EXTRACTED FROM ALREADY-WRITTEN FILES",
            "You MUST match these definitions exactly. Do NOT invent new field names,",
            "column names, or change types. Deviating causes runtime errors.",
            "=" * 70,
        ]

        if orm_models:
            lines.append("\nORM MODEL COLUMNS (SQLAlchemy — source of truth):")
            for class_name, cols in orm_models.items():
                lines.append(f"  class {class_name}:")
                for col_name, col_type in cols.items():
                    lines.append(f"    {col_name} = Column({col_type})")
            lines.append(
                "  RULE: Only reference column names listed above. "
                "NEVER use a field not defined here (e.g. reset_token_expiry, is_active "
                "if not listed). NEVER change a column's type."
            )

        if pydantic_schemas:
            lines.append("\nPYDANTIC SCHEMA FIELDS (source of truth):")
            for class_name, fields in pydantic_schemas.items():
                lines.append(f"  class {class_name}(BaseModel):")
                for fname, ftype in fields.items():
                    lines.append(f"    {fname}: {ftype}")
            lines.append(
                "  RULE: Schema field names MUST match ORM column names exactly. "
                "No invented fields. No type mismatches (e.g. bool vs String)."
            )

        if auth_sub_field:
            lines.append(f"\nJWT SUB CLAIM: `{auth_sub_field}`")
            lines.append(
                f"  RULE: The JWT 'sub' claim is set to `{auth_sub_field}` at login. "
                "ALL code that decodes the token and looks up the user MUST query "
                f"by the same field as `{auth_sub_field}`. "
                "NEVER mix email-based sub with username-based lookup or vice versa."
            )

        lines.append("=" * 70 + "\n")
        return "\n".join(lines) + "\n"
