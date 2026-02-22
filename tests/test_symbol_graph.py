import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from senior_agent.symbol_graph import SymbolGraph


class SymbolGraphTests(unittest.TestCase):
    def test_build_graph_tracks_python_symbol_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "a.py").write_text(
                "def shared() -> int:\n"
                "    return 1\n",
                encoding="utf-8",
            )
            (workspace / "b.py").write_text(
                "from a import shared\n"
                "VALUE = shared()\n",
                encoding="utf-8",
            )
            (workspace / "c.py").write_text(
                "def unrelated() -> int:\n"
                "    return 7\n",
                encoding="utf-8",
            )

            graph = SymbolGraph()
            graph.build_graph(workspace)

            dependents = graph.get_dependents(Path("a.py"), "shared")

            self.assertEqual(dependents, [(workspace / "b.py").resolve()])
            symbols = graph.get_defined_symbols(Path("a.py"))
            self.assertIn("shared", symbols)

    def test_build_graph_skips_syntax_errors_and_keeps_valid_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            (workspace / "core.py").write_text(
                "def process() -> int:\n"
                "    return 1\n",
                encoding="utf-8",
            )
            (workspace / "consumer.py").write_text(
                "from core import process\n"
                "VALUE = process()\n",
                encoding="utf-8",
            )
            (workspace / "broken.py").write_text("def invalid(:\n", encoding="utf-8")

            graph = SymbolGraph()
            graph.build_graph(workspace)

            dependents = graph.get_dependents(workspace / "core.py", "process")
            self.assertEqual(dependents, [(workspace / "consumer.py").resolve()])

    def test_build_graph_respects_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            for index in range(5):
                (workspace / f"module_{index}.py").write_text(
                    f"def symbol_{index}() -> int:\n    return {index}\n",
                    encoding="utf-8",
                )

            graph = SymbolGraph(max_files=1)
            graph.build_graph(workspace)

            # Limit should never cause crashes; empty/partial indexing is acceptable.
            dependents = graph.get_dependents(workspace / "module_0.py", "symbol_0")
            self.assertIsInstance(dependents, list)


if __name__ == "__main__":
    unittest.main()
