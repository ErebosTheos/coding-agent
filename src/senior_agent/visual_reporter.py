from __future__ import annotations

import re
from dataclasses import dataclass

from senior_agent.models import ImplementationPlan, SessionReport


@dataclass(frozen=True)
class VisualReporter:
    """Generate Mermaid summaries for orchestrator planning and execution outcomes."""

    success_fill: str = "#d4edda"
    success_stroke: str = "#1b5e20"
    fail_fill: str = "#f8d7da"
    fail_stroke: str = "#7f1d1d"
    neutral_fill: str = "#e2e8f0"
    neutral_stroke: str = "#334155"

    def generate_mermaid_summary(self, plan: ImplementationPlan, report: SessionReport) -> str:
        feature_name = plan.feature_name.strip() or "Unnamed Feature"

        lines: list[str] = ["flowchart TD"]
        classes: list[str] = []

        root_id = "feature_0"
        lines.append(f'{root_id}["Feature: {self._escape_label(feature_name)}"]')
        lines.append("classDef success fill:%s,stroke:%s,color:%s;" % (self.success_fill, self.success_stroke, self.success_stroke))
        lines.append("classDef fail fill:%s,stroke:%s,color:%s;" % (self.fail_fill, self.fail_stroke, self.fail_stroke))
        lines.append("classDef neutral fill:%s,stroke:%s,color:%s;" % (self.neutral_fill, self.neutral_stroke, self.neutral_stroke))
        classes.append(f"class {root_id} neutral;")

        file_node_ids: list[str] = []
        if plan.new_files:
            for index, file_path in enumerate(plan.new_files, start=1):
                node_id = f"new_file_{index}"
                label = f"NEW: {file_path}"
                lines.append(f'{node_id}["{self._escape_label(label)}"]')
                lines.append(f"{root_id} --> {node_id}")
                file_node_ids.append(node_id)
                classes.append(f"class {node_id} neutral;")

        if plan.modified_files:
            for index, file_path in enumerate(plan.modified_files, start=1):
                node_id = f"modified_file_{index}"
                label = f"MODIFIED: {file_path}"
                lines.append(f'{node_id}["{self._escape_label(label)}"]')
                lines.append(f"{root_id} --> {node_id}")
                file_node_ids.append(node_id)
                classes.append(f"class {node_id} neutral;")

        if not file_node_ids:
            file_node_ids.append("no_files_0")
            lines.append('no_files_0["No implementation files declared"]')
            lines.append(f"{root_id} --> no_files_0")
            classes.append("class no_files_0 neutral;")

        step_node_ids: list[str] = []
        if plan.steps:
            lines.append("subgraph plan_steps [Plan Steps]")
            previous_step_id: str | None = None
            for index, step in enumerate(plan.steps, start=1):
                node_id = f"step_{index}"
                label = step.strip() or f"Step {index}"
                lines.append(f'{node_id}["{self._escape_label(label)}"]')
                if previous_step_id is not None:
                    lines.append(f"{previous_step_id} --> {node_id}")
                previous_step_id = node_id
                step_node_ids.append(node_id)
                classes.append(f"class {node_id} neutral;")
            lines.append("end")
            lines.append(f"{root_id} --> {step_node_ids[0]}")

        validation_node_ids: list[str] = []
        validation_statuses = self._derive_validation_statuses(plan, report)
        if plan.validation_commands:
            for index, command in enumerate(plan.validation_commands, start=1):
                node_id = f"validation_{index}"
                status = validation_statuses[index - 1]
                label = f"Validate: {command}\\nStatus: {status}"
                lines.append(f'{node_id}["{self._escape_label(label)}"]')
                validation_node_ids.append(node_id)
                classes.append(
                    f"class {node_id} {'success' if status == 'Success' else 'fail'};"
                )
                for source_id in file_node_ids:
                    lines.append(f"{source_id} --> {node_id}")
            if step_node_ids:
                lines.append(f"{step_node_ids[-1]} --> {validation_node_ids[0]}")
        else:
            lines.append('validation_none_0["No validation commands configured"]')
            validation_node_ids.append("validation_none_0")
            for source_id in file_node_ids:
                lines.append(f"{source_id} --> validation_none_0")
            if step_node_ids:
                lines.append(f"{step_node_ids[-1]} --> validation_none_0")
            classes.append("class validation_none_0 neutral;")

        outcome_id = "outcome_0"
        outcome_status = "Success" if report.success else "Fail"
        outcome_reason = report.blocked_reason.strip() if report.blocked_reason else ""
        outcome_label = f"Outcome: {outcome_status}"
        if outcome_reason:
            outcome_label = f"{outcome_label}\\nReason: {outcome_reason}"
        lines.append(f'{outcome_id}["{self._escape_label(outcome_label)}"]')
        classes.append(
            f"class {outcome_id} {'success' if report.success else 'fail'};"
        )

        lines.append(f"{validation_node_ids[-1]} --> {outcome_id}")
        lines.extend(classes)
        return "\n".join(lines)

    def _derive_validation_statuses(
        self,
        plan: ImplementationPlan,
        report: SessionReport,
    ) -> list[str]:
        commands = list(plan.validation_commands)
        if not commands:
            return []
        if report.success:
            return ["Success"] * len(commands)

        final_command = report.final_result.command
        final_failed = report.final_result.return_code != 0

        statuses = ["Fail"] * len(commands)
        if final_failed and final_command in commands:
            failed_index = commands.index(final_command)
            for index in range(failed_index):
                statuses[index] = "Success"
            statuses[failed_index] = "Fail"
            return statuses

        return statuses

    @staticmethod
    def _escape_label(value: str) -> str:
        escaped = value.replace("\\", "\\\\")
        escaped = escaped.replace('"', '\\"')
        escaped = escaped.replace("\n", " ")
        escaped = re.sub(r"\s+", " ", escaped).strip()
        return escaped
