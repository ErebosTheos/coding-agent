from codegen_agent.architect import ARCHITECT_SYSTEM_PROMPT
from codegen_agent.executor import EXECUTOR_SYSTEM_PROMPT
from codegen_agent.planner_architect import COMBINED_SYSTEM_PROMPT


def test_planner_architect_prompt_includes_async_sqlalchemy_guardrails():
    assert "expire_on_commit=False" in COMBINED_SYSTEM_PROMPT
    assert "selectinload" in COMBINED_SYSTEM_PROMPT
    assert "MissingGreenlet" in COMBINED_SYSTEM_PROMPT


def test_executor_prompt_includes_async_sqlalchemy_guardrails():
    assert "expire_on_commit=False" in EXECUTOR_SYSTEM_PROMPT
    assert "selectinload" in EXECUTOR_SYSTEM_PROMPT
    assert "MissingGreenlet" in EXECUTOR_SYSTEM_PROMPT


def test_architect_prompt_includes_async_sqlalchemy_guardrails():
    assert "expire_on_commit=False" in ARCHITECT_SYSTEM_PROMPT
    assert "selectinload" in ARCHITECT_SYSTEM_PROMPT
    assert "MissingGreenlet" in ARCHITECT_SYSTEM_PROMPT
