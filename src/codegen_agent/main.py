import argparse
import asyncio
import os
import sys
from .orchestrator import Orchestrator

async def main_async():
    parser = argparse.ArgumentParser(description="Autonomous Codegen Agent")
    parser.add_argument("--prompt", type=str, help="The user prompt to build an application")
    parser.add_argument("--workspace", type=str, default="./output", help="The workspace directory")
    parser.add_argument("--config", type=str, help="Path to the agent configuration file (JSON/YAML)")
    parser.add_argument("--resume", action="store_true", help="Resume from the last checkpoint")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    
    args = parser.parse_args()
    
    if not args.prompt and not args.resume:
        parser.print_help()
        return

    workspace = os.path.abspath(args.workspace)
    os.makedirs(workspace, exist_ok=True)
    
    orchestrator = Orchestrator(workspace, args.config)
    
    try:
        print(f"Starting codegen agent in {workspace}...")
        report = await orchestrator.run(args.prompt, resume=args.resume)
        print("\n--- PIPELINE COMPLETE ---")
        print(f"Project: {report.plan.project_name if report.plan else 'Unknown'}")
        print(f"QA Score: {report.qa_report.score if report.qa_report else 'N/A'}/100")
        print(f"Report saved to: {os.path.join(workspace, '.codegen_agent', 'report_summary.md')}")
    except Exception as e:
        print(f"\nPIPELINE FAILED: {str(e)}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
