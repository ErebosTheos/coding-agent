import json
from .models import Plan, Architecture, ExecutionNode, Contract
from .llm.protocol import LLMClient
from .utils import extract_code_from_markdown

ARCHITECT_SYSTEM_PROMPT = """You are an expert Software Architect.
Your goal is to take a project plan and produce a detailed architecture in JSON format.
The architecture must include:
- file_tree: A list of all file paths to be created.
- nodes: A list of objects with:
    - node_id: Unique identifier for the node.
    - file_path: Path to the file.
    - purpose: Brief description of the file's role.
    - depends_on: List of node_ids this file depends on.
    - contract: An object with purpose, inputs, outputs, public_api, and invariants.
      CRITICAL: public_api must list every class, function, and constant that other files
      will import from this file. Do NOT leave public_api as an empty list.
      Example: "public_api": ["UserModel", "TaskModel", "Base", "get_db"]
- global_validation_commands: A list of shell commands to validate the entire project (e.g., linting, type checking).
- For FastAPI + SQLAlchemy async projects:
    - Ensure session setup uses `async_sessionmaker(..., expire_on_commit=False)`.
    - Plan eager-loading (`selectinload`) for API responses that include related ORM data.
    - Avoid lazy-load response serialization patterns that trigger MissingGreenlet.

ADDITIONAL RULES:
- ALWAYS include the language dependency manifest as a node:
    Python   → requirements.txt
    Node/TS  → package.json  (include "scripts": {"start": ..., "test": ...})
    Go       → go.mod
    Rust     → Cargo.toml
    PHP      → composer.json
    Ruby     → Gemfile
- Python only: ALWAYS include an __init__.py node for every package directory.
- Do NOT create directory placeholder nodes. Only list real files.

PLAN RULES:
- For any project with a user-facing interface, the nodes list MUST include explicit frontend
  file nodes — not just backend files. Every UI page must be a planned node with a purpose and
  contract. Missing frontend nodes = missing files. Always include nodes for:
    static/index.html, static/login.html, static/style.css, static/app.js
  And whichever dashboard pages are relevant to the roles in the project.

FRONTEND RULES (mandatory for any project with a user-facing interface):
- If the request mentions: website, portal, dashboard, frontend, UI, public page, login page,
  admin panel, CMS, landing page, or any visual user interface — you MUST generate a complete
  frontend. DO NOT generate a pure API with no HTML.
- For Python backends (FastAPI/Flask): serve the frontend via StaticFiles + Jinja2 templates OR
  a fully self-contained static/ folder. ALWAYS include:
    static/index.html      — public landing page with navigation, hero section, feature cards
    static/style.css       — complete responsive CSS (dark/light theme, mobile-first)
    static/app.js          — vanilla JS for interactivity (login modal, API calls, routing)
    static/dashboard.html  — authenticated dashboard (loads after login)
  AND wire them in main.py / app.py:
    app.mount("/static", StaticFiles(directory="static"), name="static")
    @app.get("/", response_class=HTMLResponse) → serve static/index.html
- For Node/Express backends: serve static/ with express.static() and include the same files.
- The HTML must be real, complete, and production-quality — NOT a placeholder or stub.
  Include: navbar, hero section, features/cards section, login form, footer.
- app.js must implement: JWT auth flow (login → store token → show dashboard),
  fetch wrappers for API calls, navigation between pages, error handling.
- style.css must implement: CSS custom properties, responsive grid/flex layout,
  dark theme, button/form/card styles, accessibility focus indicators.
- NEVER skip the frontend for a website project. A backend-only submission for a website
  brief is an incomplete, failing deliverable.
- The frontend must be production-quality, visually stunning, and feel like a real SaaS product.
  NOT a tutorial demo. Think Stripe, Linear, Vercel-level polish.

Respond ONLY with the JSON block."""

ARCHITECT_USER_PROMPT_TEMPLATE = """Project Plan: {plan_json}

Generate a detailed architecture for this project.

IMPORTANT — BE THOROUGH:
- Create a node for EVERY file needed to fully implement all features in the plan.
- Do not merge multiple routers into one file — each resource (users, courses, tests, results,
  notifications, analytics) gets its own router file.
- Each dashboard page (student, teacher, admin, staff) gets its own HTML file.
- Do not skip seed files, config files, utility modules, or middleware.
- More nodes = more complete implementation. Aim for full coverage, not a skeleton."""

class Architect:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def architect(self, plan: Plan) -> Architecture:
        """Generates a project architecture from a plan."""
        plan_json = json.dumps(plan.to_dict())
        user_prompt = ARCHITECT_USER_PROMPT_TEMPLATE.format(plan_json=plan_json)
        response = await self.llm_client.generate(user_prompt, system_prompt=ARCHITECT_SYSTEM_PROMPT)
        
        json_blocks = extract_code_from_markdown(response, "json")
        if not json_blocks:
            try:
                data = json.loads(response)
            except json.JSONDecodeError:
                raise ValueError(f"Failed to extract JSON from architect response: {response}")
        else:
            data = json.loads(json_blocks[0])

        nodes = []
        for n in data.get('nodes', []):
            contract_data = n.get('contract')
            contract = Contract(**contract_data) if contract_data else None
            nodes.append(ExecutionNode(
                node_id=n['node_id'],
                file_path=n['file_path'],
                purpose=n['purpose'],
                depends_on=n.get('depends_on', []),
                contract=contract
            ))

        return Architecture(
            file_tree=data['file_tree'],
            nodes=nodes,
            global_validation_commands=data.get('global_validation_commands', [])
        )
