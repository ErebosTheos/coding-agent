import importlib
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class CompatibilityShimTests(unittest.TestCase):
    def test_legacy_engine_aliases_map_to_senior_agent(self) -> None:
        from self_healing_agent.engine import SelfHealingAgent, create_default_agent
        from senior_agent.engine import SeniorAgent, create_default_senior_agent

        self.assertIs(SelfHealingAgent, SeniorAgent)
        self.assertIs(create_default_agent, create_default_senior_agent)

    def test_legacy_strategy_imports_resolve(self) -> None:
        from self_healing_agent.strategies import LLMStrategy as LegacyLLMStrategy
        from senior_agent.strategies import LLMStrategy as CanonicalLLMStrategy

        self.assertIs(LegacyLLMStrategy, CanonicalLLMStrategy)

    def test_legacy_planner_imports_resolve(self) -> None:
        from self_healing_agent.models import ImplementationPlan as LegacyPlan
        from self_healing_agent.planner import FeaturePlanner as LegacyPlanner
        from senior_agent.models import ImplementationPlan as CanonicalPlan
        from senior_agent.planner import FeaturePlanner as CanonicalPlanner

        self.assertIs(LegacyPlanner, CanonicalPlanner)
        self.assertIs(LegacyPlan, CanonicalPlan)

    def test_legacy_top_level_feature_planner_export_resolves(self) -> None:
        from self_healing_agent import FeaturePlanner as LegacyPlanner
        from senior_agent.planner import FeaturePlanner as CanonicalPlanner

        self.assertIs(LegacyPlanner, CanonicalPlanner)

    def test_legacy_top_level_orchestrator_export_resolves(self) -> None:
        from self_healing_agent import MultiAgentOrchestrator as LegacyOrchestrator
        from self_healing_agent.orchestrator import MultiAgentOrchestrator as LegacyModuleOrchestrator
        from senior_agent.orchestrator import MultiAgentOrchestrator as CanonicalOrchestrator

        self.assertIs(LegacyOrchestrator, CanonicalOrchestrator)
        self.assertIs(LegacyModuleOrchestrator, CanonicalOrchestrator)

    def test_legacy_test_writer_export_resolves(self) -> None:
        from self_healing_agent import TestWriter as LegacyTopLevelTestWriter
        from self_healing_agent.test_writer import TestWriter as LegacyModuleTestWriter
        from senior_agent.test_writer import TestWriter as CanonicalTestWriter

        self.assertIs(LegacyTopLevelTestWriter, CanonicalTestWriter)
        self.assertIs(LegacyModuleTestWriter, CanonicalTestWriter)

    def test_legacy_dependency_manager_export_resolves(self) -> None:
        from self_healing_agent import DependencyManager as LegacyTopLevelDependencyManager
        from self_healing_agent.dependency_manager import (
            DependencyManager as LegacyModuleDependencyManager,
        )
        from senior_agent.dependency_manager import DependencyManager as CanonicalDependencyManager

        self.assertIs(LegacyTopLevelDependencyManager, CanonicalDependencyManager)
        self.assertIs(LegacyModuleDependencyManager, CanonicalDependencyManager)

    def test_legacy_style_mimic_export_resolves(self) -> None:
        from self_healing_agent import StyleMimic as LegacyTopLevelStyleMimic
        from self_healing_agent.style_mimic import StyleMimic as LegacyModuleStyleMimic
        from senior_agent.style_mimic import StyleMimic as CanonicalStyleMimic

        self.assertIs(LegacyTopLevelStyleMimic, CanonicalStyleMimic)
        self.assertIs(LegacyModuleStyleMimic, CanonicalStyleMimic)

    def test_importing_legacy_package_emits_deprecation_warning(self) -> None:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", DeprecationWarning)
            module = importlib.import_module("self_healing_agent")
            importlib.reload(module)

        self.assertTrue(
            any(
                "deprecated" in str(item.message).lower()
                for item in captured
            )
        )


if __name__ == "__main__":
    unittest.main()
