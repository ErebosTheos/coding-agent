from senior_agent.classifier import classify_failure
from senior_agent.dependency_manager import DependencyManager
from senior_agent.engine import (
    SeniorAgent,
    create_default_senior_agent,
    run_shell_command,
)
from senior_agent.llm_client import (
    CodexCLIClient,
    DEFAULT_TRANSPORT_SAFETY_PROMPT,
    DEFAULT_TRANSPORT_SYSTEM_PROMPT,
    GeminiCLIClient,
    LocalOffloadClient,
    LLMClient,
    LLMClientError,
    LLMRateLimitError,
    LLMTimeoutError,
    MultiCloudRouter,
    SpeculativeResponseParser,
    build_transport_prompt,
    parse_streamed_response,
)
from senior_agent.models import (
    AttemptRecord,
    CommandResult,
    DependencyGraph,
    ExecutionNode,
    FailureContext,
    FailureType,
    FileRollback,
    FixOutcome,
    ImplementationPlan,
    NodeExecutionRecord,
    NodeStatus,
    OrchestrationTelemetry,
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
from senior_agent.symbol_graph import SymbolGraph
from senior_agent.test_writer import TestWriter

SelfHealingAgent = SeniorAgent
create_default_agent = create_default_senior_agent

__all__ = [
    "AttemptRecord",
    "CommandResult",
    "DependencyGraph",
    "ExecutionNode",
    "FailureContext",
    "FailureType",
    "FileRollback",
    "FixOutcome",
    "ImplementationPlan",
    "NodeExecutionRecord",
    "NodeStatus",
    "OrchestrationTelemetry",
    "SessionReport",
    "FeaturePlanner",
    "TestWriter",
    "DependencyManager",
    "StyleMimic",
    "SymbolGraph",
    "MultiAgentOrchestrator",
    "SeniorAgent",
    "SelfHealingAgent",
    "create_default_senior_agent",
    "create_default_agent",
    "LLMClient",
    "LLMClientError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "DEFAULT_TRANSPORT_SYSTEM_PROMPT",
    "DEFAULT_TRANSPORT_SAFETY_PROMPT",
    "build_transport_prompt",
    "SpeculativeResponseParser",
    "parse_streamed_response",
    "CodexCLIClient",
    "GeminiCLIClient",
    "LocalOffloadClient",
    "MultiCloudRouter",
    "LLMStrategy",
    "NoopStrategy",
    "RegexReplaceStrategy",
    "RepoRegexReplaceStrategy",
    "classify_failure",
    "run_shell_command",
]
