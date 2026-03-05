"""Tests for model_kwarg_guard.py — ORM constructor mismatch detection and fix.

Includes a regression test for the model/seed drift failure mode where a seed
file passes kwargs (e.g. description, stock_quantity) that don't exist on the
ORM model, causing runtime errors that bypass static analysis.
"""
from pathlib import Path

import pytest

from codegen_agent.model_kwarg_guard import (
    auto_fix_aliases,
    extract_model_fields,
    scan_issues,
)
from codegen_agent.models import GeneratedFile


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gf(file_path: str, content: str) -> GeneratedFile:
    return GeneratedFile(file_path=file_path, content=content, node_id="n", sha256="x")


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── extract_model_fields ──────────────────────────────────────────────────────

def test_extract_column_fields():
    content = (
        "from sqlalchemy import Column, Integer, String\n"
        "class Category(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(100))\n"
        "    slug = Column(String(100), unique=True)\n"
    )
    fields = extract_model_fields([_gf("src/models.py", content)])
    assert "Category" in fields
    assert {"id", "name", "slug"}.issubset(fields["Category"])


def test_extract_mapped_column_fields():
    content = (
        "from sqlalchemy.orm import mapped_column, Mapped\n"
        "class Product(Base):\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        "    name: Mapped[str] = mapped_column(String(200))\n"
        "    stock: Mapped[int] = mapped_column(default=0)\n"
        "    price: Mapped[float]\n"
    )
    fields = extract_model_fields([_gf("src/models.py", content)])
    assert "Product" in fields
    assert {"id", "name", "stock", "price"}.issubset(fields["Product"])


def test_extract_ignores_non_orm_classes():
    content = (
        "class Helper:\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
    )
    fields = extract_model_fields([_gf("src/utils.py", content)])
    assert "Helper" not in fields


def test_extract_ignores_private_fields():
    content = (
        "class MyModel(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    _cache = Column(String)  # private, not a user field\n"
    )
    fields = extract_model_fields([_gf("src/models.py", content)])
    assert "MyModel" in fields
    assert "_cache" not in fields["MyModel"]
    assert "id" in fields["MyModel"]


def test_extract_skips_syntax_errors():
    content = "class Broken(Base\n    id = Column(Integer)\n"
    fields = extract_model_fields([_gf("src/models.py", content)])
    assert fields == {}


# ── scan_issues ───────────────────────────────────────────────────────────────

def test_scan_finds_unknown_kwarg():
    model_content = (
        "class Category(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(100))\n"
        "    slug = Column(String(100))\n"
    )
    seed_content = (
        "from src.models import Category\n"
        "db.add(Category(name='Electronics', description='A category'))\n"
    )
    files = [_gf("src/models.py", model_content), _gf("src/seed.py", seed_content)]
    model_fields = extract_model_fields(files)
    issues = scan_issues(files, model_fields)

    assert "src/seed.py" in issues
    assert any("description" in msg for msg in issues["src/seed.py"])


def test_scan_flags_alias_as_informative_message():
    model_content = (
        "class Product(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    stock = Column(Integer)\n"
    )
    seed_content = "db.add(Product(name='X', stock_quantity=10))\n"
    files = [_gf("src/models.py", model_content), _gf("src/seed.py", seed_content)]
    model_fields = extract_model_fields(files)
    issues = scan_issues(files, model_fields)

    assert "src/seed.py" in issues
    msgs = issues["src/seed.py"]
    assert any("alias" in msg or "stock" in msg for msg in msgs)


def test_scan_no_issue_for_valid_kwargs():
    model_content = (
        "class Category(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    slug = Column(String)\n"
    )
    seed_content = "db.add(Category(name='Electronics', slug='electronics'))\n"
    files = [_gf("src/models.py", model_content), _gf("src/seed.py", seed_content)]
    model_fields = extract_model_fields(files)
    issues = scan_issues(files, model_fields)
    assert "src/seed.py" not in issues


def test_scan_skips_star_star_unpacking():
    model_content = (
        "class Item(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
    )
    seed_content = "db.add(Item(**data))\n"
    files = [_gf("src/models.py", model_content), _gf("src/seed.py", seed_content)]
    model_fields = extract_model_fields(files)
    issues = scan_issues(files, model_fields)
    assert "src/seed.py" not in issues


# ── auto_fix_aliases ──────────────────────────────────────────────────────────

def test_alias_rename_stock_quantity(tmp_path):
    """Regression: stock_quantity → stock when model has stock field."""
    _write(tmp_path, "src/models.py",
        "class Product(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    stock = Column(Integer, default=0)\n"
    )
    _write(tmp_path, "src/seed.py",
        "db.add(Product(name='Widget', stock_quantity=100))\n"
        "db.add(Product(name='Gadget', stock_quantity=50))\n"
    )
    model_content = (tmp_path / "src/models.py").read_text()
    seed_content = (tmp_path / "src/seed.py").read_text()
    files = [
        _gf("src/models.py", model_content),
        _gf("src/seed.py", seed_content),
    ]
    model_fields = extract_model_fields(files)
    fixed = auto_fix_aliases(files, str(tmp_path), model_fields)

    assert "src/seed.py" in fixed
    result = (tmp_path / "src/seed.py").read_text()
    assert "stock_quantity" not in result
    assert "stock=100" in result
    assert "stock=50" in result


def test_alias_rename_does_not_touch_model_file(tmp_path):
    """Model definition files must not be rewritten by alias normalization."""
    _write(tmp_path, "src/models.py",
        "class Order(Base):\n"
        "    qty = Column(Integer)\n"   # qty is a valid field here
        "    quantity = Column(Integer)\n"
    )
    model_content = (tmp_path / "src/models.py").read_text()
    files = [_gf("src/models.py", model_content)]
    model_fields = extract_model_fields(files)

    # Even if qty is in FIELD_ALIASES, the model file itself must not be touched
    fixed = auto_fix_aliases(files, str(tmp_path), model_fields)
    assert "src/models.py" not in fixed
    assert (tmp_path / "src/models.py").read_text() == model_content


def test_alias_only_fires_when_target_in_model(tmp_path):
    """stock_quantity → stock should NOT fire if model has no 'stock' field."""
    _write(tmp_path, "src/models.py",
        "class Product(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    inventory = Column(Integer)\n"  # 'stock' not present
    )
    _write(tmp_path, "src/seed.py",
        "db.add(Product(name='X', stock_quantity=10))\n"
    )
    model_content = (tmp_path / "src/models.py").read_text()
    seed_content = (tmp_path / "src/seed.py").read_text()
    files = [_gf("src/models.py", model_content), _gf("src/seed.py", seed_content)]
    model_fields = extract_model_fields(files)
    fixed = auto_fix_aliases(files, str(tmp_path), model_fields)

    assert "src/seed.py" not in fixed
    assert (tmp_path / "src/seed.py").read_text() == seed_content


def test_alias_rename_desc_to_description(tmp_path):
    _write(tmp_path, "src/models.py",
        "class Category(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    description = Column(Text)\n"
    )
    _write(tmp_path, "src/seed.py",
        "db.add(Category(name='Tech', desc='Technology'))\n"
    )
    model_content = (tmp_path / "src/models.py").read_text()
    seed_content = (tmp_path / "src/seed.py").read_text()
    files = [_gf("src/models.py", model_content), _gf("src/seed.py", seed_content)]
    model_fields = extract_model_fields(files)
    fixed = auto_fix_aliases(files, str(tmp_path), model_fields)

    assert "src/seed.py" in fixed
    result = (tmp_path / "src/seed.py").read_text()
    assert "desc=" not in result
    assert "description=" in result


# ── End-to-end regression: model/seed drift ───────────────────────────────────

def test_model_seed_drift_full_pipeline(tmp_path):
    """Regression: Category has no 'description' field; seed passes one.

    The guard must:
      1. Extract Category fields correctly (no 'description').
      2. Detect the invalid kwarg in scan_issues.
      3. Not corrupt the model file via alias renaming.
    """
    model_py = (
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import DeclarativeBase\n\n"
        "class Base(DeclarativeBase): pass\n\n"
        "class Category(Base):\n"
        "    __tablename__ = 'categories'\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(100), nullable=False)\n"
        "    slug = Column(String(100), unique=True)\n"
    )
    seed_py = (
        "from src.models import Category\n\n"
        "def seed(db):\n"
        "    db.add(Category(\n"
        "        name='Electronics',\n"
        "        slug='electronics',\n"
        "        description='All electronic items',  # NOT in model\n"
        "    ))\n"
        "    db.commit()\n"
    )
    _write(tmp_path, "src/models.py", model_py)
    _write(tmp_path, "src/seed.py", seed_py)

    files = [
        _gf("src/models.py", (tmp_path / "src/models.py").read_text()),
        _gf("src/seed.py", (tmp_path / "src/seed.py").read_text()),
    ]

    model_fields = extract_model_fields(files)

    # Step 1: fields extracted correctly
    assert "Category" in model_fields
    assert "description" not in model_fields["Category"]
    assert {"id", "name", "slug"}.issubset(model_fields["Category"])

    # Step 2: drift detected
    issues = scan_issues(files, model_fields)
    assert "src/seed.py" in issues
    assert any("description" in msg for msg in issues["src/seed.py"])

    # Step 3: model file untouched
    alias_fixed = auto_fix_aliases(files, str(tmp_path), model_fields)
    assert "src/models.py" not in alias_fixed
    assert (tmp_path / "src/models.py").read_text() == model_py
