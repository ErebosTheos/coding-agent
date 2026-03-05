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

Respond ONLY with the JSON block."""

ARCHITECT_USER_PROMPT_TEMPLATE = """Project Plan: {plan_json}

Generate a detailed architecture for this project."""

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
