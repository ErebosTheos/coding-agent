import json
from codegen_agent.models import PipelineReport, Plan, Feature

def test_pipeline_report_serialization():
    features = [Feature(id="f1", title="Test Feature", description="A test feature")]
    plan = Plan(
        project_name="Test Project",
        tech_stack="Python",
        features=features,
        entry_point="main.py",
        test_strategy="pytest"
    )
    report = PipelineReport(prompt="Build a test app", plan=plan)
    
    # Serialize
    d = report.to_dict()
    assert d['prompt'] == "Build a test app"
    assert d['plan']['project_name'] == "Test Project"
    assert len(d['plan']['features']) == 1
    
    # Check JSON compatibility
    json_str = json.dumps(d)
    assert "Test Project" in json_str
