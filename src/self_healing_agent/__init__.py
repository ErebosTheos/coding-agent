from __future__ import annotations

import warnings

warnings.warn(
    "`self_healing_agent` is deprecated; use `senior_agent` instead.",
    DeprecationWarning,
    stacklevel=2,
)

from senior_agent import (
    AttemptRecord,
    CodexCLIClient,
    CommandResult,
    FailureContext,
    FailureType,
    FileRollback,
    FixOutcome,
    GeminiCLIClient,
    ImplementationPlan,
    LLMClient,
    LLMClientError,
    LLMRateLimitError,
    LLMStrategy,
    LLMTimeoutError,
    NoopStrategy,
    RegexReplaceStrategy,
    RepoRegexReplaceStrategy,
    SeniorAgent,
    SessionReport,
    classify_failure,
    create_default_senior_agent,
    run_shell_command,
)

SelfHealingAgent = SeniorAgent
create_default_agent = create_default_senior_agent


def __getattr__(name: str):
    if name == "FeaturePlanner":
        from senior_agent.planner import FeaturePlanner

        return FeaturePlanner
    if name == "DependencyManager":
        from senior_agent.dependency_manager import DependencyManager

        return DependencyManager
    if name == "StyleMimic":
        from senior_agent.style_mimic import StyleMimic

        return StyleMimic
    if name == "TestWriter":
        from senior_agent.test_writer import TestWriter

        return TestWriter
    if name == "MultiAgentOrchestrator":
        from senior_agent.orchestrator import MultiAgentOrchestrator

        return MultiAgentOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "AttemptRecord",
    "CommandResult",
    "FailureContext",
    "FailureType",
    "FileRollback",
    "FixOutcome",
    "ImplementationPlan",
    "FeaturePlanner",
    "DependencyManager",
    "StyleMimic",
    "TestWriter",
    "MultiAgentOrchestrator",
    "SessionReport",
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
