import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "export_ai_corpus.py"
SPEC = importlib.util.spec_from_file_location("export_ai_corpus", MODULE_PATH)
export_ai_corpus = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = export_ai_corpus
assert SPEC.loader is not None
SPEC.loader.exec_module(export_ai_corpus)

pytestmark = pytest.mark.fast


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row))
            handle.write("\n")


def test_discover_codex_sessions_filters_by_repo_cwd(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    codex_root = tmp_path / "codex"
    write_jsonl(
        codex_root / "session_index.jsonl",
        [
            {"id": "sid-1", "thread_name": "Relevant thread"},
            {"id": "sid-2", "thread_name": "Other thread"},
        ],
    )
    write_jsonl(
        codex_root / "sessions" / "2026" / "03" / "19" / "relevant.jsonl",
        [
            {
                "timestamp": "2026-03-19T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sid-1", "cwd": str(repo_root), "originator": "Codex Desktop"},
            },
            {
                "timestamp": "2026-03-19T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello repo"}],
                },
            },
        ],
    )
    write_jsonl(
        codex_root / "sessions" / "2026" / "03" / "19" / "other.jsonl",
        [
            {
                "timestamp": "2026-03-19T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sid-2", "cwd": str(tmp_path / "elsewhere")},
            }
        ],
    )

    parse_errors: list[dict] = []
    sessions = export_ai_corpus.discover_codex_sessions(repo_root, codex_root, parse_errors)

    assert parse_errors == []
    assert [session.session_id for session in sessions] == ["sid-1"]
    assert sessions[0].thread_name == "Relevant thread"


def test_discover_claude_sessions_includes_selected_subagents(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    claude_root = tmp_path / "claude"
    project_dir = claude_root / "projects" / "bucket"
    project_dir.mkdir(parents=True)
    (project_dir / "sessions-index.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "sessionId": "claude-top",
                        "summary": "Top summary",
                        "firstPrompt": "First prompt",
                        "projectPath": str(repo_root),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    write_jsonl(
        project_dir / "claude-top.jsonl",
        [
            {
                "timestamp": "2026-03-19T00:00:00Z",
                "type": "user",
                "cwd": str(repo_root),
                "sessionId": "claude-top",
                "gitBranch": "main",
                "message": {"role": "user", "content": "Plan please"},
            }
        ],
    )
    write_jsonl(
        project_dir / "claude-top" / "subagents" / "agent-1.jsonl",
        [
            {
                "timestamp": "2026-03-19T00:00:01Z",
                "type": "assistant",
                "cwd": str(repo_root / ".claude" / "worktrees" / "w1"),
                "sessionId": "claude-top",
                "agentId": "agent-1",
                "slug": "swift-otter",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent"}]},
            }
        ],
    )

    parse_errors: list[dict] = []
    sessions = export_ai_corpus.discover_claude_sessions(repo_root, claude_root, True, parse_errors)

    assert parse_errors == []
    assert [session.session_id for session in sessions] == ["claude-top", "claude-top__agent-1"]
    assert sessions[0].summary == "Top summary"
    assert sessions[1].parent_session_id == "claude-top"
    assert sessions[1].is_subagent is True


def test_normalize_codex_deduplicates_commentary_and_emits_reasoning_placeholder(tmp_path: Path):
    source_path = tmp_path / "codex.jsonl"
    write_jsonl(
        source_path,
        [
            {
                "timestamp": "2026-03-19T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sid-1", "cwd": "/repo"},
            },
            {
                "timestamp": "2026-03-19T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "I am working."}],
                },
            },
            {
                "timestamp": "2026-03-19T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "duplicated user"},
            },
            {
                "timestamp": "2026-03-19T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "I am working."},
            },
            {
                "timestamp": "2026-03-19T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": "{\"cmd\": \"pwd\"}",
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-03-19T00:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "pwd output",
                },
            },
            {
                "timestamp": "2026-03-19T00:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": ["thinking"],
                    "encrypted_content": "secret",
                },
            },
        ],
    )
    session = export_ai_corpus.SessionFile(
        provider="codex",
        source_path=source_path,
        raw_relpath=Path("sessions/codex.jsonl"),
        session_id="sid-1",
        raw_session_id="sid-1",
        parent_session_id=None,
        top_level_session_id="sid-1",
        cwd="/repo",
        git_branch=None,
    )

    out_path = tmp_path / "normalized.jsonl"
    parse_errors: list[dict] = []
    metrics = export_ai_corpus.normalize_session(session, out_path, parse_errors)
    events = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]

    assert parse_errors == []
    assert metrics["normalized_event_count"] == 5
    assert [event["event_kind"] for event in events] == [
        "session_meta",
        "message",
        "tool_call",
        "tool_result",
        "reasoning_encrypted",
    ]
    assert sum(event["event_kind"] == "message" for event in events) == 1
    assert any(event["event_kind"] == "reasoning_encrypted" for event in events)


def test_normalize_claude_links_tool_result_to_tool_use(tmp_path: Path):
    source_path = tmp_path / "claude.jsonl"
    write_jsonl(
        source_path,
        [
            {
                "timestamp": "2026-03-19T00:00:00Z",
                "type": "assistant",
                "cwd": "/repo",
                "sessionId": "claude-top",
                "message": {
                    "role": "assistant",
                    "id": "msg-1",
                    "content": [
                        {"type": "text", "text": "Checking the repo."},
                        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pwd"}},
                    ],
                },
            },
            {
                "timestamp": "2026-03-19T00:00:01Z",
                "type": "user",
                "cwd": "/repo",
                "sessionId": "claude-top",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-1", "content": "pwd output", "is_error": False}
                    ],
                },
            },
        ],
    )
    session = export_ai_corpus.SessionFile(
        provider="claude",
        source_path=source_path,
        raw_relpath=Path("projects/claude.jsonl"),
        session_id="claude-top",
        raw_session_id="claude-top",
        parent_session_id=None,
        top_level_session_id="claude-top",
        cwd="/repo",
        git_branch="main",
    )

    out_path = tmp_path / "normalized.jsonl"
    parse_errors: list[dict] = []
    metrics = export_ai_corpus.normalize_session(session, out_path, parse_errors)
    events = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]

    assert parse_errors == []
    assert metrics["normalized_event_count"] == 3
    assert [event["event_kind"] for event in events] == ["tool_call", "message", "tool_result"]
    assert events[0]["tool_name"] == "Bash"
    assert events[2]["tool_name"] == "Bash"
    assert events[2]["metadata"]["tool_use_id"] == "tool-1"


def test_export_corpus_writes_bundle_and_consistent_indexes(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"

    write_jsonl(codex_root / "session_index.jsonl", [{"id": "sid-1", "thread_name": "Codex thread"}])
    write_jsonl(
        codex_root / "sessions" / "2026" / "03" / "19" / "codex.jsonl",
        [
            {
                "timestamp": "2026-03-19T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sid-1", "cwd": str(repo_root)},
            },
            {
                "timestamp": "2026-03-19T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Codex prompt"}],
                },
            },
        ],
    )

    project_dir = claude_root / "projects" / "bucket"
    project_dir.mkdir(parents=True)
    (project_dir / "sessions-index.json").write_text(
        json.dumps({"entries": [{"sessionId": "claude-top", "summary": "Claude summary"}]}),
        encoding="utf-8",
    )
    write_jsonl(
        project_dir / "claude-top.jsonl",
        [
            {
                "timestamp": "2026-03-19T00:00:00Z",
                "type": "user",
                "cwd": str(repo_root),
                "sessionId": "claude-top",
                "message": {"role": "user", "content": "Claude prompt"},
            }
        ],
    )

    out_dir = tmp_path / "bundle"
    result = export_ai_corpus.export_corpus(
        repo_root,
        out_dir,
        ["codex", "claude"],
        include_raw=True,
        include_subagents=True,
        codex_root=codex_root,
        claude_root=claude_root,
        dry_run=False,
    )

    assert result["parse_errors"] == []
    assert (out_dir / "manifest.json").exists()
    assert (out_dir / "session_index.jsonl").exists()
    assert (out_dir / "session_index.csv").exists()
    assert (out_dir / "README_FOR_REASONING_MODEL.md").exists()
    assert (out_dir / "ANALYSIS_PROMPT.md").exists()
    assert (out_dir / "normalized" / "codex__sid-1.jsonl").exists()
    assert (out_dir / "normalized" / "claude__claude-top.jsonl").exists()
    assert (out_dir / "raw" / "codex" / "sessions" / "2026" / "03" / "19" / "codex.jsonl").exists()
    assert (out_dir / "raw" / "claude" / "projects" / "bucket" / "claude-top.jsonl").exists()

    index_rows = [json.loads(line) for line in (out_dir / "session_index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(index_rows) == 2
    assert {row["provider"] for row in index_rows} == {"codex", "claude"}
    assert {row["normalized_relpath"] for row in index_rows} == {
        "normalized/codex__sid-1.jsonl",
        "normalized/claude__claude-top.jsonl",
    }
    assert any(row["first_user_prompt_preview"] == "Codex prompt" for row in index_rows)
    assert any(row["first_user_prompt_preview"] == "Claude prompt" for row in index_rows)

    with (out_dir / "session_index.csv").open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == 2
