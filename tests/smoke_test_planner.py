import asyncio
from codegen_agent.planner import Planner
from codegen_agent.llm.gemini_cli import GeminiCLIClient

async def smoke_test_planner():
    # Use Gemini CLI for planning
    client = GeminiCLIClient()
    planner = Planner(client)
    
    prompt = "Build a simple Python calculator CLI app"
    print(f"Planning for: {prompt}")
    
    try:
        plan = await planner.plan(prompt)
        print("\n--- PLAN GENERATED ---")
        print(f"Project Name: {plan.project_name}")
        print(f"Tech Stack: {plan.tech_stack}")
        print(f"Entry Point: {plan.entry_point}")
        print("Features:")
        for f in plan.features:
            print(f"- {f.title}: {f.description}")
    except Exception as e:
        print(f"Planning failed: {e}")

if __name__ == "__main__":
    asyncio.run(smoke_test_planner())
