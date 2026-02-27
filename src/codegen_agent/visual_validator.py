import os
import json
import base64
from .models import VisualAuditResult
from .llm.protocol import LLMClient
from .utils import extract_code_from_markdown

VISUAL_SYSTEM_PROMPT = """You are a Visual QA reviewer. Evaluate screenshot compliance with the project requirements.
Respond with JSON only using this schema:
{"passed": <true|false>, "visual_bugs": ["..."], "suggested_css_fixes": "...", "rationale": "..."}"""

VISUAL_USER_PROMPT_TEMPLATE = """Project Plan: {plan}
Screenshot Base64 PNG:
{encoded_image}

Audit the visual quality and compliance of this screenshot."""

class VisualValidator:
    def __init__(self, llm_client: LLMClient, workspace: str):
        self.llm_client = llm_client
        self.workspace = workspace

    async def validate(self, plan_summary: str, entry_point: str) -> VisualAuditResult:
        """Performs a visual audit of the project (if applicable)."""
        # This stage is optional and requires playwright
        full_path = os.path.join(self.workspace, entry_point)
        if not entry_point.endswith('.html') or not os.path.exists(full_path):
            return VisualAuditResult(passed=True, visual_bugs=[], suggested_css_fixes="N/A - Not a web project")

        screenshot_path = os.path.join(self.workspace, ".codegen_agent", "visual_snapshot.png")
        os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        
        try:
            # Attempt to capture screenshot using playwright
            await self._capture_screenshot(f"file://{full_path}", screenshot_path)
        except Exception as e:
            return VisualAuditResult(
                passed=False, 
                visual_bugs=[f"Failed to capture screenshot: {str(e)}"], 
                suggested_css_fixes=""
            )

        with open(screenshot_path, 'rb') as f:
            encoded_image = base64.b64encode(f.read()).decode("ascii")

        prompt = VISUAL_USER_PROMPT_TEMPLATE.format(
            plan=plan_summary,
            encoded_image=encoded_image
        )
        
        try:
            response = await self.llm_client.generate(prompt, system_prompt=VISUAL_SYSTEM_PROMPT)
            json_blocks = extract_code_from_markdown(response, "json")
            if not json_blocks:
                data = json.loads(response)
            else:
                data = json.loads(json_blocks[0])
            
            return VisualAuditResult(
                passed=data.get('passed', False),
                visual_bugs=data.get('visual_bugs', []),
                suggested_css_fixes=data.get('suggested_css_fixes', ""),
                screenshot_path=screenshot_path
            )
        except Exception as e:
            return VisualAuditResult(passed=False, visual_bugs=[f"LLM Audit failed: {str(e)}"], suggested_css_fixes="")

    async def _capture_screenshot(self, url: str, destination: str):
        """Captures a screenshot using playwright (if installed)."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError("playwright is not installed. Please run 'pip install playwright' and 'playwright install chromium'")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(url)
            await page.wait_for_load_state("domcontentloaded")
            await page.screenshot(path=destination, full_page=True)
            await browser.close()
