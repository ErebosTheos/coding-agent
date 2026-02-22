import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_IMPORT_ERROR: Exception | None = None
try:
    from senior_agent.web_api import (
        ExecutionJob,
        _build_retry_job,
        _collect_created_files,
        _compute_job_progress,
        _tail_file_text,
        _collect_validation_commands,
        _default_task_plan,
        _derive_phase_requirements_from_plan,
        _sanitize_command_list,
        _select_post_heal_commands,
        _split_program_requirement,
    )
except Exception as exc:  # pragma: no cover - dependency guard
    _IMPORT_ERROR = exc


@unittest.skipIf(_IMPORT_ERROR is not None, "web_api dependencies are unavailable.")
class ProgramRequirementSplitTests(unittest.TestCase):
    def test_returns_single_phase_when_no_numbered_sections(self) -> None:
        requirement = "Build an accessible landing page and add authentication."
        phases = _split_program_requirement(requirement, max_phases=6)
        self.assertEqual(phases, [requirement])

    def test_splits_numbered_sections_across_requested_phase_count(self) -> None:
        requirement = (
            "Project Brief\n\n"
            "1. Foundation\nSetup repository.\n\n"
            "2. Authentication\nAdd secure login.\n\n"
            "3. Public Pages\nBuild public site pages.\n\n"
            "4. Dashboards\nImplement role dashboards.\n\n"
            "5. Testing\nAdd validation and tests.\n"
        )
        phases = _split_program_requirement(requirement, max_phases=3)
        self.assertEqual(len(phases), 3)
        self.assertIn("Program delivery phase 1/3.", phases[0])
        self.assertIn("Program delivery phase 2/3.", phases[1])
        self.assertIn("Program delivery phase 3/3.", phases[2])
        combined = "\n".join(phases)
        self.assertIn("1. Foundation", combined)
        self.assertIn("5. Testing", combined)

    def test_phase_count_is_capped_by_available_sections(self) -> None:
        requirement = (
            "Project Brief\n\n"
            "1. One\nDo one.\n\n"
            "2. Two\nDo two.\n"
        )
        phases = _split_program_requirement(requirement, max_phases=8)
        self.assertEqual(len(phases), 2)

    def test_derive_phase_requirements_uses_planned_tasks(self) -> None:
        plan = {
            "feature_name": "Website",
            "summary": "Build core website",
            "tasks": [
                {
                    "id": "T1",
                    "title": "Setup",
                    "requirement": "Create project scaffold.",
                },
                {
                    "id": "T2",
                    "title": "Auth",
                    "requirement": "Implement secure authentication.",
                },
            ],
        }
        phases = _derive_phase_requirements_from_plan(
            plan,
            fallback_requirement="fallback",
            max_phases=4,
        )
        self.assertEqual(len(phases), 2)
        self.assertIn("Task T1 - Setup", phases[0])
        self.assertIn("Create project scaffold.", phases[0])

    def test_default_task_plan_generates_tasks_for_each_phase(self) -> None:
        requirement = (
            "1. Phase One\nBuild first part.\n\n"
            "2. Phase Two\nBuild second part.\n"
        )
        plan = _default_task_plan(requirement, max_phases=6)
        tasks = plan["tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["id"], "T1")
        self.assertEqual(tasks[1]["depends_on"], ["T1"])

    def test_collect_validation_commands_merges_product_and_tasks(self) -> None:
        product_spec = {"validation_commands": ["npm run lint", "npm test"]}
        task_plan = {
            "tasks": [
                {"validation_commands": ["npm test", "npm run typecheck"]},
                {"validation_commands": ["npm run lint"]},
            ]
        }
        commands = _collect_validation_commands(product_spec, task_plan)
        self.assertEqual(commands, ["npm run lint", "npm test", "npm run typecheck"])

    def test_sanitize_command_list_deduplicates_and_trims(self) -> None:
        commands = _sanitize_command_list([" npm test ", "", "npm test", "npm run lint"])
        self.assertEqual(commands, ["npm test", "npm run lint"])

    def test_select_post_heal_commands_uses_spec_first(self) -> None:
        workspace = Path(".").resolve()
        product_spec = {"validation_commands": ["npm run lint", "npm test"]}
        task_plan = {"tasks": [{"validation_commands": ["npm run typecheck"]}]}
        primary, validations, source = _select_post_heal_commands(
            workspace=workspace,
            product_spec_payload=product_spec,
            task_plan_payload=task_plan,
        )
        self.assertEqual(primary, "npm run lint")
        self.assertEqual(validations, ["npm test", "npm run typecheck"])
        self.assertEqual(source, "spec_and_plan_validation_commands")

    def test_collect_created_files_returns_unique_sorted_paths(self) -> None:
        payload = {
            "product_spec_file": "AgentReports/run/01_product_spec.json",
            "phase_results": [
                {
                    "requirement_file": "AgentReports/run/phases/phase_01_requirement.md",
                    "result_file": "AgentReports/run/phases/phase_01_result.json",
                },
                {
                    "requirement_file": "AgentReports/run/phases/phase_01_requirement.md",
                    "review_file": "AgentReports/run/phases/phase_01_review.md",
                },
            ],
            "post_self_heal": {
                "report_file": "AgentReports/run/90_self_heal_report.json",
            },
        }
        files = _collect_created_files(payload)
        self.assertEqual(files, sorted(files))
        self.assertIn("AgentReports/run/01_product_spec.json", files)
        self.assertIn("AgentReports/run/phases/phase_01_requirement.md", files)
        self.assertIn("AgentReports/run/90_self_heal_report.json", files)

    def test_compute_job_progress_for_running_program_phase(self) -> None:
        job = ExecutionJob(
            job_id="job-1",
            job_type="execute_program",
            workspace=Path(".").resolve(),
            payload={"max_phases": 6},
            status="running",
            result={"phase_current": 2, "phase_total": 4, "phase_results": [{}]},
            created_at="2026-02-22T00:00:00+00:00",
        )
        progress = _compute_job_progress(job)
        self.assertGreaterEqual(progress["percent"], 5)
        self.assertEqual(progress["active_hook"], "Phase 2/4")
        self.assertEqual(progress["steps_total"], 5)

    def test_compute_job_progress_for_cancelled_job(self) -> None:
        job = ExecutionJob(
            job_id="job-cancelled",
            job_type="execute_program",
            workspace=Path(".").resolve(),
            payload={"max_phases": 3},
            status="cancelled",
            created_at="2026-02-22T00:00:00+00:00",
        )
        progress = _compute_job_progress(job)
        self.assertEqual(progress["percent"], 100)
        self.assertEqual(progress["active_hook"], "Cancelled")

    def test_build_retry_job_copies_payload(self) -> None:
        previous = ExecutionJob(
            job_id="job-prev",
            job_type="execute_program",
            workspace=Path(".").resolve(),
            payload={"requirement": "Build feature", "meta": {"phase": 1}},
            status="failed",
            created_at="2026-02-22T00:00:00+00:00",
        )
        retry = _build_retry_job(previous=previous)
        self.assertEqual(retry.job_type, previous.job_type)
        self.assertNotEqual(retry.job_id, previous.job_id)
        self.assertEqual(retry.payload["requirement"], "Build feature")
        retry.payload["meta"]["phase"] = 2
        self.assertEqual(previous.payload["meta"]["phase"], 1)

    def test_tail_file_text_returns_last_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "artifact.txt"
            file_path.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
            tail = _tail_file_text(file_path, lines=2)
            self.assertEqual(tail, "line3\nline4")


if __name__ == "__main__":
    unittest.main()
