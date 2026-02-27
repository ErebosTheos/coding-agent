import asyncio
import time
import os
import shutil
from src.codegen_agent.orchestrator import Orchestrator

async def benchmark():
    workspace = "./benchmark_output"
    if os.path.exists(workspace):
        shutil.rmtree(workspace)
    os.makedirs(workspace)
    
    orchestrator = Orchestrator(workspace)
    
    prompt = "Create a simple calculator UI in Python that can add, subtract, multiply and divide."
    
    print(f"Starting benchmark with prompt: '{prompt}'")
    start_time = time.time()
    
    try:
        report = await orchestrator.run(prompt)
        end_time = time.time()
        
        duration = end_time - start_time
        print(f"Benchmark completed in {duration:.2f} seconds.")
        print(f"QA Score: {report.qa_report.score if report.qa_report else 'N/A'}")
        print(f"Total stages completed: {sum(1 for v in [report.plan, report.architecture, report.execution_result, report.test_suite, report.healing_report, report.qa_report] if v)}")
    except Exception as e:
        print(f"Benchmark failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(benchmark())
