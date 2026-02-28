import json
import tempfile
import os

from codegen_agent.run_log import RunSummary, append_run_summary, make_run_summary
from codegen_agent.metrics import RollingMetrics


def test_run_summary_serializes_correctly():
    with tempfile.TemporaryDirectory() as d:
        runs_path = os.path.join(d, "runs.jsonl")
        summary = RunSummary(
            run_id="run-1",
            timestamp_utc="2026-02-27T00:00:00+00:00",
            wall_clock_seconds=12.5,
            heal_attempts=2,
            heal_success=True,
            qa_approved=True,
            qa_score=93.0,
            stage_count=6,
        )
        append_run_summary(runs_path, summary)
        with open(runs_path, "r") as f:
            line = f.read().strip()
        data = json.loads(line)
        assert data["run_id"] == "run-1"
        assert data["wall_clock_seconds"] == 12.5
        assert data["heal_attempts"] == 2

        class _StubReport:
            wall_clock_seconds = 1.0
            healing_report = None
            qa_report = None
            stage_traces = []

        generated = make_run_summary(_StubReport())
        assert generated.heal_attempts == 0


def test_rolling_metrics_returns_none_when_no_file():
    with tempfile.TemporaryDirectory() as d:
        runs_path = os.path.join(d, "missing.jsonl")
        assert RollingMetrics(runs_path).compute() is None


def test_rolling_metrics_single_run():
    with tempfile.TemporaryDirectory() as d:
        runs_path = os.path.join(d, "runs.jsonl")
        append_run_summary(
            runs_path,
            RunSummary(
                run_id="run-1",
                timestamp_utc="2026-02-27T00:00:00+00:00",
                wall_clock_seconds=10.0,
                heal_attempts=0,
                heal_success=True,
                qa_approved=True,
                qa_score=100.0,
                stage_count=6,
            ),
        )
        metrics = RollingMetrics(runs_path).compute()
        assert metrics is not None
        assert metrics.run_count == 1
        assert metrics.first_pass_rate == 1.0
        assert metrics.qa_approval_rate == 1.0


def test_rolling_metrics_first_pass_rate():
    with tempfile.TemporaryDirectory() as d:
        runs_path = os.path.join(d, "runs.jsonl")
        for i, heals in enumerate([0, 0, 0, 2], start=1):
            append_run_summary(
                runs_path,
                RunSummary(
                    run_id=f"run-{i}",
                    timestamp_utc=f"2026-02-27T00:00:0{i}+00:00",
                    wall_clock_seconds=10.0 + i,
                    heal_attempts=heals,
                    heal_success=heals == 0,
                    qa_approved=True,
                    qa_score=90.0,
                    stage_count=6,
                ),
            )
        metrics = RollingMetrics(runs_path).compute()
        assert metrics is not None
        assert metrics.first_pass_rate == 0.75


def test_rolling_metrics_p50():
    with tempfile.TemporaryDirectory() as d:
        runs_path = os.path.join(d, "runs.jsonl")
        for i, wall in enumerate([10, 20, 30, 40, 50], start=1):
            append_run_summary(
                runs_path,
                RunSummary(
                    run_id=f"run-{i}",
                    timestamp_utc=f"2026-02-27T00:00:0{i}+00:00",
                    wall_clock_seconds=float(wall),
                    heal_attempts=0,
                    heal_success=True,
                    qa_approved=True,
                    qa_score=95.0,
                    stage_count=6,
                ),
            )
        metrics = RollingMetrics(runs_path).compute()
        assert metrics is not None
        assert metrics.p50_wall_clock == 30.0


def test_rolling_metrics_window_limits_to_last_n():
    with tempfile.TemporaryDirectory() as d:
        runs_path = os.path.join(d, "runs.jsonl")
        for i in range(1, 26):
            append_run_summary(
                runs_path,
                RunSummary(
                    run_id=f"run-{i}",
                    timestamp_utc=f"2026-02-27T00:00:{i:02d}+00:00",
                    wall_clock_seconds=10.0 + i,
                    heal_attempts=0,
                    heal_success=True,
                    qa_approved=i > 5,
                    qa_score=95.0,
                    stage_count=6,
                ),
            )
        metrics = RollingMetrics(runs_path).compute(window=20)
        assert metrics is not None
        assert metrics.qa_approval_rate == 1.0
