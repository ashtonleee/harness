"""Tests for the edit_file tool in the seed agent."""

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
        f"test_edit_file_{tmp_path.name}", SEED_AGENT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_exact_match_edit_succeeds(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "hello.py"
    target.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    result = mod.execute_tool("edit_file", {
        "path": str(target),
        "old_text": "return 'world'",
        "new_text": "return 'universe'",
    })
    assert result.startswith("OK: edited")
    assert "universe" in target.read_text()
    assert "world" not in target.read_text()


def test_no_match_returns_error(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "hello.py"
    target.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    result = mod.execute_tool("edit_file", {
        "path": str(target),
        "old_text": "return 'mars'",
        "new_text": "return 'venus'",
    })
    assert "ERROR" in result
    assert "not found" in result


def test_multiple_matches_returns_error_with_count(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "dup.py"
    target.write_text("x = 1\nx = 1\nx = 1\n", encoding="utf-8")
    result = mod.execute_tool("edit_file", {
        "path": str(target),
        "old_text": "x = 1",
        "new_text": "x = 2",
    })
    assert "ERROR" in result
    assert "3 times" in result


def test_whitespace_sensitive_matching(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "ws.py"
    target.write_text("    indented\n  less\n", encoding="utf-8")
    # Only matches with exact whitespace
    result = mod.execute_tool("edit_file", {
        "path": str(target),
        "old_text": "indented",  # no leading spaces
        "new_text": "changed",
    })
    # "indented" appears once (without the leading spaces it's still a substring match)
    assert result.startswith("OK: edited")

    target.write_text("    indented\n    indented\n", encoding="utf-8")
    result = mod.execute_tool("edit_file", {
        "path": str(target),
        "old_text": "    indented",
        "new_text": "    changed",
    })
    assert "ERROR" in result
    assert "2 times" in result


def test_file_not_found_returns_error(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    result = mod.execute_tool("edit_file", {
        "path": str(tmp_path / "nonexistent.py"),
        "old_text": "foo",
        "new_text": "bar",
    })
    assert "ERROR" in result
    assert "not found" in result
