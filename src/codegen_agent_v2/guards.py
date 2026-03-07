"""V2 Guards — all pre-write integrity checks in one place.

Every check runs BEFORE the file is written to disk.
If any check fails, the file is queued for individual retry.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass

# ── Patterns ──────────────────────────────────────────────────────────────

_TRUNC_BRACKET_RE  = re.compile(r'^\s*\[\.{2,}[^\]\n]*\]\s*$', re.MULTILINE)
# Catches last line that is indented + bare identifier (not a complete statement)
_MIDWORD_RE        = re.compile(r'\n([ \t]+)([a-z_][a-z0-9_]*)\s*$')
# Catches mid-expression: line ends after an operator, open paren, or comma
_MIDEXPR_RE        = re.compile(r'[,\(\[\{+\-\*\/=<>|&%]\s*$', re.MULTILINE)
# Catches comment-like truncation markers
_TRUNC_COMMENT_RE  = re.compile(r'#\s*(\.\.\.|truncated|omitted|rest of|continue[sd]?)(?:\s|$)', re.IGNORECASE)
_VALID_LAST_WORDS  = frozenset({
    'pass', 'break', 'continue', 'raise', 'yield',
    'true', 'false', 'none', 'else', 'finally',
})

# Agent internal symbols that should never appear in generated code
_AGENT_MARKERS = frozenset({
    "CachingLLMClient", "LLMRouter", "HEALER_SYSTEM_PROMPT",
    "EXECUTOR_SYSTEM_PROMPT", "PlannerArchitect", "_BulkFileParser",
    "HealingReport", "codegen_agent_v2.server", "LayerGateError",
})

# Minimum line counts by extension
_MIN_LINES: dict[str, int] = {
    ".html": 200, ".css": 300, ".js": 150, ".py": 15,
}
# Dashboard HTML pages need even more content
_MIN_LINES_DASHBOARD = 300

_SKIP_SIZE_CHECK = frozenset({
    "__init__.py", "requirements.txt", "pyproject.toml", "package.json",
    "go.mod", "Cargo.toml", ".gitignore", "pytest.ini", "setup.cfg",
    "alembic.ini",
})


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""
    detail: str = ""


# ── Individual checks ──────────────────────────────────────────────────────

def check_syntax(file_path: str, content: str) -> GuardResult:
    """Python AST parse — catches mid-statement truncations and any syntax error."""
    if not file_path.endswith(".py"):
        return GuardResult(ok=True)
    try:
        ast.parse(content)
        return GuardResult(ok=True)
    except SyntaxError as exc:
        return GuardResult(ok=False, reason="SyntaxError", detail=f"line {exc.lineno}: {exc.msg}")


def check_truncation(file_path: str, content: str) -> GuardResult:
    """Detect LLM truncation: [...] placeholder, mid-expression cut, or truncation comment."""
    # __init__.py files are intentionally empty — never flag them
    if os.path.basename(file_path) == "__init__.py":
        return GuardResult(ok=True)
    stripped = content.rstrip()
    if not stripped:
        return GuardResult(ok=False, reason="TruncationGuard", detail="empty content")
    # [...] placeholder anywhere in file
    if _TRUNC_BRACKET_RE.search(content):
        return GuardResult(ok=False, reason="TruncationGuard", detail="[...] placeholder found")
    # Comment like "# ... rest of implementation"
    if _TRUNC_COMMENT_RE.search(content):
        return GuardResult(ok=False, reason="TruncationGuard", detail="truncation comment found")
    # Last non-empty line ends with an operator/open-bracket (incomplete expression)
    # Skip this check for HTML files — closing tags like </html> contain > which
    # falsely matches the operator pattern.
    last_line = stripped.splitlines()[-1] if stripped else ""
    if not file_path.endswith(".html") and _MIDEXPR_RE.search(last_line):
        return GuardResult(ok=False, reason="TruncationGuard", detail=f"ends mid-expression: '{last_line.strip()[-30:]}'")
    # Last indented word is not a valid statement-ender
    m = _MIDWORD_RE.search(content)
    if m:
        word = m.group(2)
        if word not in _VALID_LAST_WORDS:
            return GuardResult(ok=False, reason="TruncationGuard", detail=f"ends mid-word: '{word}'")
    return GuardResult(ok=True)


def check_size(file_path: str, content: str) -> GuardResult:
    """Reject files that are too short to be real implementations."""
    basename = os.path.basename(file_path)
    if basename in _SKIP_SIZE_CHECK:
        return GuardResult(ok=True)
    ext = os.path.splitext(file_path)[1].lower()
    min_lines = _MIN_LINES.get(ext, 0)
    # Dashboard HTML pages need significantly more content
    if ext == ".html" and "dashboard" in file_path.lower():
        min_lines = _MIN_LINES_DASHBOARD
    if min_lines and content.count("\n") < min_lines:
        return GuardResult(
            ok=False, reason="SizeGuard",
            detail=f"{content.count(chr(10))} lines < minimum {min_lines} for {ext}"
        )
    return GuardResult(ok=True)


def check_stubs(file_path: str, content: str) -> GuardResult:
    """Detect functions whose body is just pass / ... / NotImplementedError."""
    if not file_path.endswith(".py"):
        return GuardResult(ok=True)
    basename = os.path.basename(file_path)
    if basename in ("__init__.py",) or basename.startswith("test_"):
        return GuardResult(ok=True)
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return GuardResult(ok=True)  # caught by check_syntax

    stubs: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        # Strip leading docstring
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        if not body:
            stubs.append(node.name)
            continue
        if len(body) == 1:
            stmt = body[0]
            if isinstance(stmt, ast.Pass):
                stubs.append(node.name)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
                stubs.append(node.name)
            elif isinstance(stmt, ast.Raise):
                exc = stmt.exc
                if exc and isinstance(exc, ast.Call):
                    fname = exc.func
                    name = (
                        fname.id if isinstance(fname, ast.Name)
                        else fname.attr if isinstance(fname, ast.Attribute)
                        else ""
                    )
                    if name in ("NotImplementedError", "NotImplemented"):
                        stubs.append(node.name)

    if stubs:
        return GuardResult(ok=False, reason="StubGuard", detail=f"unimplemented: {stubs}")
    return GuardResult(ok=True)


def check_injection(file_path: str, content: str) -> GuardResult:
    """Detect agent internals accidentally injected into generated code."""
    if not file_path.endswith(".py"):
        return GuardResult(ok=True)
    for marker in _AGENT_MARKERS:
        if marker in content:
            return GuardResult(ok=False, reason="InjectionGuard", detail=f"agent marker: {marker}")
    return GuardResult(ok=True)


# ── Combined entry point ──────────────────────────────────────────────────

def run_all(file_path: str, content: str) -> list[GuardResult]:
    """Run every guard. Returns list of failed GuardResults (empty = all pass)."""
    checks = [
        check_injection(file_path, content),
        check_truncation(file_path, content),
        check_syntax(file_path, content),
        check_size(file_path, content),
        check_stubs(file_path, content),
    ]
    return [r for r in checks if not r.ok]
