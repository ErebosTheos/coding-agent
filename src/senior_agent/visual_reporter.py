from __future__ import annotations

import json
from datetime import datetime, timezone
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from senior_agent.models import ImplementationPlan, SessionReport
from senior_agent.utils import is_within_workspace


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
        node_statuses = {record.node_id: record.status.value for record in report.node_records}

        lines: list[str] = ["flowchart TD"]
        classes: list[str] = []

        root_id = "feature_0"
        lines.append(f'{root_id}["Feature: {self._escape_label(feature_name)}"]')
        lines.append("classDef success fill:%s,stroke:%s,color:%s;" % (self.success_fill, self.success_stroke, self.success_stroke))
        lines.append("classDef fail fill:%s,stroke:%s,color:%s;" % (self.fail_fill, self.fail_stroke, self.fail_stroke))
        lines.append("classDef neutral fill:%s,stroke:%s,color:%s;" % (self.neutral_fill, self.neutral_stroke, self.neutral_stroke))
        classes.append(f"class {root_id} neutral;")

        file_node_ids: list[str] = []
        if plan.dependency_graph is not None and plan.dependency_graph.nodes:
            lines.append("subgraph node_grid [Execution Nodes]")
            graph_node_ids: dict[str, str] = {}
            for node in plan.dependency_graph.nodes:
                node_id = self._graph_node_id(node.node_id)
                graph_node_ids[node.node_id] = node_id
                status = node_statuses.get(node.node_id, "pending")
                label = (
                    f"Node {node.node_id}: {node.title}\\n"
                    f"Status: {status}"
                )
                lines.append(f'{node_id}["{self._escape_label(label)}"]')
                if status == "success":
                    classes.append(f"class {node_id} success;")
                elif status in {"failed", "evicted"}:
                    classes.append(f"class {node_id} fail;")
                else:
                    classes.append(f"class {node_id} neutral;")
                file_node_ids.append(node_id)

            for node in plan.dependency_graph.nodes:
                target_id = graph_node_ids[node.node_id]
                if node.depends_on:
                    for dependency in node.depends_on:
                        source = graph_node_ids.get(dependency)
                        if source:
                            lines.append(f"{source} --> {target_id}")
                else:
                    lines.append(f"{root_id} --> {target_id}")
            lines.append("end")
        elif plan.new_files:
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

        if report.telemetry is not None:
            telemetry_id = "telemetry_0"
            telemetry = report.telemetry
            label = (
                f"Parallel Gain: {telemetry.parallel_gain:.2f}\\n"
                f"Concurrency: {telemetry.initial_concurrency}->{telemetry.final_concurrency}\\n"
                f"Level1 pass/fail: {telemetry.level1_pass_nodes}/{telemetry.level1_failed_nodes}"
            )
            lines.append(f'{telemetry_id}["{self._escape_label(label)}"]')
            lines.append(f"{outcome_id} --> {telemetry_id}")
            classes.append("class telemetry_0 neutral;")

        lines.extend(classes)
        return "\n".join(lines)

    def generate_dashboard_payload(
        self,
        plan: ImplementationPlan,
        report: SessionReport,
        *,
        workspace_root: Path | None = None,
        stage: str = "final",
    ) -> dict[str, Any]:
        telemetry_payload = (
            None if report.telemetry is None else report.telemetry.to_dict()
        )
        node_records = {record.node_id: record for record in report.node_records}
        nodes: list[dict[str, Any]] = []

        if plan.dependency_graph is not None and plan.dependency_graph.nodes:
            for node in plan.dependency_graph.nodes:
                record = node_records.get(node.node_id)
                trace_path = self._resolve_trace_relative_path(
                    workspace_root=workspace_root,
                    node_id=node.node_id,
                )
                nodes.append(
                    {
                        "node_id": node.node_id,
                        "title": node.title,
                        "summary": node.summary,
                        "contract_node": node.contract_node,
                        "depends_on": list(node.depends_on),
                        "status": (
                            "pending" if record is None else record.status.value
                        ),
                        "trace_id": "" if record is None else record.trace_id,
                        "duration_seconds": 0.0 if record is None else record.duration_seconds,
                        "note": "" if record is None else record.note,
                        "commands_run": [] if record is None else list(record.commands_run),
                        "trace_log_path": trace_path,
                    }
                )
        else:
            for index, raw_path in enumerate([*plan.new_files, *plan.modified_files], start=1):
                nodes.append(
                    {
                        "node_id": f"file_{index}",
                        "title": raw_path,
                        "summary": "File-level execution unit",
                        "contract_node": False,
                        "depends_on": [],
                        "status": "success" if report.success else "failed",
                        "trace_id": "",
                        "duration_seconds": 0.0,
                        "note": "",
                        "commands_run": [],
                        "trace_log_path": None,
                    }
                )

        return {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "feature_name": plan.feature_name,
            "summary": plan.summary,
            "success": report.success,
            "blocked_reason": report.blocked_reason,
            "final_result": {
                "command": report.final_result.command,
                "return_code": report.final_result.return_code,
                "stdout": report.final_result.stdout,
                "stderr": report.final_result.stderr,
            },
            "telemetry": telemetry_payload,
            "validation_commands": list(plan.validation_commands),
            "nodes": nodes,
        }

    def generate_dashboard_html(
        self,
        *,
        initial_payload: dict[str, Any],
        dashboard_json_relative_path: str,
    ) -> str:
        initial_json = json.dumps(initial_payload, ensure_ascii=False)
        source_path_json = json.dumps(dashboard_json_relative_path, ensure_ascii=False)
        template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Senior Agent Dashboard</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --panel-muted: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --success: #16a34a;
      --failed: #dc2626;
      --pending: #f59e0b;
      --running: #0ea5e9;
      --border: #374151;
      --mono: "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
      color: var(--text);
      background: radial-gradient(circle at 15% 10%, #1d4ed8 0%, var(--bg) 45%);
      min-height: 100vh;
    }
    .wrap {
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      gap: 16px;
    }
    .card {
      background: color-mix(in oklab, var(--panel) 92%, black 8%);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }
    .title { font-size: 1.4rem; margin: 0 0 8px; }
    .meta { color: var(--muted); font-size: 0.95rem; margin: 0; }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .pill {
      background: var(--panel-muted);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px 12px;
    }
    .pill .k { color: var(--muted); display: block; font-size: 0.85rem; }
    .pill .v { font-weight: 600; margin-top: 4px; display: block; }
    .status-success { color: var(--success); }
    .status-failed, .status-evicted { color: var(--failed); }
    .status-pending { color: var(--pending); }
    .status-running { color: var(--running); }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
      font-size: 0.94rem;
    }
    th, td {
      text-align: left;
      border-bottom: 1px solid var(--border);
      padding: 8px;
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; }
    code, pre { font-family: var(--mono); font-size: 0.88rem; }
    pre {
      white-space: pre-wrap;
      background: var(--panel-muted);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      margin: 0;
      max-height: 240px;
      overflow: auto;
    }
  </style>
</head>
<body>
  <main class="wrap">
    <section class="card">
      <h1 class="title" id="feature-name">Senior Agent Dashboard</h1>
      <p class="meta" id="summary-text"></p>
      <div class="stats">
        <div class="pill"><span class="k">Stage</span><span class="v" id="stage-text"></span></div>
        <div class="pill"><span class="k">Outcome</span><span class="v" id="outcome-text"></span></div>
        <div class="pill"><span class="k">Updated</span><span class="v" id="updated-text"></span></div>
        <div class="pill"><span class="k">Parallel Gain</span><span class="v" id="parallel-gain">n/a</span></div>
      </div>
    </section>
    <section class="card">
      <h2 class="title">Execution Grid</h2>
      <table>
        <thead>
          <tr>
            <th>Node</th>
            <th>Status</th>
            <th>Trace</th>
            <th>Duration(s)</th>
            <th>Dependencies</th>
            <th>Note</th>
          </tr>
        </thead>
        <tbody id="node-body"></tbody>
      </table>
    </section>
    <section class="card">
      <h2 class="title">Final Result</h2>
      <pre id="final-result"></pre>
    </section>
  </main>
  <script>
    const sourcePath = __SOURCE_PATH__;
    let payload = __INITIAL_PAYLOAD__;

    function safe(value) {
      if (value === null || value === undefined) return "";
      return String(value);
    }
    function statusClass(status) {
      return `status-${safe(status).toLowerCase()}`;
    }
    function render(data) {
      payload = data || payload;
      if (!payload) return;
      document.getElementById("feature-name").textContent = safe(payload.feature_name || "Senior Agent Dashboard");
      document.getElementById("summary-text").textContent = safe(payload.summary || "");
      document.getElementById("stage-text").textContent = safe(payload.stage || "unknown");
      const outcomeLabel = payload.success ? "SUCCESS" : "IN PROGRESS / FAILED";
      const outcomeEl = document.getElementById("outcome-text");
      outcomeEl.textContent = outcomeLabel;
      outcomeEl.className = `v ${payload.success ? "status-success" : "status-pending"}`;
      document.getElementById("updated-text").textContent = safe(payload.updated_at || "");
      const gain = payload.telemetry && payload.telemetry.parallel_gain;
      document.getElementById("parallel-gain").textContent = gain === undefined || gain === null ? "n/a" : Number(gain).toFixed(2);

      const body = document.getElementById("node-body");
      body.textContent = "";
      const nodes = Array.isArray(payload.nodes) ? payload.nodes : [];
      for (const node of nodes) {
        const row = document.createElement("tr");
        const nameCell = document.createElement("td");
        nameCell.innerHTML = `<strong>${safe(node.node_id)}</strong><br><span>${safe(node.title || "")}</span>`;
        row.appendChild(nameCell);

        const statusCell = document.createElement("td");
        statusCell.textContent = safe(node.status || "pending");
        statusCell.className = statusClass(node.status || "pending");
        row.appendChild(statusCell);

        const traceCell = document.createElement("td");
        if (node.trace_log_path) {
          const link = document.createElement("code");
          link.textContent = safe(node.trace_log_path);
          traceCell.appendChild(link);
        } else {
          traceCell.textContent = "n/a";
        }
        row.appendChild(traceCell);

        const durationCell = document.createElement("td");
        durationCell.textContent = Number(node.duration_seconds || 0).toFixed(2);
        row.appendChild(durationCell);

        const depsCell = document.createElement("td");
        depsCell.textContent = (node.depends_on || []).join(", ") || "-";
        row.appendChild(depsCell);

        const noteCell = document.createElement("td");
        noteCell.textContent = safe(node.note || "");
        row.appendChild(noteCell);

        body.appendChild(row);
      }

      document.getElementById("final-result").textContent = JSON.stringify(payload.final_result || {}, null, 2);
    }

    async function refresh() {
      if (!sourcePath) return;
      try {
        const response = await fetch(`${sourcePath}?_=${Date.now()}`, { cache: "no-store" });
        if (!response.ok) return;
        const latest = await response.json();
        render(latest);
      } catch (_) {}
    }

    render(payload);
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""
        return template.replace("__INITIAL_PAYLOAD__", initial_json).replace(
            "__SOURCE_PATH__", source_path_json
        )

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

    @staticmethod
    def _graph_node_id(node_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", node_id).strip("_")
        if not safe:
            safe = "node"
        return f"graph_node_{safe}"

    @staticmethod
    def _resolve_trace_relative_path(*, workspace_root: Path | None, node_id: str) -> str | None:
        if workspace_root is None:
            return None
        workspace_resolved = workspace_root.resolve()
        safe_node_id = re.sub(r"[^A-Za-z0-9_-]+", "_", node_id).strip("_") or "node"
        candidate = (workspace_resolved / ".senior_agent" / f"node_{safe_node_id}.log").resolve()
        if not is_within_workspace(workspace_resolved, candidate):
            return None
        if not candidate.exists() or not candidate.is_file():
            return None
        try:
            return candidate.relative_to(workspace_resolved).as_posix()
        except ValueError:
            return None
