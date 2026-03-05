"""ModelKwargGuard — deterministic ORM constructor mismatch detection and auto-fix.

Extracts SQLAlchemy/ORM model field definitions from generated files, then:
  - Renames known field aliases (e.g. stock_quantity → stock) in non-model files.
  - Logs remaining unknown kwargs as structured issues for the LLM micro-heal step.

Zero LLM cost. Runs as part of LiveGuard Tier 2 (post-Stage 3, pre-deps/tests).
Stage 6 LLM healing handles anything that can't be fixed deterministically.
"""
import ast
import re
from pathlib import Path

# ── Field alias map ───────────────────────────────────────────────────────────
# Maps incorrect kwarg names → canonical model field names.
# A rename only fires when the canonical name actually exists in the target model.
_FIELD_ALIASES: dict[str, str] = {
    "stock_quantity": "stock",
    "quantity_in_stock": "stock",
    "qty": "quantity",
    "desc": "description",
    "short_description": "description",
    "image": "image_url",
    "photo": "image_url",
    "img": "image_url",
    "user_name": "username",
    "category_name": "name",
    "product_name": "name",
    "item_name": "name",
}

# SQLAlchemy / ORM column/field call names (class-level assignments)
_ORM_FIELD_CALLS = frozenset({
    "Column", "mapped_column", "relationship", "backref",
    "field",        # Django / dataclasses
    "CharField", "IntegerField", "FloatField", "BooleanField",
    "TextField", "DateField", "DateTimeField", "ForeignKey",
})

# Heuristic: files that define ORM models (skip alias renaming inside these)
_MODEL_FILE_MARKERS = re.compile(
    r"\b(Column|mapped_column|DeclarativeBase|declarative_base|Base\.metadata)\b"
)

# Seed/fixture file indicators (for SeedContractGuard scope)
_SEED_FILE_MARKERS = re.compile(
    r"\b(db\.add|session\.add|bulk_insert|insert_many|\.commit\(\)|seed|fixture)\b",
    re.IGNORECASE,
)


# ── Model field extraction ────────────────────────────────────────────────────

def extract_model_fields(generated_files) -> dict[str, set[str]]:
    """Parse ORM model classes from generated files.

    Returns {ClassName -> {field_name, ...}} for every class that has at least
    one Column / mapped_column / relationship assignment at class body level.
    """
    model_fields: dict[str, set[str]] = {}

    for f in generated_files:
        if not f.file_path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            fields: set[str] = set()
            for stmt in node.body:
                # class-level assignment:  id = Column(Integer, primary_key=True)
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and not target.id.startswith("_"):
                            if _is_orm_field_call(stmt.value):
                                fields.add(target.id)

                # annotated assignment:  id: Mapped[int] = mapped_column(...)
                #                        id: int  (no value — bare annotation)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    name = stmt.target.id
                    if not name.startswith("_"):
                        if stmt.value is None or _is_orm_field_call(stmt.value):
                            fields.add(name)

            if fields:
                model_fields[node.name] = fields

    return model_fields


def _is_orm_field_call(node: ast.expr) -> bool:
    """True if the expression is a call to a known ORM field constructor."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = (
        func.id if isinstance(func, ast.Name)
        else func.attr if isinstance(func, ast.Attribute)
        else None
    )
    return name in _ORM_FIELD_CALLS


# ── Issue scanning ────────────────────────────────────────────────────────────

def scan_issues(
    generated_files,
    model_fields: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Find constructor calls that pass kwargs not defined on the model.

    Returns {file_path -> [issue_message, ...]}. Skips **unpacking kwargs.
    Issues are phrased to be directly usable as healer prompts.
    """
    issues: dict[str, list[str]] = {}

    for f in generated_files:
        if not f.file_path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue

        file_issues: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            class_name = _call_class_name(node)
            if class_name not in model_fields:
                continue

            allowed = model_fields[class_name]
            for kw in node.keywords:
                if kw.arg is None:          # **unpacking — skip
                    continue
                if kw.arg in allowed:
                    continue

                alias_target = _FIELD_ALIASES.get(kw.arg)
                if alias_target and alias_target in allowed:
                    file_issues.append(
                        f"line {node.lineno}: {class_name}({kw.arg}=...) "
                        f"should be '{alias_target}=' (alias rename needed)"
                    )
                else:
                    file_issues.append(
                        f"line {node.lineno}: {class_name}({kw.arg}=...) "
                        f"is not a field on {class_name}; valid fields: "
                        f"{sorted(allowed)}"
                    )

        if file_issues:
            issues[f.file_path] = file_issues

    return issues


def _call_class_name(call_node: ast.Call) -> str | None:
    func = call_node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ── Deterministic auto-fix ────────────────────────────────────────────────────

def auto_fix_aliases(
    generated_files,
    workspace: str,
    model_fields: dict[str, set[str]],
) -> list[str]:
    """Rename known field aliases in non-model files (regex, zero LLM cost).

    A rename only applies when:
      - The wrong name appears in _FIELD_ALIASES
      - The canonical target name exists in at least one parsed model
      - The file is not itself a model definition file (to avoid corrupting
        Column(...) definitions whose field name happens to match an alias)

    Returns list of relative file paths that were modified on disk.
    """
    if not model_fields:
        return []

    # Union of all canonical field names across all models
    all_canonical: set[str] = set().union(*model_fields.values())

    # Only activate aliases whose target actually exists in some model
    active_aliases: dict[str, str] = {
        wrong: right
        for wrong, right in _FIELD_ALIASES.items()
        if right in all_canonical
    }
    if not active_aliases:
        return []

    fixed: list[str] = []

    for f in generated_files:
        if not f.file_path.endswith(".py"):
            continue

        full_path = Path(workspace) / f.file_path
        if not full_path.exists():
            continue

        content = full_path.read_text(encoding="utf-8")

        # Skip model definition files — renaming Column field names there
        # would corrupt the schema that everything else depends on.
        if _MODEL_FILE_MARKERS.search(content):
            continue

        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        lines = content.splitlines(keepends=True)
        starts: list[int] = []
        offset = 0
        for line in lines:
            starts.append(offset)
            offset += len(line)

        def _abs(line_no: int, col: int) -> int:
            # AST line numbers are 1-based.
            return starts[line_no - 1] + col

        # Only rewrite inside model constructor call spans, never globally.
        spans: list[tuple[int, int, dict[str, str]]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            class_name = _call_class_name(node)
            if not class_name or class_name not in model_fields:
                continue
            allowed = model_fields[class_name]
            alias_for_call: dict[str, str] = {}
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                if kw.arg not in active_aliases:
                    continue
                target = active_aliases[kw.arg]
                if target in allowed:
                    alias_for_call[kw.arg] = target
            if not alias_for_call:
                continue
            if not all(hasattr(node, attr) for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset")):
                continue
            start = _abs(node.lineno, node.col_offset)
            end = _abs(node.end_lineno, node.end_col_offset)
            spans.append((start, end, alias_for_call))

        if not spans:
            continue

        new_content = content
        # Apply from right to left to avoid index shifts.
        for start, end, alias_map in sorted(spans, key=lambda s: s[0], reverse=True):
            segment = new_content[start:end]
            for wrong, right in alias_map.items():
                pattern = rf"\b{re.escape(wrong)}\s*=(?!=)"
                segment = re.sub(pattern, f"{right}=", segment)
            new_content = new_content[:start] + segment + new_content[end:]

        if new_content != content:
            full_path.write_text(new_content, encoding="utf-8")
            fixed.append(f.file_path)

    return fixed


# ── SchemaDriftGuard ──────────────────────────────────────────────────────────

# Matches names like CategoryCreate, CategoryUpdate, CategoryBase, CategorySchema, etc.
_SCHEMA_SUFFIX_RE = re.compile(
    r"^(.+?)(?:Create|Update|Response|Base|Schema|Read|Out|In|List)$"
)
# Pydantic base class names (common variants)
_PYDANTIC_BASES = frozenset({"BaseModel", "Schema"})
# Fields that exist on schemas but not ORM models — never flag these
_SCHEMA_ONLY_FIELDS = frozenset({
    "id", "created_at", "updated_at", "deleted_at",
    "model_config", "model_fields", "model_fields_set",
})


def scan_schema_drift_issues(
    generated_files,
    model_fields: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Detect Pydantic schema fields that don't exist on the corresponding ORM model.

    Matches schema classes by name suffix convention:
        CategoryCreate → Category, ProductUpdate → Product, etc.

    Only flags fields that:
    - Are annotated at class body level (not methods, not ClassVar, not inherited)
    - Have no default value OR have a required Field(...) — indicating they
      must be supplied by the caller
    - Are not in the schema-only allowlist (id, timestamps, model_config)

    Returns {file_path -> [issue_message, ...]}
    """
    if not model_fields:
        return {}

    issues: dict[str, list[str]] = {}

    for f in generated_files:
        if not f.file_path.endswith(".py"):
            continue
        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue

        file_issues: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            # Check if class inherits from a Pydantic base
            base_names = {
                b.id if isinstance(b, ast.Name)
                else b.attr if isinstance(b, ast.Attribute)
                else None
                for b in node.bases
            }
            if not (base_names & _PYDANTIC_BASES):
                continue

            # Try to match class name to an ORM model
            m = _SCHEMA_SUFFIX_RE.match(node.name)
            if not m:
                continue
            model_name = m.group(1)
            if model_name not in model_fields:
                continue

            orm_fields = model_fields[model_name]

            # Collect schema field names from annotated class-level statements
            schema_fields: set[str] = set()
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign):
                    continue
                if not isinstance(stmt.target, ast.Name):
                    continue
                name = stmt.target.id
                if name.startswith("_"):
                    continue
                schema_fields.add(name)

            # Flag schema fields not present in ORM model or allowlist
            unknown = schema_fields - orm_fields - _SCHEMA_ONLY_FIELDS
            if unknown:
                file_issues.append(
                    f"{node.name}: fields {sorted(unknown)} not found on ORM model "
                    f"'{model_name}' (available: {sorted(orm_fields)})"
                )

        if file_issues:
            issues[f.file_path] = file_issues

    return issues


# ── SeedContractGuard ─────────────────────────────────────────────────────────

def scan_seed_contract_issues(
    generated_files,
    model_fields: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Detect missing required fields in seed/fixture constructor calls.

    Currently checks:
    - Model has a ``slug`` field, call provides ``name`` but omits ``slug``.
      Slug is almost always derived from name and must be present for unique
      constraints; omitting it causes IntegrityError at runtime.

    Scoped to files that look like seeds/fixtures (contain db.add / session.add
    / commit patterns) to minimise false positives in service layer code.

    Returns {file_path -> [issue_message, ...]}.
    """
    models_with_slug = {cls for cls, fields in model_fields.items() if "slug" in fields}
    if not models_with_slug:
        return {}

    issues: dict[str, list[str]] = {}

    for f in generated_files:
        if not f.file_path.endswith(".py"):
            continue
        if not _SEED_FILE_MARKERS.search(f.content):
            continue  # not a seed/fixture file

        try:
            tree = ast.parse(f.content)
        except SyntaxError:
            continue

        file_issues: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            class_name = _call_class_name(node)
            if class_name not in models_with_slug:
                continue

            kwarg_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            if "name" in kwarg_names and "slug" not in kwarg_names:
                file_issues.append(
                    f"line {node.lineno}: {class_name}(name=...) omits 'slug', "
                    f"which is a required unique field on {class_name}. "
                    f"Derive it from name (e.g. slug=name.lower().replace(' ', '-'))."
                )

        if file_issues:
            issues[f.file_path] = file_issues

    return issues
