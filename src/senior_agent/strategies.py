from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import difflib
import hashlib
import json
import logging
from collections import OrderedDict
import re
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import subprocess
import threading
from uuid import uuid4

from senior_agent.llm_client import LLMClient, LLMClientError, parse_streamed_response
from senior_agent.models import (
    FailureContext,
    FailureType,
    FileRollback,
    FixOutcome,
)
from senior_agent.symbol_graph import SymbolGraph
from senior_agent.utils import is_within_workspace

logger = logging.getLogger(__name__)

_PROMPT_RESPONSE_CACHE: OrderedDict[str, str] = OrderedDict()
_PROMPT_RESPONSE_CACHE_LOCK = threading.Lock()


def _prompt_cache_get(cache_key: str) -> str | None:
    with _PROMPT_RESPONSE_CACHE_LOCK:
        cached = _PROMPT_RESPONSE_CACHE.get(cache_key)
        if cached is not None:
            _PROMPT_RESPONSE_CACHE.move_to_end(cache_key)
        return cached


def _prompt_cache_set(cache_key: str, response: str, max_entries: int) -> None:
    with _PROMPT_RESPONSE_CACHE_LOCK:
        _PROMPT_RESPONSE_CACHE[cache_key] = response
        _PROMPT_RESPONSE_CACHE.move_to_end(cache_key)
        while len(_PROMPT_RESPONSE_CACHE) > max_entries:
            _PROMPT_RESPONSE_CACHE.popitem(last=False)

_SUPPORTED_SOURCE_EXTENSIONS = (
    "py",
    "js",
    "jsx",
    "ts",
    "tsx",
    "go",
    "rs",
    "java",
    "c",
    "h",
    "cpp",
    "cc",
    "cxx",
    "hpp",
    "hh",
    "kt",
    "kts",
)

_ERROR_FILE_PATTERN = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:"
    + "|".join(re.escape(ext) for ext in _SUPPORTED_SOURCE_EXTENSIONS)
    + r"))(?::(?P<line>\d+))?(?::(?P<column>\d+))?"
)
_FENCED_CODE_PATTERN = re.compile(
    r"```(?:[a-zA-Z0-9_+-]+)?\n(?P<code>[\s\S]*?)```",
    re.MULTILINE,
)
_HUNK_HEADER_PATTERN = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _build_diff_summary(
    workspace_root: Path,
    file_path: Path,
    before_text: str,
    after_text: str,
    max_hunks: int = 3,
) -> tuple[str, ...]:
    relative_file = file_path.relative_to(workspace_root)
    if before_text == after_text:
        return (f"No textual change in {relative_file}.",)

    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=str(relative_file),
            tofile=str(relative_file),
            n=0,
            lineterm="",
        )
    )
    added = 0
    removed = 0
    hunks: list[str] = []
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        elif line.startswith("@@"):
            match = _HUNK_HEADER_PATTERN.search(line)
            if match:
                old_line = match.group(1)
                new_line = match.group(2)
                hunks.append(f"Hunk at {relative_file}: old line {old_line}, new line {new_line}.")

    summary: list[str] = [f"Modified {relative_file}: +{added}/-{removed} lines."]
    before_bytes = before_text.encode("utf-8", errors="ignore")
    after_bytes = after_text.encode("utf-8", errors="ignore")
    byte_delta = len(after_bytes) - len(before_bytes)
    summary.append(
        f"Byte delta for {relative_file}: {byte_delta:+d} bytes (before={len(before_bytes)}, after={len(after_bytes)})."
    )
    first_divergence = None
    for index, (before_byte, after_byte) in enumerate(zip(before_bytes, after_bytes)):
        if before_byte != after_byte:
            first_divergence = index
            break
    if first_divergence is None and len(before_bytes) != len(after_bytes):
        first_divergence = min(len(before_bytes), len(after_bytes))
    if first_divergence is not None:
        summary.append(
            f"First byte divergence at offset {first_divergence} in {relative_file}."
        )
    summary.extend(hunks[:max_hunks])
    return tuple(summary)


def _validate_regex_pattern(pattern: str, strategy_name: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            f"Invalid regex pattern for strategy '{strategy_name}': {pattern!r}. "
            f"Regex error: {exc}"
        ) from exc


@dataclass(frozen=True)
class NoopStrategy:
    name: str = "noop"
    reason: str = "No-op strategy did not apply any changes."

    def apply(self, context: FailureContext) -> FixOutcome:
        """Return a non-applying outcome for baseline and test flows."""
        return FixOutcome(applied=False, note=self.reason)


@dataclass(frozen=True)
class _ErrorFileReference:
    path: str
    line_number: int | None = None


@dataclass(frozen=True)
class _PythonDefinitionSpan:
    name: str
    start_line: int
    end_line: int
    source: str


@dataclass(frozen=True)
class LLMStrategy:
    """Ask an LLM for a full-file fix using error-driven repository context."""

    llm_client: LLMClient
    fallback_llm_clients: tuple[LLMClient, ...] = ()
    name: str = "llm_strategy"
    allowed_failures: set[FailureType] | None = None
    max_context_files: int = 3
    max_file_chars: int = 20000
    max_output_chars: int = 500000
    max_growth_factor: float = 6.0
    min_retention_ratio: float = 0.1
    min_original_chars_for_retention_check: int = 200
    max_control_char_ratio: float = 0.02
    context_chunk_radius: int = 50
    max_chunk_line_multiplier: float = 4.0
    max_error_chars: int = 12000
    enable_symbol_context: bool = True
    max_symbol_targets: int = 3
    max_symbol_dependents_per_target: int = 2
    symbol_dependent_snippet_radius: int = 12
    max_symbol_context_chars: int = 8000
    enable_lsp_context: bool = True
    lsp_timeout_seconds: float = 5.0
    max_lsp_context_chars: int = 4000
    enable_streaming_speculative_parsing: bool = True
    enable_response_cache: bool = True
    response_cache_max_entries: int = 256
    cache_namespace: str = field(default_factory=lambda: uuid4().hex, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_context_files < 1:
            raise ValueError("max_context_files must be >= 1")
        if self.max_file_chars < 1:
            raise ValueError("max_file_chars must be >= 1")
        if self.max_output_chars < 1:
            raise ValueError("max_output_chars must be >= 1")
        if self.max_growth_factor <= 1.0:
            raise ValueError("max_growth_factor must be > 1.0")
        if not 0 < self.min_retention_ratio <= 1.0:
            raise ValueError("min_retention_ratio must be in (0, 1].")
        if self.min_original_chars_for_retention_check < 1:
            raise ValueError("min_original_chars_for_retention_check must be >= 1")
        if not 0 <= self.max_control_char_ratio <= 1.0:
            raise ValueError("max_control_char_ratio must be in [0, 1].")
        if self.context_chunk_radius < 0:
            raise ValueError("context_chunk_radius must be >= 0.")
        if self.max_chunk_line_multiplier <= 1.0:
            raise ValueError("max_chunk_line_multiplier must be > 1.0.")
        if self.max_error_chars < 1:
            raise ValueError("max_error_chars must be >= 1.")
        if self.max_symbol_targets < 1:
            raise ValueError("max_symbol_targets must be >= 1.")
        if self.max_symbol_dependents_per_target < 1:
            raise ValueError("max_symbol_dependents_per_target must be >= 1.")
        if self.symbol_dependent_snippet_radius < 0:
            raise ValueError("symbol_dependent_snippet_radius must be >= 0.")
        if self.max_symbol_context_chars < 1:
            raise ValueError("max_symbol_context_chars must be >= 1.")
        if self.lsp_timeout_seconds <= 0:
            raise ValueError("lsp_timeout_seconds must be > 0.")
        if self.max_lsp_context_chars < 1:
            raise ValueError("max_lsp_context_chars must be >= 1.")
        if self.response_cache_max_entries < 1:
            raise ValueError("response_cache_max_entries must be >= 1.")
        if any(client is self.llm_client for client in self.fallback_llm_clients):
            raise ValueError("fallback_llm_clients must not include the primary llm_client.")

    def apply(self, context: FailureContext) -> FixOutcome:
        """Apply an LLM-proposed fix to the primary error-referenced file."""
        workspace_root = Path(context.workspace).resolve()
        if (
            self.allowed_failures is not None
            and context.failure_type not in self.allowed_failures
        ):
            return FixOutcome(
                applied=False,
                note=(
                    f"Skipped because failure type {context.failure_type.value} is not "
                    "allowed for this strategy."
                ),
            )

        detected_references = self._extract_file_references(context.command_result.stderr)
        detected_paths = [reference.path for reference in detected_references]
        resolved_references = self._resolve_context_file_references(
            workspace_root=workspace_root,
            detected_references=detected_references,
            limit=self.max_context_files,
        )
        if not resolved_references:
            if detected_paths:
                return FixOutcome(
                    applied=False,
                    note="Detected paths were outside workspace or missing.",
                )
            return FixOutcome(
                applied=False,
                note="No candidate source files found in stderr.",
            )

        context_files = [file_path for file_path, _ in resolved_references]
        target_file = context_files[0]
        line_hints = {
            file_path: line_number
            for file_path, line_number in resolved_references
            if line_number is not None
        }
        try:
            current_code = target_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError) as exc:
            logger.warning("Skipping unreadable target file %s: %s", target_file, exc)
            return FixOutcome(
                applied=False,
                note=(
                    f"Unable to read target file "
                    f"{target_file.relative_to(workspace_root)} due to permission or I/O error."
                ),
            )

        additional_context_files = context_files[1:]
        context_file_contents = self._read_context_files(additional_context_files)

        target_line_hint = line_hints.get(target_file)
        target_window: tuple[int, int] | None = None
        if target_line_hint is not None:
            start_line, end_line, _, _ = self._compute_line_window(
                file_text=current_code,
                line_number=target_line_hint,
                radius=self.context_chunk_radius,
            )
            target_window = (start_line, end_line)
            prompt_target_code = current_code
        else:
            prompt_target_code = self._truncate_for_prompt(current_code)

        symbol_context_block = self._build_symbol_context_block(
            workspace_root=workspace_root,
            target_file=target_file,
            target_file_content=current_code,
            target_line_hint=target_line_hint,
        )
        lsp_context_block = self._build_lsp_context_block(
            workspace_root=workspace_root,
            target_file=target_file,
            target_line_hint=target_line_hint,
        )

        prompt = self._build_prompt(
            context=context,
            workspace_root=workspace_root,
            target_file=target_file,
            target_file_content=prompt_target_code,
            additional_context_file_contents=context_file_contents,
            context_line_hints=line_hints,
            chunk_radius=self.context_chunk_radius,
            symbol_context_block=symbol_context_block,
            lsp_context_block=lsp_context_block,
        )
        try:
            llm_output = self._generate_fix_with_fallback(prompt)
        except LLMClientError as exc:
            logger.warning("LLM strategy failed: strategy=%s error=%s", self.name, exc)
            return FixOutcome(applied=False, note=f"LLM error: {exc}")

        if not isinstance(llm_output, str):
            return FixOutcome(
                applied=False,
                note="LLM returned non-text output; refusing to overwrite file.",
            )

        llm_suggestion = self._extract_suggested_code(llm_output)
        if target_window is not None:
            chunk_error = self._validate_chunk_replacement(
                replacement_text=llm_suggestion,
                target_window=target_window,
                target_file=target_file,
                workspace_root=workspace_root,
            )
            if chunk_error is not None:
                return FixOutcome(applied=False, note=chunk_error)
            suggested_code = self._replace_line_window(
                file_text=current_code,
                start_line=target_window[0],
                end_line=target_window[1],
                replacement_text=llm_suggestion,
            )
        else:
            suggested_code = llm_suggestion
        safety_error = self._validate_suggested_code(
            current_code=current_code,
            suggested_code=suggested_code,
            target_file=target_file,
            workspace_root=workspace_root,
        )
        if safety_error is not None:
            return FixOutcome(applied=False, note=safety_error)

        if suggested_code == current_code:
            return FixOutcome(
                applied=False,
                note=f"LLM returned no changes for {target_file.relative_to(workspace_root)}.",
            )

        diff_summary = _build_diff_summary(
            workspace_root=workspace_root,
            file_path=target_file,
            before_text=current_code,
            after_text=suggested_code,
        )
        target_file.write_text(suggested_code, encoding="utf-8")
        logger.info(
            "Applied LLM strategy: strategy=%s file=%s",
            self.name,
            target_file,
        )
        return FixOutcome(
            applied=True,
            note=f"Applied LLM-generated fix to {target_file.relative_to(workspace_root)}.",
            changed_files=(target_file,),
            diff_summary=diff_summary,
            rollback_entries=(
                FileRollback(
                    path=target_file,
                    existed_before=True,
                    content=current_code,
                ),
            ),
        )

    def _validate_suggested_code(
        self,
        current_code: str,
        suggested_code: str,
        target_file: Path,
        workspace_root: Path,
    ) -> str | None:
        relative_path = target_file.relative_to(workspace_root)

        if not suggested_code:
            return (
                f"Safety check blocked overwrite for {relative_path}: "
                "LLM output was empty."
            )
        if len(suggested_code) > self.max_output_chars:
            return (
                f"Safety check blocked overwrite for {relative_path}: "
                f"output size {len(suggested_code)} exceeds max_output_chars "
                f"{self.max_output_chars}."
            )
        if "\x00" in suggested_code:
            return (
                f"Safety check blocked overwrite for {relative_path}: "
                "LLM output contains NUL bytes (binary-like content)."
            )

        disallowed_control_chars = sum(
            1
            for char in suggested_code
            if ord(char) < 32 and char not in ("\n", "\r", "\t")
        )
        if suggested_code and (
            disallowed_control_chars / len(suggested_code)
        ) > self.max_control_char_ratio:
            return (
                f"Safety check blocked overwrite for {relative_path}: "
                "LLM output appears non-text due to excessive control characters."
            )

        original_size = len(current_code)
        new_size = len(suggested_code)
        if original_size > 0 and new_size > original_size * self.max_growth_factor:
            return (
                f"Safety check blocked overwrite for {relative_path}: "
                f"output grew from {original_size} to {new_size} chars "
                f"(max factor {self.max_growth_factor}x)."
            )

        if (
            original_size >= self.min_original_chars_for_retention_check
            and new_size < original_size * self.min_retention_ratio
        ):
            return (
                f"Safety check blocked overwrite for {relative_path}: "
                f"output shrank from {original_size} to {new_size} chars "
                f"(min retention {self.min_retention_ratio:.2f})."
            )

        return None

    def _validate_chunk_replacement(
        self,
        replacement_text: str,
        target_window: tuple[int, int],
        target_file: Path,
        workspace_root: Path,
    ) -> str | None:
        expected_lines = max(1, target_window[1] - target_window[0] + 1)
        replacement_lines = max(1, len(replacement_text.splitlines()))
        max_allowed_lines = max(
            expected_lines + 10,
            int(expected_lines * self.max_chunk_line_multiplier),
        )
        if replacement_lines > max_allowed_lines:
            relative_path = target_file.relative_to(workspace_root)
            return (
                f"Safety check blocked chunk overwrite for {relative_path}: "
                f"expected a snippet near {expected_lines} lines but received "
                f"{replacement_lines} lines."
            )
        return None

    @staticmethod
    def _extract_candidate_paths(stderr: str) -> list[str]:
        return [reference.path for reference in LLMStrategy._extract_file_references(stderr)]

    @staticmethod
    def _extract_file_references(stderr: str) -> list[_ErrorFileReference]:
        seen_indexes: dict[str, int] = {}
        ordered: list[_ErrorFileReference] = []
        for match in _ERROR_FILE_PATTERN.finditer(stderr):
            raw_path = match.group("path").strip("()[]{}<>'\"`,")
            raw_line = match.group("line")
            line_number = int(raw_line) if raw_line is not None else None
            existing_index = seen_indexes.get(raw_path)
            if existing_index is not None:
                existing = ordered[existing_index]
                if existing.line_number is None and line_number is not None:
                    ordered[existing_index] = _ErrorFileReference(
                        path=raw_path,
                        line_number=line_number,
                    )
                continue

            seen_indexes[raw_path] = len(ordered)
            ordered.append(_ErrorFileReference(path=raw_path, line_number=line_number))
        return ordered

    @staticmethod
    def _resolve_candidate_path(workspace_root: Path, raw_path: str) -> Path | None:
        normalized = raw_path.replace("\\", "/")
        candidate = Path(normalized)
        if not candidate.is_absolute():
            candidate = workspace_root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            logger.warning("Failed to resolve candidate path: %s", raw_path)
            return None
        if not is_within_workspace(workspace_root, resolved):
            logger.warning(
                "Rejected LLM candidate path outside workspace: path=%s workspace=%s",
                raw_path,
                workspace_root,
            )
            return None
        try:
            exists = resolved.exists()
            is_file = resolved.is_file()
        except OSError:
            logger.warning("Failed to inspect candidate path: %s", resolved)
            return None
        if not exists or not is_file:
            return None
        return resolved

    @staticmethod
    def _resolve_context_files(
        workspace_root: Path,
        detected_paths: list[str],
        limit: int,
    ) -> list[Path]:
        references = [_ErrorFileReference(path=path) for path in detected_paths]
        resolved_references = LLMStrategy._resolve_context_file_references(
            workspace_root=workspace_root,
            detected_references=references,
            limit=limit,
        )
        return [file_path for file_path, _ in resolved_references]

    @staticmethod
    def _resolve_context_file_references(
        workspace_root: Path,
        detected_references: list[_ErrorFileReference],
        limit: int,
    ) -> list[tuple[Path, int | None]]:
        resolved_references: list[tuple[Path, int | None]] = []
        seen: set[Path] = set()
        for reference in detected_references:
            candidate = LLMStrategy._resolve_candidate_path(workspace_root, reference.path)
            if candidate is None:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            resolved_references.append((candidate, reference.line_number))
            if len(resolved_references) >= max(1, limit):
                break
        return resolved_references

    @staticmethod
    def _compute_line_window(
        file_text: str,
        line_number: int,
        radius: int,
    ) -> tuple[int, int, str, int]:
        lines = file_text.splitlines(keepends=True)
        if not lines:
            return 1, 1, "", 0

        total_lines = len(lines)
        clamped_line = max(1, min(line_number, total_lines))
        start_line = max(1, clamped_line - radius)
        end_line = min(total_lines, clamped_line + radius)
        snippet = "".join(lines[start_line - 1 : end_line])
        return start_line, end_line, snippet, total_lines

    @staticmethod
    def _replace_line_window(
        file_text: str,
        start_line: int,
        end_line: int,
        replacement_text: str,
    ) -> str:
        lines = file_text.splitlines(keepends=True)
        if not lines:
            return replacement_text

        safe_start = max(1, min(start_line, len(lines) + 1))
        safe_end = max(safe_start - 1, min(end_line, len(lines)))
        start_index = safe_start - 1
        end_index = safe_end
        replacement_lines = replacement_text.splitlines(keepends=True)
        merged_lines = lines[:start_index] + replacement_lines + lines[end_index:]
        return "".join(merged_lines)

    def _read_context_files(self, context_files: list[Path]) -> dict[Path, str]:
        context_map: dict[Path, str] = {}
        if not context_files:
            return context_map

        def read_one(file_path: Path) -> tuple[Path, str] | None:
            try:
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError, OSError) as exc:
                logger.warning("Skipping unreadable context file %s: %s", file_path, exc)
                return None
            return file_path, self._truncate_for_prompt(text)

        max_workers = min(4, len(context_files))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for result in executor.map(read_one, context_files):
                if result is None:
                    continue
                file_path, text = result
                context_map[file_path] = text
        return context_map

    def _truncate_for_prompt(self, file_text: str) -> str:
        if len(file_text) <= self.max_file_chars:
            return file_text
        truncated = file_text[: self.max_file_chars]
        return (
            f"{truncated}\n\n"
            f"# [TRUNCATED: original file exceeded {self.max_file_chars} characters]"
        )

    def _build_symbol_context_block(
        self,
        *,
        workspace_root: Path,
        target_file: Path,
        target_file_content: str,
        target_line_hint: int | None,
    ) -> str:
        if not self.enable_symbol_context:
            return "None"
        if target_file.suffix.lower() != ".py":
            return "None"

        symbol_spans = self._extract_python_definition_spans(target_file_content)
        if not symbol_spans:
            return "None"
        focus_spans = self._select_focus_symbol_spans(
            symbol_spans=symbol_spans,
            target_line_hint=target_line_hint,
            limit=self.max_symbol_targets,
        )
        if not focus_spans:
            return "None"

        graph = SymbolGraph()
        try:
            graph.build_graph(workspace_root)
        except Exception as exc:  # pragma: no cover - defensive guardrail
            logger.debug("Symbol graph build failed for LLMStrategy prompt context: %s", exc)
            return "None"

        sections: list[str] = []
        for span in focus_spans:
            sections.append(
                f"Target Definition: {span.name} (lines {span.start_line}-{span.end_line})\n"
                f"--- Definition Source ({target_file.relative_to(workspace_root)}) ---\n"
                f"{span.source}"
            )
            dependents = graph.get_dependents(target_file, span.name)
            for dependent in dependents[: self.max_symbol_dependents_per_target]:
                dependent_snippet = self._read_dependent_symbol_snippet(
                    workspace_root=workspace_root,
                    dependent_file=dependent,
                    symbol_name=span.name,
                )
                if dependent_snippet is None:
                    continue
                sections.append(
                    f"Immediate Dependent: {dependent.relative_to(workspace_root)}\n"
                    f"--- Dependent Snippet ---\n"
                    f"{dependent_snippet}"
                )

        if not sections:
            return "None"
        block = "\n\n".join(sections)
        if len(block) > self.max_symbol_context_chars:
            block = (
                f"{block[: self.max_symbol_context_chars]}\n\n"
                f"[TRUNCATED: symbol-aware context exceeded {self.max_symbol_context_chars} characters]"
            )
        return block

    @staticmethod
    def _extract_python_definition_spans(file_text: str) -> list[_PythonDefinitionSpan]:
        try:
            tree = ast.parse(file_text)
        except (SyntaxError, ValueError):
            return []

        lines = file_text.splitlines(keepends=True)
        spans: list[_PythonDefinitionSpan] = []
        seen: set[tuple[str, int, int]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start_line = getattr(node, "lineno", None)
            end_line = getattr(node, "end_lineno", None)
            if not isinstance(start_line, int) or not isinstance(end_line, int):
                continue
            if start_line < 1 or end_line < start_line:
                continue
            key = (node.name, start_line, end_line)
            if key in seen:
                continue
            seen.add(key)
            source = "".join(lines[start_line - 1 : end_line])
            spans.append(
                _PythonDefinitionSpan(
                    name=node.name,
                    start_line=start_line,
                    end_line=end_line,
                    source=source,
                )
            )
        spans.sort(key=lambda span: (span.start_line, span.end_line))
        return spans

    @staticmethod
    def _select_focus_symbol_spans(
        *,
        symbol_spans: list[_PythonDefinitionSpan],
        target_line_hint: int | None,
        limit: int,
    ) -> list[_PythonDefinitionSpan]:
        if not symbol_spans:
            return []

        if target_line_hint is None:
            return symbol_spans[:limit]

        matching = [
            span
            for span in symbol_spans
            if span.start_line <= target_line_hint <= span.end_line
        ]
        if matching:
            matching.sort(key=lambda span: ((span.end_line - span.start_line), span.start_line))
            deduped: list[_PythonDefinitionSpan] = []
            seen_names: set[str] = set()
            for span in matching:
                if span.name in seen_names:
                    continue
                seen_names.add(span.name)
                deduped.append(span)
                if len(deduped) >= limit:
                    break
            if deduped:
                return deduped

        return symbol_spans[:limit]

    def _read_dependent_symbol_snippet(
        self,
        *,
        workspace_root: Path,
        dependent_file: Path,
        symbol_name: str,
    ) -> str | None:
        if not is_within_workspace(workspace_root, dependent_file):
            return None
        if not dependent_file.exists() or not dependent_file.is_file():
            return None

        try:
            dependent_text = dependent_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            return None

        line_hint = self._find_symbol_line_hint(dependent_text, symbol_name)
        if line_hint is None:
            return self._truncate_for_prompt(dependent_text)

        start_line, end_line, snippet, total_lines = self._compute_line_window(
            file_text=dependent_text,
            line_number=line_hint,
            radius=self.symbol_dependent_snippet_radius,
        )
        return (
            f"Focused around symbol '{symbol_name}' "
            f"(line {line_hint}, snippet {start_line}-{end_line} of {total_lines})\n"
            f"{snippet}"
        )

    @staticmethod
    def _find_symbol_line_hint(file_text: str, symbol_name: str) -> int | None:
        if not symbol_name:
            return None
        pattern = re.compile(rf"\b{re.escape(symbol_name)}\b")
        for index, line in enumerate(file_text.splitlines(), start=1):
            if pattern.search(line):
                return index
        return None

    def _build_prompt(
        self,
        context: FailureContext,
        workspace_root: Path,
        target_file: Path,
        target_file_content: str,
        additional_context_file_contents: dict[Path, str],
        context_line_hints: dict[Path, int],
        chunk_radius: int,
        symbol_context_block: str,
        lsp_context_block: str,
    ) -> str:
        relative_file = target_file.relative_to(workspace_root)
        target_line_hint = context_line_hints.get(target_file)
        if target_line_hint is not None:
            target_start, target_end, target_snippet, target_total = LLMStrategy._compute_line_window(
                file_text=target_file_content,
                line_number=target_line_hint,
                radius=chunk_radius,
            )
            response_instruction = (
                "Return ONLY the corrected code excerpt for the target snippet "
                f"(lines {target_start}-{target_end} of {target_total})."
            )
            target_context_note = (
                f"Target snippet lines: {target_start}-{target_end} of {target_total}\n"
            )
            target_context_code = target_snippet
        else:
            response_instruction = "Return ONLY the full corrected file content for the target file."
            target_context_note = ""
            target_context_code = target_file_content

        additional_sections: list[str] = []
        for file_path, content in additional_context_file_contents.items():
            relative_path = file_path.relative_to(workspace_root)
            line_hint = context_line_hints.get(file_path)
            if line_hint is None:
                additional_sections.append(
                    f"Additional Context: {relative_path}\n"
                    f"--- Code for {relative_path} ---\n"
                    f"{content}\n"
                )
                continue

            start_line, end_line, snippet, total_lines = LLMStrategy._compute_line_window(
                file_text=content,
                line_number=line_hint,
                radius=chunk_radius,
            )
            additional_sections.append(
                f"Additional Context: {relative_path}\n"
                f"Error line hint: {line_hint}\n"
                f"Provided snippet lines: {start_line}-{end_line} of {total_lines}\n"
                f"--- Code for {relative_path} (excerpt) ---\n"
                f"{snippet}\n"
            )
        additional_block = "\n".join(additional_sections).strip()
        if not additional_block:
            additional_block = "None"
        error_output = context.command_result.combined_output
        if len(error_output) > self.max_error_chars:
            error_output = (
                f"{error_output[: self.max_error_chars]}\n\n"
                f"[TRUNCATED: error output exceeded {self.max_error_chars} characters]"
            )

        return (
            "You are fixing a failing command in a local repository.\n"
            f"{response_instruction}\n"
            "Do not include markdown fences or commentary.\n\n"
            f"Failing command:\n{context.command_result.command}\n\n"
            f"Full error output:\n{error_output}\n\n"
            "Symbol-Aware Context:\n"
            f"{symbol_context_block}\n\n"
            "LSP-Injected Context:\n"
            f"{lsp_context_block}\n\n"
            f"Primary Target: {relative_file}\n"
            f"{target_context_note}"
            f"--- Code for {relative_file} ---\n"
            f"{target_context_code}\n\n"
            "Additional Context Files:\n"
            f"{additional_block}"
        )

    def _build_lsp_context_block(
        self,
        *,
        workspace_root: Path,
        target_file: Path,
        target_line_hint: int | None,
    ) -> str:
        if not self.enable_lsp_context:
            return "None"
        if target_file.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
            return "None"
        pyright_bin = shutil.which("pyright")
        if pyright_bin is None:
            return "None"
        try:
            completed = subprocess.run(
                [pyright_bin, str(target_file), "--outputjson"],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.lsp_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "None"

        output = completed.stdout.strip()
        if not output:
            return "None"
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return "None"
        diagnostics = payload.get("generalDiagnostics")
        if not isinstance(diagnostics, list):
            return "None"

        relative_target = target_file.relative_to(workspace_root).as_posix()
        lines: list[str] = []
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            file_value = str(diagnostic.get("file", "")).replace("\\", "/")
            if not file_value.endswith(relative_target):
                continue
            start = diagnostic.get("range", {}).get("start", {}) if isinstance(diagnostic.get("range"), dict) else {}
            line_number = int(start.get("line", 0)) + 1 if isinstance(start, dict) else 0
            if target_line_hint is not None and line_number:
                if abs(line_number - target_line_hint) > 50:
                    continue
            severity = str(diagnostic.get("severity", "warning")).lower()
            message = str(diagnostic.get("message", "")).strip()
            if not message:
                continue
            lines.append(f"- {severity} line {line_number}: {message}")
            if len(lines) >= 8:
                break

        if not lines:
            return "None"
        block = "\n".join(lines)
        if len(block) > self.max_lsp_context_chars:
            block = (
                f"{block[: self.max_lsp_context_chars]}\n"
                f"[TRUNCATED: LSP context exceeded {self.max_lsp_context_chars} characters]"
            )
        return block

    @staticmethod
    def _extract_suggested_code(raw_output: str) -> str:
        fence_match = _FENCED_CODE_PATTERN.search(raw_output)
        if fence_match:
            return fence_match.group("code")
        return raw_output

    def _generate_fix_with_fallback(self, prompt: str) -> str:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        cache_key = f"{self.cache_namespace}:{prompt_hash}"
        if self.enable_response_cache:
            cached_response = _prompt_cache_get(cache_key)
            if cached_response is not None:
                return cached_response

        clients: tuple[LLMClient, ...] = (self.llm_client, *self.fallback_llm_clients)
        if len(clients) == 1:
            response = self._invoke_client(self.llm_client, prompt)
            if self.enable_response_cache and isinstance(response, str):
                _prompt_cache_set(cache_key, response, self.response_cache_max_entries)
            return response

        failures: list[Exception] = []
        executor = ThreadPoolExecutor(max_workers=len(clients))
        wait_for_remaining = True
        try:
            futures = {
                executor.submit(self._invoke_client, client, prompt): index
                for index, client in enumerate(clients)
            }
            for future in as_completed(futures):
                try:
                    output = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures.append(exc)
                    continue

                for pending in futures:
                    if pending is not future:
                        pending.cancel()
                wait_for_remaining = False
                executor.shutdown(wait=False, cancel_futures=True)
                if self.enable_response_cache and isinstance(output, str):
                    _prompt_cache_set(cache_key, output, self.response_cache_max_entries)
                return output
        finally:
            if wait_for_remaining:
                executor.shutdown(wait=True, cancel_futures=True)

        llm_failures = [
            failure
            for failure in failures
            if isinstance(failure, LLMClientError)
        ]
        if llm_failures:
            first_error = llm_failures[0]
            details = " | ".join(str(error) for error in llm_failures[:3])
            raise LLMClientError(f"All configured LLM clients failed: {details}") from first_error

        if failures:
            first_error = failures[0]
            details = " | ".join(str(error) for error in failures[:3])
            raise LLMClientError(
                f"All configured LLM clients failed with unexpected errors: {details}"
            ) from first_error

        raise LLMClientError("All configured LLM clients failed without detailed error output.")

    def _invoke_client(self, client: LLMClient, prompt: str) -> str:
        if self.enable_streaming_speculative_parsing:
            stream_method = getattr(client, "stream_fix", None)
            if callable(stream_method):
                stream_output = stream_method(prompt)
                if isinstance(stream_output, str):
                    return stream_output.strip()
                return parse_streamed_response(stream_output)
        return client.generate_fix(prompt)


@dataclass(frozen=True)
class RegexReplaceStrategy:
    """Apply a targeted regex replacement to one repository file."""

    name: str
    target_file: str
    pattern: str
    replacement: str
    count: int = 0
    allowed_failures: set[FailureType] | None = None

    def __post_init__(self) -> None:
        _validate_regex_pattern(self.pattern, self.name)

    def apply(self, context: FailureContext) -> FixOutcome:
        """Apply regex replacement to one target file within workspace scope."""
        workspace_root = Path(context.workspace).resolve()
        if (
            self.allowed_failures is not None
            and context.failure_type not in self.allowed_failures
        ):
            return FixOutcome(
                applied=False,
                note=(
                    f"Skipped because failure type {context.failure_type.value} is not "
                    "allowed for this strategy."
                ),
            )

        file_path = (workspace_root / self.target_file).resolve()
        if not is_within_workspace(workspace_root, file_path):
            logger.warning(
                "Blocked out-of-repo edit attempt: strategy=%s target=%s workspace=%s",
                self.name,
                self.target_file,
                workspace_root,
            )
            return FixOutcome(
                applied=False,
                note=f"Blocked: target file {self.target_file} is outside workspace.",
            )

        if not file_path.exists():
            return FixOutcome(
                applied=False,
                note=f"Target file {self.target_file} does not exist.",
            )

        original_text = file_path.read_text(encoding="utf-8")
        updated_text, replacements = re.subn(
            self.pattern,
            self.replacement,
            original_text,
            count=self.count,
        )
        if replacements == 0:
            return FixOutcome(
                applied=False,
                note="Pattern not found; no replacement made.",
            )

        file_path.write_text(updated_text, encoding="utf-8")
        diff_summary = _build_diff_summary(
            workspace_root=workspace_root,
            file_path=file_path,
            before_text=original_text,
            after_text=updated_text,
            max_hunks=2,
        )
        logger.info(
            "Applied regex strategy: strategy=%s file=%s replacements=%s",
            self.name,
            file_path,
            replacements,
        )
        return FixOutcome(
            applied=True,
            note=f"Applied {replacements} replacement(s) in {self.target_file}.",
            changed_files=(file_path,),
            diff_summary=diff_summary,
            rollback_entries=(
                FileRollback(
                    path=file_path,
                    existed_before=True,
                    content=original_text,
                ),
            ),
        )


@dataclass(frozen=True)
class RepoRegexReplaceStrategy:
    """Apply a regex replacement across matching files within repository scope."""

    name: str
    pattern: str
    replacement: str
    include_globs: tuple[str, ...] = ("**/*.py",)
    exclude_dirs: tuple[str, ...] = (
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
    )
    count: int = 0
    max_files: int = 500
    allowed_failures: set[FailureType] | None = None

    def __post_init__(self) -> None:
        _validate_regex_pattern(self.pattern, self.name)

    def apply(self, context: FailureContext) -> FixOutcome:
        """Apply regex replacement across repository files that match include globs."""
        workspace_root = Path(context.workspace).resolve()
        if (
            self.allowed_failures is not None
            and context.failure_type not in self.allowed_failures
        ):
            return FixOutcome(
                applied=False,
                note=(
                    f"Skipped because failure type {context.failure_type.value} is not "
                    "allowed for this strategy."
                ),
            )

        changed_files: list[Path] = []
        total_replacements = 0
        scanned_files = 0
        seen_files: set[Path] = set()
        per_file_summary: list[str] = []
        rollback_entries: list[FileRollback] = []
        io_skip_count = 0

        for glob_pattern in self.include_globs:
            for candidate in workspace_root.glob(glob_pattern):
                if scanned_files >= self.max_files:
                    break
                try:
                    if not candidate.is_file():
                        continue
                    resolved_candidate = candidate.resolve()
                except OSError as exc:
                    logger.warning("Skipping unreadable repository candidate %s: %s", candidate, exc)
                    io_skip_count += 1
                    continue

                if resolved_candidate in seen_files:
                    continue
                seen_files.add(resolved_candidate)

                if not is_within_workspace(workspace_root, resolved_candidate):
                    continue

                relative_parts = resolved_candidate.relative_to(workspace_root).parts
                if any(part in self.exclude_dirs for part in relative_parts):
                    continue

                scanned_files += 1
                try:
                    original_text = resolved_candidate.read_text(encoding="utf-8")
                except (UnicodeDecodeError, PermissionError, OSError) as exc:
                    logger.warning(
                        "Skipping unreadable repository file %s: %s",
                        resolved_candidate,
                        exc,
                    )
                    io_skip_count += 1
                    continue

                updated_text, replacements = re.subn(
                    self.pattern,
                    self.replacement,
                    original_text,
                    count=self.count,
                )
                if replacements == 0:
                    continue

                try:
                    resolved_candidate.write_text(updated_text, encoding="utf-8")
                except OSError as exc:
                    logger.warning(
                        "Skipping unwritable repository file %s: %s",
                        resolved_candidate,
                        exc,
                    )
                    io_skip_count += 1
                    continue
                logger.info(
                    "Repo regex changed file: strategy=%s file=%s replacements=%s",
                    self.name,
                    resolved_candidate,
                    replacements,
                )
                changed_files.append(resolved_candidate)
                total_replacements += replacements
                rollback_entries.append(
                    FileRollback(
                        path=resolved_candidate,
                        existed_before=True,
                        content=original_text,
                    )
                )
                summary = _build_diff_summary(
                    workspace_root=workspace_root,
                    file_path=resolved_candidate,
                    before_text=original_text,
                    after_text=updated_text,
                    max_hunks=1,
                )
                if summary:
                    per_file_summary.append(summary[0])

            if scanned_files >= self.max_files:
                break

        if total_replacements == 0:
            skip_note = ""
            if io_skip_count:
                skip_note = f" Skipped {io_skip_count} file(s) due to I/O or permission errors."
            return FixOutcome(
                applied=False,
                note=f"No matching patterns found in repository scope.{skip_note}",
            )

        logger.info(
            "Applied repo-wide regex strategy: strategy=%s files=%s replacements=%s scanned=%s",
            self.name,
            len(changed_files),
            total_replacements,
            scanned_files,
        )
        diff_summary = [
            (
                f"Modified {len(changed_files)} file(s) in repository scope; "
                f"total replacements={total_replacements}."
            )
        ]
        if io_skip_count:
            diff_summary.append(
                f"Skipped {io_skip_count} file(s) due to I/O or permission errors."
            )
        diff_summary.extend(per_file_summary[:10])
        skip_note = ""
        if io_skip_count:
            skip_note = f" Skipped {io_skip_count} file(s) due to I/O or permission errors."
        return FixOutcome(
            applied=True,
            note=(
                "Applied "
                f"{total_replacements} replacement(s) across {len(changed_files)} file(s)."
                f"{skip_note}"
            ),
            changed_files=tuple(changed_files),
            diff_summary=tuple(diff_summary),
            rollback_entries=tuple(rollback_entries),
        )
