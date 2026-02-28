import dataclasses
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class MetricWindow:
    run_count: int
    p50_wall_clock: float       # seconds
    p90_wall_clock: float       # seconds
    first_pass_rate: float      # 0.0–1.0; runs where heal_attempts == 0
    avg_heal_attempts: float
    qa_approval_rate: float     # 0.0–1.0


class RollingMetrics:
    """Compute rolling-window metrics from a runs.jsonl file.

    Reads the last `window` entries (default 20) matching §7.2 policy.
    Returns None when no data is available.
    """

    def __init__(self, runs_path: str):
        self._path = Path(runs_path)

    def _load_runs(self) -> list[dict]:
        if not self._path.exists():
            return []
        runs = []
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return runs

    def compute(self, window: int = 20) -> Optional[MetricWindow]:
        """Return a MetricWindow for the last `window` runs, or None if no data."""
        runs = self._load_runs()
        recent = runs[-window:]
        if not recent:
            return None

        n = len(recent)
        wall_clocks = sorted(r["wall_clock_seconds"] for r in recent)

        p50 = statistics.median(wall_clocks)
        p90_idx = max(0, int(n * 0.9) - 1) if n > 1 else 0
        p90 = wall_clocks[p90_idx]

        first_pass = sum(1 for r in recent if r["heal_attempts"] == 0) / n
        avg_heal = sum(r["heal_attempts"] for r in recent) / n
        qa_rate = sum(1 for r in recent if r["qa_approved"]) / n

        return MetricWindow(
            run_count=n,
            p50_wall_clock=p50,
            p90_wall_clock=p90,
            first_pass_rate=first_pass,
            avg_heal_attempts=avg_heal,
            qa_approval_rate=qa_rate,
        )


def save_baseline(baseline_path: str, window: MetricWindow) -> None:
    """Persist a MetricWindow as the comparison baseline."""
    p = Path(baseline_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dataclasses.asdict(window)))


def load_baseline(baseline_path: str) -> Optional[MetricWindow]:
    """Load a saved baseline, or return None if not found or corrupt."""
    p = Path(baseline_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return MetricWindow(**data)
    except Exception:
        return None


# §7.2 metric bands
def _band(value: float, green: float, amber: float, higher_is_better: bool = True) -> str:
    """Return Green / Amber / Red based on §7.2 thresholds."""
    if higher_is_better:
        if value >= green:
            return "Green"
        if value >= amber:
            return "Amber"
        return "Red"
    else:
        # lower is better (e.g. wall clock, heal attempts)
        if value <= green:
            return "Green"
        if value <= amber:
            return "Amber"
        return "Red"


def compare(current: MetricWindow, baseline: MetricWindow) -> dict[str, str]:
    """Return Green/Amber/Red verdict per §7.2 metric band for each metric.

    Bands (§7.2):
    - Runtime improvement: Green >=35%, Amber 20–35%, Red <20%
    - First-pass rate improvement: Green >=+20pp, Amber +5pp to +20pp, Red < -3pp
    - Heal attempt reduction: Green >=30%, Amber 15–30%, Red <15%
    - QA approval: Red only if drops >2pp from baseline
    """
    verdicts: dict[str, str] = {}

    # Runtime: improvement = (baseline - current) / baseline
    if baseline.p50_wall_clock > 0:
        improvement = (baseline.p50_wall_clock - current.p50_wall_clock) / baseline.p50_wall_clock
        verdicts["runtime"] = _band(improvement, green=0.35, amber=0.20, higher_is_better=True)
    else:
        verdicts["runtime"] = "N/A"

    # First-pass rate: improvement in percentage points
    fp_delta = current.first_pass_rate - baseline.first_pass_rate
    if fp_delta >= 0.20:
        verdicts["first_pass"] = "Green"
    elif fp_delta >= 0.05:
        verdicts["first_pass"] = "Amber"
    elif fp_delta < -0.03:
        verdicts["first_pass"] = "Red"
    else:
        verdicts["first_pass"] = "Amber"  # between -3pp and +5pp

    # Heal attempts: reduction = (baseline - current) / baseline
    if baseline.avg_heal_attempts > 0:
        reduction = (baseline.avg_heal_attempts - current.avg_heal_attempts) / baseline.avg_heal_attempts
        verdicts["heal_attempts"] = _band(reduction, green=0.30, amber=0.15, higher_is_better=True)
    else:
        verdicts["heal_attempts"] = "Green"   # already zero

    # QA approval: Red only if drops >2pp
    qa_delta = current.qa_approval_rate - baseline.qa_approval_rate
    verdicts["qa_approval"] = "Red" if qa_delta < -0.02 else "Green"

    return verdicts
