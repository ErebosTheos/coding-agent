import json
import os
import asyncio
from dataclasses import asdict
from typing import Optional, Any, Dict
from .models import PipelineReport, Feature, Plan, Architecture, ExecutionNode, Contract, GeneratedFile, ExecutionResult, TestSuite, HealingReport, HealAttempt, CommandResult, FailureType, QAReport, VisualAuditResult

class CheckpointManager:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.checkpoint_dir = os.path.join(workspace, ".codegen_agent")
        self.checkpoint_path = os.path.join(self.checkpoint_dir, "checkpoint.json")

    def save(self, report: PipelineReport):
        """Saves the pipeline report to a JSON file atomically. (Synchronous fallback)"""
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        tmp_path = self.checkpoint_path + ".tmp"
        
        def dumper(obj):
            if isinstance(obj, FailureType):
                return obj.value
            return asdict(obj)

        with open(tmp_path, 'w') as f:
            json.dump(asdict(report), f, separators=(',', ':'), default=dumper)
        
        os.replace(tmp_path, self.checkpoint_path)

    async def asave(self, report: PipelineReport):
        """Saves the pipeline report to a JSON file atomically using a thread."""
        await asyncio.to_thread(self.save, report)

    def load(self) -> Optional[PipelineReport]:
        """Loads the pipeline report from the checkpoint file."""
        if not os.path.exists(self.checkpoint_path):
            return None
        
        with open(self.checkpoint_path, 'r') as f:
            data = json.load(f)
            return self._from_dict(data)

    def _from_dict(self, data: Dict[str, Any]) -> PipelineReport:
        # Reconstruction logic...
        
        def dict_to_plan(d):
            if not d: return None
            features = [Feature(**f) for f in d.get('features', [])]
            return Plan(
                project_name=d['project_name'],
                tech_stack=d['tech_stack'],
                features=features,
                entry_point=d['entry_point'],
                test_strategy=d['test_strategy']
            )

        def dict_to_arch(d):
            if not d: return None
            nodes = []
            for n in d.get('nodes', []):
                contract = Contract(**n['contract']) if n.get('contract') else None
                nodes.append(ExecutionNode(
                    node_id=n['node_id'],
                    file_path=n['file_path'],
                    purpose=n['purpose'],
                    depends_on=n.get('depends_on', []),
                    contract=contract
                ))
            return Architecture(
                file_tree=d['file_tree'],
                nodes=nodes,
                global_validation_commands=data.get('architecture', {}).get('global_validation_commands', []) if 'architecture' in data else []
            )

        def dict_to_exec(d):
            if not d: return None
            gen_files = [GeneratedFile(**f) for f in d.get('generated_files', [])]
            return ExecutionResult(
                generated_files=gen_files,
                skipped_nodes=d.get('skipped_nodes', []),
                failed_nodes=d.get('failed_nodes', [])
            )

        def dict_to_test(d):
            if not d: return None
            return TestSuite(**d)

        def dict_to_heal(d):
            if not d: return None
            attempts = []
            for a in d.get('attempts', []):
                attempts.append(HealAttempt(
                    attempt_number=a['attempt_number'],
                    failure_type=FailureType(a['failure_type']),
                    fix_applied=a['fix_applied'],
                    changed_files=a['changed_files'],
                    note=a.get('note')
                ))
            final_res = CommandResult(**d['final_command_result']) if d.get('final_command_result') else None
            return HealingReport(
                success=d['success'],
                attempts=attempts,
                final_command_result=final_res,
                blocked_reason=d.get('blocked_reason')
            )

        def dict_to_qa(d):
            if not d: return None
            return QAReport(**d)

        def dict_to_visual(d):
            if not d: return None
            return VisualAuditResult(**d)

        return PipelineReport(
            prompt=data['prompt'],
            plan=dict_to_plan(data.get('plan')),
            architecture=dict_to_arch(data.get('architecture')),
            execution_result=dict_to_exec(data.get('execution_result')),
            dependency_resolution=data.get('dependency_resolution'), # Simply load for now
            test_suite=dict_to_test(data.get('test_suite')),
            healing_report=dict_to_heal(data.get('healing_report')),
            qa_report=dict_to_qa(data.get('qa_report')),
            visual_audit=dict_to_visual(data.get('visual_audit')),
            wall_clock_seconds=data.get('wall_clock_seconds', 0.0)
        )
