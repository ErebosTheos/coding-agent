import os
import json
import shutil
from codegen_agent.checkpoint import CheckpointManager
from codegen_agent.models import PipelineReport, Plan, Feature

def test_checkpoint_save_load(tmp_path):
    workspace = str(tmp_path)
    manager = CheckpointManager(workspace)
    
    features = [Feature(id="f1", title="Test", description="Desc")]
    plan = Plan(project_name="P1", tech_stack="TS", features=features, entry_point="ep", test_strategy="ts")
    report = PipelineReport(prompt="Test Prompt", plan=plan)
    
    # Save
    manager.save(report)
    assert os.path.exists(os.path.join(workspace, ".codegen_agent", "checkpoint.json"))
    
    # Load
    loaded = manager.load()
    assert loaded is not None
    assert loaded.prompt == "Test Prompt"
    assert loaded.plan.project_name == "P1"
    assert len(loaded.plan.features) == 1
    assert loaded.plan.features[0].id == "f1"

def test_checkpoint_no_file(tmp_path):
    workspace = str(tmp_path)
    manager = CheckpointManager(workspace)
    assert manager.load() is None
