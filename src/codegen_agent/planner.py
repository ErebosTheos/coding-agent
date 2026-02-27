import json
from .models import Plan, Feature
from .llm.protocol import LLMClient
from .utils import extract_code_from_markdown

PLANNER_SYSTEM_PROMPT = """You are an expert Product Manager and System Architect.
Your goal is to take a user request and produce a high-level project plan in JSON format.
The plan must include:
- project_name: A short, descriptive name.
- tech_stack: A summary of the languages and frameworks to use.
- features: A list of objects with id, title, and description.
- entry_point: The main file to run the application (e.g., main.py, index.html).
- test_strategy: A brief description of how to test the application.

Respond ONLY with the JSON block."""

PLANNER_USER_PROMPT_TEMPLATE = """User Request: {prompt}

Generate a project plan for this request."""

class Planner:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def plan(self, prompt: str) -> Plan:
        """Generates a project plan from a user prompt."""
        user_prompt = PLANNER_USER_PROMPT_TEMPLATE.format(prompt=prompt)
        response = await self.llm_client.generate(user_prompt, system_prompt=PLANNER_SYSTEM_PROMPT)
        
        json_blocks = extract_code_from_markdown(response, "json")
        if not json_blocks:
            # Fallback to the entire response if no JSON blocks found
            try:
                data = json.loads(response)
            except json.JSONDecodeError:
                raise ValueError(f"Failed to extract JSON from planner response: {response}")
        else:
            data = json.loads(json_blocks[0])

        features = [Feature(**f) for f in data.get('features', [])]
        return Plan(
            project_name=data['project_name'],
            tech_stack=data['tech_stack'],
            features=features,
            entry_point=data['entry_point'],
            test_strategy=data['test_strategy']
        )
