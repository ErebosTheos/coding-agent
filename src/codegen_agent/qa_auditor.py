import json
from .models import QAReport, PipelineReport
from .llm.protocol import LLMClient
from .utils import find_json_in_text

QA_SYSTEM_PROMPT = """You are an expert QA Auditor and Senior Developer.
Your goal is to audit a completed software project and provide a quality report in JSON format.
The report must include:
- score: A number from 0 to 100.
- issues: A list of identified bugs or poor practices.
- suggestions: A list of improvements.
- approved: A boolean indicating if the project is ready for delivery.

Respond ONLY with the JSON block."""

QA_USER_PROMPT_TEMPLATE = """Project Summary:
{pipeline_summary}

Audit the project and provide a report."""

class QAAuditor:
    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def audit(self, report: PipelineReport) -> QAReport:
        """Audits the project and generates a QA report."""
        # Create a condensed summary for the LLM
        summary = {
            "prompt": report.prompt,
            "plan": report.plan.to_dict() if report.plan else None,
            "architecture": report.architecture.to_dict() if report.architecture else None,
            "execution_success": report.execution_result is not None,
            "healing_success": report.healing_report.success if report.healing_report else False
        }
        
        user_prompt = QA_USER_PROMPT_TEMPLATE.format(pipeline_summary=json.dumps(summary, indent=2))
        response = await self.llm_client.generate(user_prompt, system_prompt=QA_SYSTEM_PROMPT)
        
        data = find_json_in_text(response)
        if not data:
            raise ValueError(f"Failed to extract JSON from QA response: {response}")

        return QAReport(**data)
