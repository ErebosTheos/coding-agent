import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.style_mimic import StyleMimic


class StyleMimicTests(unittest.TestCase):
    def test_infers_python_fastapi_style(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            source_dir = workspace / "src"
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "api.py").write_text(
                "from fastapi import FastAPI\n\n"
                "app = FastAPI()\n\n"
                "def fetch_user_name(user_id: int) -> str:\n"
                "    user_name = \"demo\"\n"
                "    if user_id < 0:\n"
                "        return \"\"\n"
                "    return user_name\n",
                encoding="utf-8",
            )

            summary = StyleMimic().infer_project_style(workspace)

            self.assertIn("4-space indentation", summary)
            self.assertIn("snake_case names", summary)
            self.assertIn("double quotes", summary)
            self.assertIn("FastAPI patterns", summary)

    def test_infers_react_style_for_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            source_dir = workspace / "src"
            source_dir.mkdir(parents=True, exist_ok=True)
            (workspace / "package.json").write_text(
                '{"dependencies":{"react":"^18.0.0"}}',
                encoding="utf-8",
            )
            (source_dir / "App.jsx").write_text(
                "import React from 'react';\n\n"
                "export default function App() {\n"
                "  const handleClick = () => {\n"
                "    const localValue = 'ok';\n"
                "    return localValue;\n"
                "  };\n"
                "  return <button onClick={handleClick}>Run</button>;\n"
                "}\n",
                encoding="utf-8",
            )

            summary = StyleMimic().infer_project_style(workspace)

            self.assertIn("2-space indentation", summary)
            self.assertIn("camelCase names", summary)
            self.assertIn("single quotes", summary)
            self.assertIn("React patterns", summary)

    def test_falls_back_when_workspace_is_missing(self) -> None:
        missing_path = Path("/tmp/this-style-mimic-path-should-not-exist-12345")

        summary = StyleMimic().infer_project_style(missing_path)

        self.assertEqual(summary, "Style: preserve existing conventions.")


if __name__ == "__main__":
    unittest.main()
