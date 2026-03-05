"""Tests for PatchCache — persistent failure-hash → patch store."""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from codegen_agent.patch_cache import PatchCache, _MAX_ENTRIES
from codegen_agent.healer import Healer
from codegen_agent.models import CommandResult


# ── PatchCache unit tests ─────────────────────────────────────────────────────

def test_miss_returns_none(tmp_path):
    cache = PatchCache(str(tmp_path))
    assert cache.get("nonexistent") is None


def test_put_then_get_roundtrip(tmp_path):
    cache = PatchCache(str(tmp_path))
    patch = {"src/main.py": "def foo(): return 1\n"}
    cache.put("abc123", patch)
    assert cache.get("abc123") == patch


def test_persists_across_instances(tmp_path):
    cache1 = PatchCache(str(tmp_path))
    cache1.put("hashA", {"src/a.py": "content_a"})

    cache2 = PatchCache(str(tmp_path))
    assert cache2.get("hashA") == {"src/a.py": "content_a"}


def test_evicts_oldest_when_full(tmp_path):
    cache = PatchCache(str(tmp_path))
    # Fill to capacity
    for i in range(_MAX_ENTRIES):
        cache.put(f"hash_{i:04d}", {f"f{i}.py": f"content_{i}"})
    assert cache.size == _MAX_ENTRIES
    # Add one more — hash_0000 should be evicted
    cache.put("hash_new", {"new.py": "new"})
    assert cache.size == _MAX_ENTRIES
    assert cache.get("hash_0000") is None
    assert cache.get("hash_new") is not None


def test_put_empty_patch_is_noop(tmp_path):
    cache = PatchCache(str(tmp_path))
    cache.put("hash1", {})
    assert cache.get("hash1") is None
    assert cache.size == 0


def test_update_existing_key_refreshes_order(tmp_path):
    cache = PatchCache(str(tmp_path))
    cache.put("hash_old", {"a.py": "v1"})
    cache.put("hash_new", {"b.py": "v2"})
    # Update hash_old — it should move to end
    cache.put("hash_old", {"a.py": "v2"})
    assert cache.get("hash_old") == {"a.py": "v2"}


def test_cache_file_is_valid_json(tmp_path):
    cache = PatchCache(str(tmp_path))
    cache.put("h1", {"x.py": "y"})
    raw = (tmp_path / ".codegen_agent" / "patch_cache.json").read_text()
    data = json.loads(raw)
    assert "h1" in data


def test_corrupted_cache_file_ignored(tmp_path):
    cache_path = tmp_path / ".codegen_agent" / "patch_cache.json"
    cache_path.parent.mkdir()
    cache_path.write_text("not json")
    cache = PatchCache(str(tmp_path))
    assert cache.get("anything") is None   # graceful degradation


# ── Integration: Healer uses cache ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_healer_stores_patch_after_successful_fix(tmp_path):
    """After a successful LLM fix, the patch must be stored in the cache."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(return_value="def fixed(): return 1\n")
    healer = Healer(llm_client=mock_llm, workspace=str(tmp_path), max_attempts=2)

    (tmp_path / "src.py").write_text("def broken(): pass\n")

    call_count = 0

    def side_effect(cmd, cwd):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return CommandResult(
                command=cmd, exit_code=1, stdout="",
                stderr='File "src.py", line 1\nFAILED test_foo',
            )
        return CommandResult(command=cmd, exit_code=0, stdout="1 passed", stderr="")

    with patch("codegen_agent.healer.run_shell_command", side_effect=side_effect):
        report = await healer.heal(["pytest"])

    assert report.success
    # Cache should now have an entry
    assert healer._patch_cache is not None
    assert healer._patch_cache.size == 1


@pytest.mark.asyncio
async def test_healer_uses_cache_on_second_run(tmp_path):
    """On the second run with the same failure, the cache is hit (no LLM call)."""
    mock_llm = MagicMock()
    # LLM only called once total across two healer instances
    mock_llm.generate = AsyncMock(return_value="def fixed(): return 1\n")

    src = tmp_path / "src.py"
    src.write_text("def broken(): pass\n")

    failure = CommandResult(
        command="pytest", exit_code=1, stdout="",
        stderr='File "src.py", line 1\nFAILED test_foo',
    )
    success = CommandResult(command="pytest", exit_code=0, stdout="1 passed", stderr="")

    call_count = 0

    def side_effect(cmd, cwd):
        nonlocal call_count
        call_count += 1
        return failure if call_count % 2 == 1 else success

    # First healer run: cache miss → LLM fixes → stores patch
    healer1 = Healer(llm_client=mock_llm, workspace=str(tmp_path), max_attempts=2)
    with patch("codegen_agent.healer.run_shell_command", side_effect=side_effect):
        await healer1.heal(["pytest"])

    llm_calls_after_first_run = mock_llm.generate.call_count
    assert llm_calls_after_first_run >= 1

    # Reset source file to broken state for second run
    src.write_text("def broken(): pass\n")
    call_count = 0

    # Second healer: same cache file → should hit cache → LLM not called
    healer2 = Healer(llm_client=mock_llm, workspace=str(tmp_path), max_attempts=2)
    with patch("codegen_agent.healer.run_shell_command", side_effect=side_effect):
        report2 = await healer2.heal(["pytest"])

    assert report2.cache_hits >= 1
    # LLM should not have been called again for the second run
    assert mock_llm.generate.call_count == llm_calls_after_first_run
