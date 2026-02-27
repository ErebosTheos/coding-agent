import json
from .models import Plan, Feature, Architecture, ExecutionNode, Contract
from .llm.protocol import LLMClient
from .utils import find_json_in_text, extract_code_from_markdown

COMBINED_SYSTEM_PROMPT = """You are a Senior Software Architect.
Given a user request, return a single JSON object with exactly two keys: "plan" and "architecture".

"plan" schema:
{
  "project_name": string,
  "tech_stack": string,
  "features": [{"id": string, "title": string, "description": string, "priority": int}],
  "entry_point": string,
  "test_strategy": string
}

"architecture" schema:
{
  "file_tree": [string],
  "nodes": [{
    "node_id": string,
    "file_path": string,
    "purpose": string,
    "depends_on": [string],
    "contract": {"purpose": string, "inputs": [], "outputs": [], "public_api": [], "invariants": []}
  }],
  "global_validation_commands": [string]
  // Shell commands that fully validate the project — must match the tech stack exactly.
  // Examples by stack:
  //   Python/pytest:   ["pytest tests/"]
  //   Django:          ["python manage.py test"]
  //   PHP/Laravel:     ["php artisan test"]
  //   PHP/PHPUnit:     ["./vendor/bin/phpunit"]
  //   Node/Jest:       ["npx jest"]
  //   Node/Mocha:      ["npx mocha"]
  //   Go:              ["go test ./..."]
  //   Rust:            ["cargo test"]
  //   Ruby/RSpec:      ["bundle exec rspec"]
  //   Ruby/Rails:      ["bin/rails test"]
  // Include ALL commands needed (lint + test). These are run verbatim in the project root.
}

Respond ONLY with the raw JSON object. No markdown fences, no commentary."""

COMBINED_USER_PROMPT = """User Request: {prompt}

Produce the project plan and full file architecture in a single JSON response."""


class PlannerArchitect:
    """Combines the Planner and Architect into a single LLM call, saving one full round-trip."""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def plan_and_architect(self, prompt: str) -> tuple[Plan, Architecture]:
        user_prompt = COMBINED_USER_PROMPT.format(prompt=prompt)
        response = await self.llm_client.generate(user_prompt, system_prompt=COMBINED_SYSTEM_PROMPT)

        # Try JSON block extraction first, then raw parse
        json_blocks = extract_code_from_markdown(response, "json")
        try:
            data = json.loads(json_blocks[0]) if json_blocks else (find_json_in_text(response) or json.loads(response))
        except (json.JSONDecodeError, TypeError):
            raise ValueError(f"PlannerArchitect: failed to parse combined response: {response[:500]}")

        plan = self._parse_plan(data.get("plan", data))
        architecture = self._parse_architecture(data.get("architecture", data))
        return plan, architecture

    @staticmethod
    def _parse_plan(d: dict) -> Plan:
        features = [Feature(**f) for f in d.get("features", [])]
        return Plan(
            project_name=d["project_name"],
            tech_stack=d["tech_stack"],
            features=features,
            entry_point=d["entry_point"],
            test_strategy=d["test_strategy"],
        )

    @staticmethod
    def _parse_architecture(d: dict) -> Architecture:
        nodes = []
        for n in d.get("nodes", []):
            contract = Contract(**n["contract"]) if n.get("contract") else None
            nodes.append(ExecutionNode(
                node_id=n["node_id"],
                file_path=n["file_path"],
                purpose=n["purpose"],
                depends_on=n.get("depends_on", []),
                contract=contract,
            ))

        # Synthesize nodes for any file_tree entry the LLM forgot to plan.
        # Common omissions: __init__.py, requirements.txt, *.ini, *.toml, README.md
        covered = {n.file_path for n in nodes}
        for path in d.get("file_tree", []):
            if path in covered:
                continue
            import os as _os
            name = _os.path.basename(path)
            purpose = (
                "Empty Python package marker" if name == "__init__.py" else
                "Project dependencies list" if name == "requirements.txt" else
                "Project configuration" if name.endswith((".toml", ".ini", ".cfg")) else
                "Project documentation" if name.endswith(".md") else
                f"Supporting file: {name}"
            )
            synthetic_id = path.replace("/", "_").replace(".", "_")
            nodes.append(ExecutionNode(
                node_id=synthetic_id,
                file_path=path,
                purpose=purpose,
                depends_on=[],
            ))

        return Architecture(
            file_tree=d["file_tree"],
            nodes=nodes,
            global_validation_commands=d.get("global_validation_commands", []),
        )
