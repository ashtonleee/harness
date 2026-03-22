"""Tests for context compaction logic."""

from __future__ import annotations

import importlib.util
import json
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
        f"test_compaction_{tmp_path.name}", SEED_AGENT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_estimate_tokens(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    messages = [{"role": "user", "content": "x" * 400}]
    tokens = mod.estimate_tokens(messages)
    # 400 chars content + json overhead ≈ ~110 tokens
    assert tokens > 50


def test_extract_findings_urls(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    messages = [
        {"role": "tool", "content": "Found https://example.com/api and https://test.org/free"},
        {"role": "assistant", "content": "I found two URLs"},
    ]
    findings = mod.extract_findings(messages)
    urls = [f for f in findings if f.startswith("URL:")]
    assert len(urls) == 2
    assert "URL: https://example.com/api" in findings


def test_extract_findings_providers(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    messages = [
        {"role": "tool", "content": "OpenAI offers GPT-4. Anthropic has Claude. Groq is fast."},
    ]
    findings = mod.extract_findings(messages)
    providers = [f for f in findings if f.startswith("Provider:")]
    assert len(providers) == 3


def test_extract_findings_prices(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    messages = [
        {"role": "tool", "content": "GPT-4 costs $0.03/1k tokens. Claude costs $0.01/1k."},
    ]
    findings = mod.extract_findings(messages)
    prices = [f for f in findings if f.startswith("Price:")]
    assert len(prices) == 2


def test_compact_context_reduces_to_two_messages(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    knowledge = mod.load_knowledge()
    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "Do something"},
        {"role": "assistant", "content": "OK"},
        {"role": "tool", "content": "Found https://api.groq.com free tier"},
        {"role": "assistant", "content": "Great"},
    ] * 10  # 50 messages
    # Keep first message as system
    messages[0] = {"role": "system", "content": "You are an agent."}
    result = mod.compact_context(messages, knowledge)
    assert len(result) == 2
    assert result[0]["role"] == "system"
    assert "CONTEXT COMPACTED" in result[1]["content"]


def test_compact_context_saves_to_knowledge_json(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    knowledge = mod.load_knowledge()
    messages = [
        {"role": "system", "content": "You are an agent."},
        {"role": "tool", "content": "https://openrouter.ai free access. Anthropic pricing $0.01"},
    ]
    mod.compact_context(messages, knowledge)
    # Knowledge should be saved to disk
    k_path = tmp_path / "knowledge.json"
    assert k_path.exists()
    saved = json.loads(k_path.read_text())
    assert any("openrouter" in f.lower() for f in saved["findings"])


def test_knowledge_loaded_at_startup(tmp_path: Path) -> None:
    mod = load_seed_agent(tmp_path)
    # Pre-populate knowledge.json
    k_path = tmp_path / "knowledge.json"
    k_path.write_text(json.dumps({
        "version": 2,
        "restarts": 3,
        "findings": ["URL: https://saved.example.com", "Provider: groq"],
        "providers_checked": [],
        "free_tiers_found": [],
        "proposals_submitted": [],
        "domains_accessible": [],
        "domains_blocked": [],
    }))
    knowledge = mod.load_knowledge()
    assert knowledge["restarts"] == 3
    assert "URL: https://saved.example.com" in knowledge["findings"]
