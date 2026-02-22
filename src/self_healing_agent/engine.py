from __future__ import annotations

from senior_agent.engine import (
    SeniorAgent,
    create_default_senior_agent,
    run_shell_command,
)

SelfHealingAgent = SeniorAgent
create_default_agent = create_default_senior_agent

__all__ = [
    "SeniorAgent",
    "SelfHealingAgent",
    "create_default_senior_agent",
    "create_default_agent",
    "run_shell_command",
]
