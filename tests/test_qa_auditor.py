import asyncio

from codegen_agent.models import (
    Architecture,
    Contract,
    ExecutionNode,
    ExecutionResult,
    Feature,
    GeneratedFile,
    HealingReport,
    PipelineReport,
    Plan,
    TestSuite as SuiteModel,
)
from codegen_agent.qa_auditor import QAAuditor


class _FakeLLM:
    def __init__(self):
        self.last_prompt = ""

    async def generate(self, prompt: str, system_prompt: str = "") -> str:
        self.last_prompt = prompt
        return '{"score": 90, "issues": [], "suggestions": [], "approved": true}'


def test_qa_auditor_compact_prompt_is_pruned():
    llm = _FakeLLM()
    auditor = QAAuditor(llm)

    nodes = []
    file_tree = []
    for i in range(120):
        file_path = f"src/mod_{i}.py"
        file_tree.append(file_path)
        nodes.append(
            ExecutionNode(
                node_id=f"n{i}",
                file_path=file_path,
                purpose="module",
                contract=Contract(
                    purpose="x" * 200,
                    inputs=["a"] * 10,
                    outputs=["b"] * 10,
                    public_api=["f()"] * 10,
                    invariants=["inv"] * 10,
                ),
            )
        )

    report = PipelineReport(
        prompt="build stuff " * 3000,
        plan=Plan(
            project_name="bigproj",
            tech_stack="python",
            features=[Feature(id=f"f{i}", title=f"Feature {i}", description="d") for i in range(30)],
            entry_point="main.py",
            test_strategy="pytest",
        ),
        architecture=Architecture(
            file_tree=file_tree,
            nodes=nodes,
            global_validation_commands=["pytest tests/"],
        ),
        execution_result=ExecutionResult(
            generated_files=[
                GeneratedFile(file_path="main.py", content="print(1)", node_id="n0", sha256="abc")
            ]
        ),
        healing_report=HealingReport(success=True, attempts=[]),
    )

    qa = asyncio.run(auditor.audit(report))
    assert qa.approved is True
    assert len(llm.last_prompt) <= 28_000

def test_qa_auditor_filters_missing_file_hallucination():
    class _LLM:
        async def generate(self, prompt: str, system_prompt: str = "") -> str:
            return (
                '{"score": 54, "approved": false, "issues": ['
                '"Missing required file `tests/test_stack.py` does not exist"'
                '], "suggestions": []}'
            )

    report = PipelineReport(
        prompt="stack project",
        plan=Plan(
            project_name="stack",
            tech_stack="python",
            features=[Feature(id="f1", title="Stack", description="LIFO stack")],
            entry_point="stack.py",
            test_strategy="pytest",
        ),
        architecture=Architecture(
            file_tree=["stack.py", "tests/test_stack.py"],
            nodes=[],
            global_validation_commands=["pytest tests/"],
        ),
        execution_result=ExecutionResult(
            generated_files=[
                GeneratedFile(file_path="stack.py", content="class Stack: ...", node_id="n1", sha256="x")
            ]
        ),
        test_suite=SuiteModel(
            test_files={"tests/test_stack.py": "def test_stack():\n    assert True\n"},
            validation_commands=["pytest tests/"],
            framework="pytest",
        ),
        healing_report=HealingReport(success=True, attempts=[]),
    )

    qa = asyncio.run(QAAuditor(_LLM()).audit(report))
    assert qa.issues == []
    assert qa.approved is True
    assert qa.score >= 54  # hallucinated issue was filtered; score from LLM preserved


def test_qa_auditor_normalizes_issue_objects_to_strings():
    class _LLM:
        async def generate(self, prompt: str, system_prompt: str = "") -> str:
            return (
                '{"score": 70, "approved": false, "issues": ['
                '{"severity":"high","file":"a.py","issue":"boom"}'
                '], "suggestions": [{"title":"add tests"}]}'
            )

    report = PipelineReport(
        prompt="demo",
        plan=Plan(
            project_name="demo",
            tech_stack="python",
            features=[Feature(id="f1", title="Demo", description="x")],
            entry_point="main.py",
            test_strategy="pytest",
        ),
        architecture=Architecture(file_tree=["main.py"], nodes=[], global_validation_commands=[]),
    )

    qa = asyncio.run(QAAuditor(_LLM()).audit(report))
    assert isinstance(qa.issues, list)
    assert qa.issues and isinstance(qa.issues[0], str)
    assert isinstance(qa.suggestions, list)
