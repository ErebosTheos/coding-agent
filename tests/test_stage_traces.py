import json
import tempfile

from codegen_agent.models import PipelineReport, StageTrace
from codegen_agent.orchestrator import _role_provider
from codegen_agent.reporter import Reporter


def test_stage_trace_round_trips_through_pipeline_report_to_dict():
    trace = StageTrace(
        stage="qa",
        provider="claude_cli",
        model=None,
        start_monotonic=0.0,
        end_monotonic=2.0,
        duration_seconds=2.0,
        start_unix_ts=0.0,
        end_unix_ts=2.0,
        prompt_chars=0,
        response_chars=0,
    )
    report = PipelineReport(prompt="test", stage_traces=[trace])
    data = report.to_dict()
    assert "stage_traces" in data
    assert data["stage_traces"][0]["stage"] == "qa"
    assert data["stage_traces"][0]["duration_seconds"] == 2.0


def test_pipeline_report_stage_traces_default_empty_list():
    assert PipelineReport(prompt="x").stage_traces == []


def test_role_provider_returns_expected_values_and_unknown_defaults():
    class _FakeRouter:
        config = {"roles": {"planner": {"provider": "gemini_cli", "model": "gemini-2.5-flash"}}}

    prov, mdl = _role_provider(_FakeRouter(), "planner")
    assert prov == "gemini_cli"
    assert mdl == "gemini-2.5-flash"

    prov2, mdl2 = _role_provider(_FakeRouter(), "nonexistent")
    assert prov2 == "unknown"
    assert mdl2 is None


def test_reporter_writes_traces_jsonl():
    with tempfile.TemporaryDirectory() as ws:
        reporter = Reporter(ws)
        trace = StageTrace(
            stage="qa",
            provider="claude_cli",
            model=None,
            start_monotonic=0.0,
            end_monotonic=2.0,
            duration_seconds=2.0,
            start_unix_ts=0.0,
            end_unix_ts=2.0,
            prompt_chars=0,
            response_chars=0,
        )
        report = PipelineReport(prompt="test", stage_traces=[trace])
        reporter.save_report(report)
        with open(f"{ws}/.codegen_agent/traces.jsonl") as handle:
            lines = handle.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["stage"] == "qa"
