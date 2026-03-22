"""Tests for the truncate_output helper and shell output metadata."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEED_AGENT_PATH = ROOT / "sandbox" / "seed" / "main.py"


def load_seed_agent(tmp_path: Path):
    os.environ["RSI_AGENT_WORKSPACE"] = str(tmp_path)
    os.environ["LITELLM_URL"] = "http://litellm:4000"
    os.environ["WALLET_URL"] = "http://bridge:8081"
    os.environ["RSI_MODEL"] = "default"
    os.environ["RSI_MAX_TURNS"] = "5"
    spec = importlib.util.spec_from_file_location(
        f"test_truncation_{tmp_path.name}", SEED_AGENT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_short_text_passes_through(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    text = "hello world"
    assert mod.truncate_output(text) == text


def test_long_text_preserves_prefix_and_suffix(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    # Create text longer than 10000 chars
    text = "A" * 5000 + "MIDDLE" + "Z" * 5000
    result = mod.truncate_output(text, max_chars=1000)
    # Should start with A's
    assert result.startswith("A")
    # Should end with Z's
    assert result.endswith("Z")
    # Should contain truncation message
    assert "truncated" in result


def test_truncation_message_includes_char_count(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    text = "x" * 15000
    result = mod.truncate_output(text, max_chars=10000)
    # Dropped = 15000 - 10000 = 5000
    assert "5000 chars" in result


def test_shell_output_includes_exit_code_and_duration(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    result = mod.execute_tool("shell", {"command": "echo hello"})
    assert "exit_code=0" in result
    assert "duration=" in result
    assert "hello" in result


def test_shell_output_shows_nonzero_exit_code(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    result = mod.execute_tool("shell", {"command": "exit 42"})
    assert "exit_code=42" in result


def test_shell_truncated_output_marked(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    # Generate output larger than 10k chars
    result = mod.execute_tool("shell", {"command": "python3 -c \"print('x' * 20000)\""})
    assert "truncated" in result
