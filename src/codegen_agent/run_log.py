import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RunSummary:
    run_id: str
    timestamp_utc: str
    wall_clock_seconds: float
    heal_attempts: int
    heal_success: bool
    qa_approved: bool
    qa_score: float
    stage_count: int


def append_run_summary(runs_path: str, summary: RunSummary) -> None:
    """Append one JSON line to runs.jsonl (creates file if absent)."""
    p = Path(runs_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(asdict(summary)) + "\n")


def make_run_summary(report) -> RunSummary:
    """Build a RunSummary from a PipelineReport."""
    return RunSummary(
        run_id=str(uuid.uuid4()),
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        wall_clock_seconds=report.wall_clock_seconds,
        heal_attempts=(
            len(report.healing_report.attempts) if report.healing_report else 0
        ),
        heal_success=(
            report.healing_report.success if report.healing_report else False
        ),
        qa_approved=(
            report.qa_report.approved if report.qa_report else False
        ),
        qa_score=(
            report.qa_report.score if report.qa_report else 0.0
        ),
        stage_count=len(report.stage_traces),
    )
