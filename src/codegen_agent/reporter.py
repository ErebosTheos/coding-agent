import dataclasses
import json
import os
from enum import Enum
from .models import PipelineReport
from .run_log import make_run_summary, append_run_summary

class Reporter:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.report_dir = os.path.join(workspace, ".codegen_agent")

    def generate_summary(self, report: PipelineReport) -> str:
        """Generates a human-readable summary of the pipeline run."""
        summary = [
            f"# Pipeline Report: {report.plan.project_name if report.plan else 'Unknown'}",
            f"**Prompt:** {report.prompt}",
            f"**Status:** {'Success' if report.qa_report and report.qa_report.approved else 'In Progress/Failed'}",
            f"**Wall Clock Time:** {report.wall_clock_seconds:.2f}s",
            "",
            "## Stages"
        ]
        
        stages = [
            ("PLAN", report.plan is not None),
            ("ARCHITECT", report.architecture is not None),
            ("EXECUTE", report.execution_result is not None),
            ("TESTS", report.test_suite is not None),
            ("HEAL", report.healing_report.success if report.healing_report else False),
            ("QA", report.qa_report is not None),
        ]
        
        for name, success in stages:
            summary.append(f"- **{name}:** {'✅' if success else '❌'}")

        if report.execution_result and report.execution_result.generated_files:
            summary.append("")
            summary.append("## Generated Source Files")
            for gf in report.execution_result.generated_files:
                summary.append(f"- `{gf.file_path}`")

        if report.test_suite and report.test_suite.test_files:
            summary.append("")
            summary.append("## Generated Test Files")
            for tf in report.test_suite.test_files.keys():
                summary.append(f"- `{tf}`")
            
        if report.qa_report:
            summary.append("")
            summary.append(f"## QA Score: {report.qa_report.score}/100")
            summary.append("### Issues")
            for issue in report.qa_report.issues:
                summary.append(f"- {issue}")
        
        return "\n".join(summary)

    def generate_mermaid(self, report: PipelineReport) -> str:
        """Generates a Mermaid diagram representing the project architecture."""
        if not report.architecture:
            return "graph TD\n  A[No Architecture Generated]"
        
        lines = ["graph TD"]
        for node in report.architecture.nodes:
            label = f"{node.node_id} ({node.file_path})"
            lines.append(f'  {node.node_id}["{self._escape_label(label)}"]')
            for dep in node.depends_on:
                lines.append(f"  {dep} --> {node.node_id}")
                
        return "\n".join(lines)

    def _escape_label(self, label: str) -> str:
        return label.replace('"', '"')

    def save_report(self, report: PipelineReport):
        """Saves the report and mermaid diagram to the workspace."""
        os.makedirs(self.report_dir, exist_ok=True)
        
        # Save JSON report
        def _default(obj):
            if isinstance(obj, Enum):
                return obj.value
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        with open(os.path.join(self.report_dir, "pipeline_report.json"), 'w') as f:
            json.dump(report.to_dict(), f, indent=2, default=_default)

        # Save per-stage trace log (append mode — one JSONL line per stage per run)
        if report.stage_traces:
            traces_path = os.path.join(self.report_dir, "traces.jsonl")
            with open(traces_path, "a") as f:
                for trace in report.stage_traces:
                    f.write(json.dumps(dataclasses.asdict(trace)) + "\n")
            
        # Save Mermaid diagram
        mermaid = self.generate_mermaid(report)
        with open(os.path.join(self.report_dir, "architecture.mermaid"), 'w') as f:
            f.write(mermaid)
            
        # Save Markdown summary
        summary = self.generate_summary(report)
        with open(os.path.join(self.report_dir, "report_summary.md"), 'w') as f:
            f.write(summary)

        # Append one-line run summary for rolling-window metrics
        run_summary = make_run_summary(report)
        append_run_summary(os.path.join(self.report_dir, "runs.jsonl"), run_summary)
