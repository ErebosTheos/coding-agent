from codegen_agent.healer import _truncate_error_output, _cap_file_content
from codegen_agent.utils import prune_prompt


def test_truncate_error_output_short_unchanged():
    text = "line\n" * 10
    assert _truncate_error_output(text) == text


def test_truncate_error_output_long_tail_preserved():
    lines = [f"line{i}" for i in range(200)]
    result = _truncate_error_output("\n".join(lines), max_lines=10)
    assert "truncated" in result
    assert "line199" in result
    assert "line0" not in result


def test_cap_file_content_short_unchanged():
    short = "x" * 100
    assert _cap_file_content(short) == short


def test_cap_file_content_long_tail_preserved():
    big = "a" * 20_000
    result = _cap_file_content(big, max_chars=8_000)
    assert len(result) < 20_000
    assert result.endswith("a" * 100)


def test_prune_prompt_fires_as_safety_net():
    big_prompt = "x" * 40_000
    result = prune_prompt(big_prompt, max_chars=16_000)
    assert len(result) <= 16_000
