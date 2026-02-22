from senior_agent.classifier import classify_failure
from senior_agent.dependency_manager import DependencyManager
from senior_agent.engine import (
    SeniorAgent,
    create_default_senior_agent,
    run_shell_command,
)
from senior_agent.llm_client import (
    CodexCLIClient,
    GeminiCLIClient,
    LLMClient,
    LLMClientError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from senior_agent.models import (
    AttemptRecord,
    CommandResult,
    FailureContext,
    FailureType,
    FileRollback,
    FixOutcome,
    ImplementationPlan,
    SessionReport,
)
from senior_agent.strategies import (
    LLMStrategy,
    NoopStrategy,
    RegexReplaceStrategy,
    RepoRegexReplaceStrategy,
)
from senior_agent.planner import FeaturePlanner
from senior_agent.orchestrator import MultiAgentOrchestrator
from senior_agent.style_mimic import StyleMimic
from senior_agent.test_writer import TestWriter

SelfHealingAgent = SeniorAgent
create_default_agent = create_default_senior_agent

__all__ = [
    "AttemptRecord",
    "CommandResult",
    "FailureContext",
    "FailureType",
    "FileRollback",
    "FixOutcome",
    "ImplementationPlan",
    "SessionReport",
    "FeaturePlanner",
    "TestWriter",
    "DependencyManager",
    "StyleMimic",
    "MultiAgentOrchestrator",
    "SeniorAgent",
    "SelfHealingAgent",
    "create_default_senior_agent",
    "create_default_agent",
    "LLMClient",
    "LLMClientError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "CodexCLIClient",
    "GeminiCLIClient",
    "LLMStrategy",
    "NoopStrategy",
    "RegexReplaceStrategy",
    "RepoRegexReplaceStrategy",
    "classify_failure",
    "run_shell_command",
]
