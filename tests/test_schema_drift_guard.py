"""Tests for SchemaDriftGuard — Pydantic schema vs ORM model field drift."""
from codegen_agent.model_kwarg_guard import extract_model_fields, scan_schema_drift_issues
from codegen_agent.models import GeneratedFile


def _gf(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(file_path=path, content=content, node_id="n", sha256="x")


# ── scan_schema_drift_issues ──────────────────────────────────────────────────

def test_flags_schema_field_not_on_orm_model():
    model = _gf("src/models.py",
        "class Category(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    slug = Column(String)\n"
    )
    schema = _gf("src/schemas.py",
        "from pydantic import BaseModel\n"
        "class CategoryCreate(BaseModel):\n"
        "    name: str\n"
        "    nonexistent_field: str\n"  # not on ORM model
    )
    fields = extract_model_fields([model, schema])
    issues = scan_schema_drift_issues([model, schema], fields)
    assert "src/schemas.py" in issues
    msgs = issues["src/schemas.py"]
    assert any("nonexistent_field" in m for m in msgs)


def test_no_issue_when_all_schema_fields_on_model():
    model = _gf("src/models.py",
        "class Product(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    price = Column(Float)\n"
    )
    schema = _gf("src/schemas.py",
        "from pydantic import BaseModel\n"
        "class ProductCreate(BaseModel):\n"
        "    name: str\n"
        "    price: float\n"
    )
    fields = extract_model_fields([model, schema])
    issues = scan_schema_drift_issues([model, schema], fields)
    assert "src/schemas.py" not in issues


def test_id_field_not_flagged_in_response_schema():
    """id is in the allowlist — ResponseSchema can expose it without ORM having it explicitly."""
    model = _gf("src/models.py",
        "class Tag(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
    )
    schema = _gf("src/schemas.py",
        "from pydantic import BaseModel\n"
        "class TagResponse(BaseModel):\n"
        "    id: int\n"
        "    name: str\n"
    )
    fields = extract_model_fields([model, schema])
    issues = scan_schema_drift_issues([model, schema], fields)
    assert "src/schemas.py" not in issues


def test_no_match_when_no_orm_model_with_same_prefix():
    schema = _gf("src/schemas.py",
        "from pydantic import BaseModel\n"
        "class FooCreate(BaseModel):\n"
        "    bar: str\n"
    )
    fields = extract_model_fields([schema])
    issues = scan_schema_drift_issues([schema], fields)
    # No ORM model named 'Foo' — guard must not flag this
    assert "src/schemas.py" not in issues


def test_non_pydantic_class_not_flagged():
    """Classes that don't inherit BaseModel should be ignored."""
    model = _gf("src/models.py",
        "class Item(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
    )
    other = _gf("src/other.py",
        "class ItemCreate:\n"  # no BaseModel inheritance
        "    ghost: str\n"
    )
    fields = extract_model_fields([model])
    issues = scan_schema_drift_issues([model, other], fields)
    assert "src/other.py" not in issues


def test_update_schema_variant_matched():
    model = _gf("src/models.py",
        "class Order(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    total = Column(Float)\n"
    )
    schema = _gf("src/schemas.py",
        "from pydantic import BaseModel\n"
        "class OrderUpdate(BaseModel):\n"
        "    total: float\n"
        "    ghost_field: str\n"
    )
    fields = extract_model_fields([model, schema])
    issues = scan_schema_drift_issues([model, schema], fields)
    assert "src/schemas.py" in issues
    assert any("ghost_field" in m for m in issues["src/schemas.py"])


def test_returns_empty_when_no_model_fields():
    schema = _gf("src/schemas.py",
        "from pydantic import BaseModel\n"
        "class FooCreate(BaseModel):\n"
        "    name: str\n"
    )
    issues = scan_schema_drift_issues([schema], {})
    assert issues == {}
