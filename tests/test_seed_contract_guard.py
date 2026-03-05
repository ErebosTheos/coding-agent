"""Tests for SeedContractGuard — missing required fields in seed constructor calls."""
from codegen_agent.model_kwarg_guard import (
    extract_model_fields,
    scan_seed_contract_issues,
)
from codegen_agent.models import GeneratedFile


def _gf(path: str, content: str) -> GeneratedFile:
    return GeneratedFile(file_path=path, content=content, node_id="n", sha256="x")


# ── scan_seed_contract_issues ─────────────────────────────────────────────────

def test_detects_missing_slug_when_name_present():
    model = _gf("src/models.py",
        "class Category(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(100))\n"
        "    slug = Column(String(100), unique=True)\n"
    )
    seed = _gf("src/seed.py",
        "def seed(db):\n"
        "    db.add(Category(name='Electronics'))\n"
        "    db.commit()\n"
    )
    fields = extract_model_fields([model, seed])
    issues = scan_seed_contract_issues([model, seed], fields)

    assert "src/seed.py" in issues
    msgs = issues["src/seed.py"]
    assert any("slug" in m for m in msgs)
    assert any("name" in m for m in msgs)


def test_no_issue_when_slug_provided():
    model = _gf("src/models.py",
        "class Category(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
        "    slug = Column(String, unique=True)\n"
    )
    seed = _gf("src/seed.py",
        "db.add(Category(name='Tech', slug='tech'))\n"
        "db.commit()\n"
    )
    fields = extract_model_fields([model, seed])
    issues = scan_seed_contract_issues([model, seed], fields)
    assert "src/seed.py" not in issues


def test_no_issue_when_model_has_no_slug():
    model = _gf("src/models.py",
        "class Tag(Base):\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
    )
    seed = _gf("src/seed.py",
        "db.add(Tag(name='python'))\n"
        "db.commit()\n"
    )
    fields = extract_model_fields([model, seed])
    issues = scan_seed_contract_issues([model, seed], fields)
    assert "src/seed.py" not in issues


def test_only_fires_in_seed_files():
    """Service layer files that call Model(...) without slug should not be flagged."""
    model = _gf("src/models.py",
        "class Category(Base):\n"
        "    name = Column(String)\n"
        "    slug = Column(String)\n"
    )
    # This is a service file, not a seed — no db.add / commit markers
    service = _gf("src/services.py",
        "def create_category(name: str) -> Category:\n"
        "    return Category(name=name)\n"
    )
    fields = extract_model_fields([model, service])
    issues = scan_seed_contract_issues([model, service], fields)
    assert "src/services.py" not in issues


def test_no_issue_when_using_star_star_unpacking():
    """**data unpacking should not be flagged (can contain slug)."""
    model = _gf("src/models.py",
        "class Product(Base):\n"
        "    name = Column(String)\n"
        "    slug = Column(String)\n"
    )
    seed = _gf("src/seed.py",
        "for data in products:\n"
        "    db.add(Product(**data))\n"
        "db.commit()\n"
    )
    fields = extract_model_fields([model, seed])
    issues = scan_seed_contract_issues([model, seed], fields)
    assert "src/seed.py" not in issues
