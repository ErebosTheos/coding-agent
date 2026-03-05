"""Tests for ValidationCommandGuard — inference of validation commands."""
from codegen_agent.orchestrator import _infer_validation_commands
from codegen_agent.models import GeneratedFile


def _gf(path: str) -> GeneratedFile:
    return GeneratedFile(file_path=path, content="", node_id="n", sha256="x")


def test_infers_pytest_for_python():
    files = [_gf("src/main.py"), _gf("tests/test_main.py")]
    cmds = _infer_validation_commands(files)
    assert any("pytest" in c for c in cmds)


def test_infers_npm_test_when_package_json_present():
    files = [_gf("package.json"), _gf("src/index.ts")]
    cmds = _infer_validation_commands(files)
    assert any("npm" in c for c in cmds)


def test_infers_node_test_without_package_json():
    files = [_gf("src/index.js")]
    cmds = _infer_validation_commands(files)
    assert any("node" in c for c in cmds)


def test_infers_go_test():
    files = [_gf("main.go"), _gf("utils/helper.go")]
    cmds = _infer_validation_commands(files)
    assert any("go test" in c for c in cmds)


def test_infers_cargo_test_for_rust():
    files = [_gf("src/main.rs")]
    cmds = _infer_validation_commands(files)
    assert any("cargo test" in c for c in cmds)


def test_returns_empty_for_unknown_stack():
    files = [_gf("README.md"), _gf("data.csv")]
    cmds = _infer_validation_commands(files)
    assert cmds == []


def test_mixed_stack_gets_multiple_commands():
    files = [_gf("server.py"), _gf("client/index.ts"), _gf("package.json")]
    cmds = _infer_validation_commands(files)
    assert len(cmds) >= 2
