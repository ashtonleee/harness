"""Tests for read_file with offset/limit line-range support."""

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
        f"test_read_ranges_{tmp_path.name}", SEED_AGENT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_numbered_file(path: Path, n: int = 50) -> None:
    lines = [f"line {i}" for i in range(1, n + 1)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_offset_and_limit(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "data.txt"
    _make_numbered_file(target, 50)
    result = mod.execute_tool("read_file", {
        "path": str(target),
        "offset": 10,
        "limit": 5,
    })
    # Should contain lines 10-14
    assert "10\t" in result
    assert "line 10" in result
    assert "line 14" in result
    # Should NOT contain line 15
    assert "line 15" not in result or "more lines" in result


def test_default_reads_first_200_lines(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "big.txt"
    _make_numbered_file(target, 300)
    result = mod.execute_tool("read_file", {"path": str(target)})
    # First line should be present
    assert "line 1" in result
    # Line 200 should be present
    assert "line 200" in result
    # Should show "more lines" indicator
    assert "more lines" in result


def test_shows_more_lines_when_truncated(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "medium.txt"
    _make_numbered_file(target, 20)
    result = mod.execute_tool("read_file", {
        "path": str(target),
        "offset": 1,
        "limit": 10,
    })
    assert "more lines" in result


def test_small_file_no_truncation(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "small.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")
    result = mod.execute_tool("read_file", {"path": str(target)})
    assert "hello" in result
    assert "world" in result
    assert "more lines" not in result


def test_line_numbers_are_prepended(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    target = tmp_path / "numbered.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = mod.execute_tool("read_file", {"path": str(target)})
    # Lines should have tab-separated line numbers
    assert "1\talpha" in result
    assert "2\tbeta" in result
    assert "3\tgamma" in result
