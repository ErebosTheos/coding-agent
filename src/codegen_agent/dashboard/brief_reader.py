"""Universal brief parser: PDF / DOCX / TXT / MD → structured ProjectBrief.

Uses the codegen_agent LLM router (planner role) to extract structured fields.
Falls back gracefully if JSON parsing fails.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

_EXTRACTION_PROMPT = """\
You are a project brief analyst. Extract structured data from the brief below.

Return ONLY valid JSON — no markdown fences, no explanation:
{
  "name": "slug-style-project-name",
  "description": "One paragraph describing what this project is",
  "target_users": ["list", "of", "user", "types"],
  "features": ["each", "required", "feature", "as", "a", "brief", "string"],
  "tech_hints": ["any", "mentioned", "tech", "stack", "preferences"],
  "accessibility": false,
  "timeline": "deadline or empty string"
}

Rules:
- name must be lowercase kebab-case, max 30 chars, inferred from the project identity
- features must be concrete and implementable
- tech_hints: only include what is explicitly mentioned; empty list if none
- accessibility: true only if the brief mentions a11y, WCAG, screen readers, or inclusive design

BRIEF:
"""


@dataclass
class ProjectBrief:
    name: str
    description: str
    target_users: list[str]
    features: list[str]
    tech_hints: list[str]
    accessibility: bool
    timeline: str
    raw_text: str
    source_file: str = ""
    parallel: bool = False
    priority: str = "normal"
    mode: str = "all"

    def to_dict(self) -> dict:
        return asdict(self)


# ── Text extraction ───────────────────────────────────────────────────────────

def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as exc:
        log.warning("PDF extraction failed (%s): %s", path.name, exc)
        return ""


def _extract_docx(path: Path) -> str:
    try:
        import docx
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as exc:
        log.warning("DOCX extraction failed (%s): %s", path.name, exc)
        return ""


def _extract_plain(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_bytes().decode("utf-8", errors="replace")


def extract_raw_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        text = _extract_pdf(path)
    elif ext == ".docx":
        text = _extract_docx(path)
    else:
        text = _extract_plain(path)

    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── LLM parsing ───────────────────────────────────────────────────────────────

async def _call_llm(prompt: str) -> str:
    """Call the codegen_agent LLM router (planner role) with a simple prompt."""
    from ..llm.router import LLMRouter
    router = LLMRouter()
    client = router.get_client_for_role("planner")
    return await client.generate(prompt)


async def parse_brief(path: Path, sidecar: dict | None = None) -> ProjectBrief:
    """Parse a brief file into a structured ProjectBrief."""
    log.info("Parsing brief: %s", path.name)
    raw_text = extract_raw_text(path)

    if not raw_text.strip():
        raise ValueError(f"Could not extract any text from {path.name}")

    prompt = _EXTRACTION_PROMPT + raw_text[:8000]

    try:
        resp = await _call_llm(prompt)
        resp = re.sub(r"```(?:json)?\n?", "", resp).strip().rstrip("`")
        data = json.loads(resp)
    except Exception as exc:
        log.warning("Brief LLM extraction failed (%s), using fallback: %s", path.name, exc)
        data = {
            "name": re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")[:30],
            "description": raw_text[:300],
            "target_users": [],
            "features": [],
            "tech_hints": [],
            "accessibility": False,
            "timeline": "",
        }

    brief = ProjectBrief(
        name=data.get("name", path.stem.lower()[:30]),
        description=data.get("description", ""),
        target_users=data.get("target_users", []),
        features=data.get("features", []),
        tech_hints=data.get("tech_hints", []),
        accessibility=bool(data.get("accessibility", False)),
        timeline=data.get("timeline", ""),
        raw_text=raw_text,
        source_file=str(path),
        parallel=sidecar.get("parallel", False) if sidecar else False,
        priority=sidecar.get("priority", "normal") if sidecar else "normal",
        mode=sidecar.get("mode", "all") if sidecar else "all",
    )

    log.info("Brief parsed → project: %s", brief.name)
    return brief


async def parse_text(text: str, sidecar: dict | None = None) -> ProjectBrief:
    """Parse a raw text description into a ProjectBrief."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp = Path(f.name)
    try:
        return await parse_brief(tmp, sidecar)
    finally:
        tmp.unlink(missing_ok=True)
