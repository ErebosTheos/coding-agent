import tempfile
import os

from codegen_agent.metrics import MetricWindow, save_baseline, load_baseline, compare


def _make_window(**kwargs) -> MetricWindow:
    defaults = dict(
        run_count=8,
        p50_wall_clock=100.0,
        p90_wall_clock=120.0,
        first_pass_rate=0.75,
        avg_heal_attempts=1.0,
        qa_approval_rate=0.90,
    )
    defaults.update(kwargs)
    return MetricWindow(**defaults)


def test_save_and_load_baseline():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "baseline.json")
        window = _make_window(run_count=10, p50_wall_clock=42.5, first_pass_rate=0.8)
        save_baseline(path, window)
        loaded = load_baseline(path)
        assert loaded is not None
        assert loaded.run_count == 10
        assert loaded.p50_wall_clock == 42.5
        assert loaded.first_pass_rate == 0.8
        assert loaded.qa_approval_rate == 0.90


def test_load_baseline_missing_returns_none():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "does_not_exist.json")
        assert load_baseline(path) is None


def test_compare_green_runtime():
    # baseline p50=100s, current p50=60s → 40% improvement → runtime=Green
    baseline = _make_window(p50_wall_clock=100.0)
    current = _make_window(p50_wall_clock=60.0)
    verdicts = compare(current, baseline)
    assert verdicts["runtime"] == "Green"


def test_compare_red_qa():
    # baseline qa_approval_rate=0.90, current=0.87 → delta=-0.03 → Red
    baseline = _make_window(qa_approval_rate=0.90)
    current = _make_window(qa_approval_rate=0.87)
    verdicts = compare(current, baseline)
    assert verdicts["qa_approval"] == "Red"
