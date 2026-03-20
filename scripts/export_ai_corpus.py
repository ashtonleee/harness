#!/usr/bin/env python3
"""Export Codex and Claude conversation logs for a repo into a local corpus bundle."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX_ROOT = Path.home() / ".codex"
CLAUDE_ROOT = Path.home() / ".claude"


@dataclass
class SessionFile:
    provider: str
    source_path: Path
    raw_relpath: Path
    session_id: str
    raw_session_id: str
    parent_session_id: str | None
    top_level_session_id: str
    cwd: str | None
    git_branch: str | None
    thread_name: str | None = None
    summary: str | None = None
    is_subagent: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(ROOT), help="Repo root to scope sessions to.")
    parser.add_argument(
        "--out-dir",
        default=str(default_out_dir()),
        help="Output directory. Defaults to ~/Downloads/rsi-econ-ai-corpus-<timestamp>/",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report the export plan without writing files.")
    parser.add_argument(
        "--sources",
        default="codex,claude",
        help="Comma-separated list of sources to include: codex, claude.",
    )
    parser.add_argument(
        "--include-raw",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Copy raw source logs into the export bundle.",
    )
    parser.add_argument(
        "--include-subagents",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include Claude subagent logs when the parent session is in scope.",
    )
    return parser.parse_args(argv)


def default_out_dir(now: datetime | None = None) -> Path:
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return Path.home() / "Downloads" / f"rsi-econ-ai-corpus-{stamp}"


def normalize_sources(raw_value: str) -> list[str]:
    allowed = {"codex", "claude"}
    sources = [piece.strip().lower() for piece in raw_value.split(",") if piece.strip()]
    if not sources:
        raise SystemExit("No sources requested.")
    unknown = [source for source in sources if source not in allowed]
    if unknown:
        raise SystemExit(f"Unknown sources: {', '.join(sorted(unknown))}")
    seen: list[str] = []
    for source in sources:
        if source not in seen:
            seen.append(source)
    return seen


def discover_sessions(
    repo_root: Path,
    sources: list[str],
    *,
    include_subagents: bool,
    codex_root: Path = CODEX_ROOT,
    claude_root: Path = CLAUDE_ROOT,
) -> tuple[list[SessionFile], list[dict[str, object]]]:
    parse_errors: list[dict[str, object]] = []
    sessions: list[SessionFile] = []
    if "codex" in sources:
        sessions.extend(discover_codex_sessions(repo_root, codex_root, parse_errors))
    if "claude" in sources:
        sessions.extend(discover_claude_sessions(repo_root, claude_root, include_subagents, parse_errors))
    sessions.sort(key=lambda item: (item.provider, str(item.source_path)))
    return sessions, parse_errors


def discover_codex_sessions(
    repo_root: Path,
    codex_root: Path,
    parse_errors: list[dict[str, object]],
) -> list[SessionFile]:
    thread_names = load_codex_thread_names(codex_root)
    candidates: list[SessionFile] = []
    paths = sorted((codex_root / "sessions").glob("**/*.jsonl"))
    paths.extend(sorted((codex_root / "archived_sessions").glob("*.jsonl")))
    for path in paths:
        obj = read_first_json(path, "codex", parse_errors)
        if not obj or obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload", {})
        cwd = payload.get("cwd")
        if not path_matches_repo(cwd, repo_root):
            continue
        raw_session_id = str(payload.get("id") or path.stem)
        candidates.append(
            SessionFile(
                provider="codex",
                source_path=path,
                raw_relpath=path.relative_to(codex_root),
                session_id=raw_session_id,
                raw_session_id=raw_session_id,
                parent_session_id=None,
                top_level_session_id=raw_session_id,
                cwd=cwd,
                git_branch=None,
                thread_name=thread_names.get(raw_session_id),
                summary=None,
                metadata={
                    "originator": payload.get("originator"),
                    "source": payload.get("source"),
                    "model_provider": payload.get("model_provider"),
                    "cli_version": payload.get("cli_version"),
                },
            )
        )
    return candidates


def discover_claude_sessions(
    repo_root: Path,
    claude_root: Path,
    include_subagents: bool,
    parse_errors: list[dict[str, object]],
) -> list[SessionFile]:
    index = load_claude_session_index(claude_root, parse_errors)
    top_level: list[SessionFile] = []
    top_level_ids: set[str] = set()
    top_level_dirs: set[Path] = set()

    for path in sorted((claude_root / "projects").glob("**/*.jsonl")):
        if "/subagents/" in path.as_posix():
            continue
        sample = sample_claude_records(path, parse_errors)
        if not sample:
            continue
        cwd = first_truthy(sample, "cwd")
        if not path_matches_repo(cwd, repo_root):
            continue
        session_id = str(first_truthy(sample, "sessionId") or path.stem)
        meta = index.get(session_id, {})
        top_level.append(
            SessionFile(
                provider="claude",
                source_path=path,
                raw_relpath=path.relative_to(claude_root),
                session_id=session_id,
                raw_session_id=session_id,
                parent_session_id=None,
                top_level_session_id=session_id,
                cwd=cwd,
                git_branch=first_truthy(sample, "gitBranch"),
                thread_name=meta.get("summary"),
                summary=meta.get("summary"),
                metadata={
                    "project_path": meta.get("projectPath"),
                    "first_prompt": meta.get("firstPrompt"),
                },
            )
        )
        top_level_ids.add(session_id)
        top_level_dirs.add(path.parent / session_id)

    if not include_subagents:
        return top_level

    subagents: list[SessionFile] = []
    for path in sorted((claude_root / "projects").glob("**/subagents/*.jsonl")):
        session_dir = path.parents[1]
        top_level_session_id = session_dir.name
        sample = sample_claude_records(path, parse_errors)
        if not sample:
            continue
        cwd = first_truthy(sample, "cwd")
        in_scope = session_dir in top_level_dirs or top_level_session_id in top_level_ids or path_matches_repo(cwd, repo_root)
        if not in_scope:
            continue
        raw_session_id = str(first_truthy(sample, "sessionId") or top_level_session_id)
        agent_id = str(first_truthy(sample, "agentId") or path.stem)
        logical_session_id = f"{raw_session_id}__{agent_id}"
        subagents.append(
            SessionFile(
                provider="claude",
                source_path=path,
                raw_relpath=path.relative_to(claude_root),
                session_id=logical_session_id,
                raw_session_id=raw_session_id,
                parent_session_id=raw_session_id,
                top_level_session_id=top_level_session_id,
                cwd=cwd,
                git_branch=first_truthy(sample, "gitBranch"),
                thread_name=None,
                summary=None,
                is_subagent=True,
                metadata={
                    "agent_id": agent_id,
                    "slug": first_truthy(sample, "slug"),
                },
            )
        )
    return top_level + subagents


def load_codex_thread_names(codex_root: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    path = codex_root / "session_index.jsonl"
    if not path.exists():
        return mapping
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = obj.get("id")
            thread_name = obj.get("thread_name")
            if session_id and thread_name:
                mapping[str(session_id)] = str(thread_name)
    return mapping


def load_claude_session_index(claude_root: Path, parse_errors: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    mapping: dict[str, dict[str, object]] = {}
    for path in sorted((claude_root / "projects").glob("**/sessions-index.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive guard
            add_parse_error(parse_errors, "claude", path, 0, f"sessions-index parse failed: {exc}")
            continue
        for entry in obj.get("entries", []):
            session_id = entry.get("sessionId")
            if session_id:
                mapping[str(session_id)] = entry
    return mapping


def sample_claude_records(path: Path, parse_errors: list[dict[str, object]], limit: int = 20) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no > limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                add_parse_error(parse_errors, "claude", path, line_no, f"JSON decode failed: {exc}")
                break
    return records


def first_truthy(records: list[dict[str, object]], key: str) -> object | None:
    for record in records:
        value = record.get(key)
        if value:
            return value
    return None


def read_first_json(path: Path, provider: str, parse_errors: list[dict[str, object]]) -> dict[str, object] | None:
    line_no = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                return json.loads(line)
    except json.JSONDecodeError as exc:
        add_parse_error(parse_errors, provider, path, line_no, f"JSON decode failed: {exc}")
    except FileNotFoundError as exc:
        add_parse_error(parse_errors, provider, path, 0, str(exc))
    return None


def path_matches_repo(candidate: object, repo_root: Path) -> bool:
    if not candidate or not isinstance(candidate, str):
        return False
    repo = repo_root.resolve()
    try:
        path = Path(candidate).resolve()
    except Exception:
        return False
    return path == repo or repo in path.parents


def add_parse_error(
    parse_errors: list[dict[str, object]],
    provider: str,
    path: Path,
    line_no: int,
    error: str,
) -> None:
    parse_errors.append(
        {
            "provider": provider,
            "source_file": str(path),
            "source_line": line_no,
            "error": error,
        }
    )


def export_corpus(
    repo_root: Path,
    out_dir: Path,
    sources: list[str],
    *,
    include_raw: bool,
    include_subagents: bool,
    codex_root: Path = CODEX_ROOT,
    claude_root: Path = CLAUDE_ROOT,
    dry_run: bool = False,
) -> dict[str, object]:
    sessions, parse_errors = discover_sessions(
        repo_root,
        sources,
        include_subagents=include_subagents,
        codex_root=codex_root,
        claude_root=claude_root,
    )

    if not dry_run and out_dir.exists():
        raise SystemExit(f"Output directory already exists: {out_dir}")

    if not dry_run:
        (out_dir / "normalized").mkdir(parents=True, exist_ok=True)
        if include_raw:
            (out_dir / "raw").mkdir(parents=True, exist_ok=True)

    index_rows: list[dict[str, object]] = []
    totals = {
        "session_count": 0,
        "raw_event_count": 0,
        "normalized_event_count": 0,
        "providers": {},
    }

    for session in sessions:
        normalized_relpath = Path("normalized") / f"{session.provider}__{session.session_id}.jsonl"
        normalized_path = out_dir / normalized_relpath
        metrics = normalize_session(
            session,
            normalized_path if not dry_run else None,
            parse_errors,
        )
        raw_relpath = Path("raw") / session.provider / session.raw_relpath if include_raw else None
        if not dry_run and include_raw and raw_relpath is not None:
            raw_copy_path = out_dir / raw_relpath
            raw_copy_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(session.source_path, raw_copy_path)

        row = {
            "provider": session.provider,
            "session_id": session.session_id,
            "raw_session_id": session.raw_session_id,
            "parent_session_id": session.parent_session_id,
            "top_level_session_id": session.top_level_session_id,
            "source_path": str(session.source_path),
            "raw_relpath": str(raw_relpath) if raw_relpath else None,
            "normalized_relpath": str(normalized_relpath),
            "start_timestamp": metrics["start_timestamp"],
            "end_timestamp": metrics["end_timestamp"],
            "cwd": session.cwd,
            "git_branch": session.git_branch,
            "thread_name": session.thread_name,
            "summary": session.summary,
            "first_user_prompt_preview": metrics["first_user_prompt_preview"],
            "raw_event_count": metrics["raw_event_count"],
            "normalized_event_count": metrics["normalized_event_count"],
            "is_subagent": session.is_subagent,
            "metadata": session.metadata,
        }
        index_rows.append(row)
        totals["session_count"] += 1
        totals["raw_event_count"] += metrics["raw_event_count"]
        totals["normalized_event_count"] += metrics["normalized_event_count"]
        provider_totals = totals["providers"].setdefault(
            session.provider,
            {"session_count": 0, "raw_event_count": 0, "normalized_event_count": 0},
        )
        provider_totals["session_count"] += 1
        provider_totals["raw_event_count"] += metrics["raw_event_count"]
        provider_totals["normalized_event_count"] += metrics["normalized_event_count"]

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo_root": str(repo_root),
        "out_dir": str(out_dir),
        "sources": sources,
        "include_raw": include_raw,
        "include_subagents": include_subagents,
        "dry_run": dry_run,
        "totals": totals,
        "parse_error_count": len(parse_errors),
    }

    if not dry_run:
        write_json(out_dir / "manifest.json", manifest)
        write_jsonl(out_dir / "session_index.jsonl", index_rows)
        write_csv(out_dir / "session_index.csv", index_rows)
        write_text(out_dir / "README_FOR_REASONING_MODEL.md", build_reasoning_readme(manifest, index_rows))
        write_text(out_dir / "ANALYSIS_PROMPT.md", build_analysis_prompt(manifest))
        if parse_errors:
            write_jsonl(out_dir / "parse_errors.jsonl", parse_errors)

    return {
        "manifest": manifest,
        "session_index": index_rows,
        "parse_errors": parse_errors,
    }


def normalize_session(
    session: SessionFile,
    destination: Path | None,
    parse_errors: list[dict[str, object]],
) -> dict[str, object]:
    writer = JsonlWriter(destination)
    metrics = {
        "raw_event_count": 0,
        "normalized_event_count": 0,
        "start_timestamp": None,
        "end_timestamp": None,
        "first_user_prompt_preview": None,
    }
    if session.provider == "codex":
        normalize_codex_session(session, writer, metrics, parse_errors)
    elif session.provider == "claude":
        normalize_claude_session(session, writer, metrics, parse_errors)
    else:  # pragma: no cover - protected by CLI validation
        raise ValueError(f"Unsupported provider: {session.provider}")
    writer.close()
    metrics["normalized_event_count"] = writer.count
    return metrics


class JsonlWriter:
    def __init__(self, path: Path | None):
        self.path = path
        self.handle = None
        self.count = 0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = path.open("w", encoding="utf-8")

    def write(self, record: dict[str, object]) -> None:
        self.count += 1
        if self.handle is None:
            return
        self.handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        self.handle.write("\n")

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()


def normalize_codex_session(
    session: SessionFile,
    writer: JsonlWriter,
    metrics: dict[str, object],
    parse_errors: list[dict[str, object]],
) -> None:
    with session.source_path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            obj = parse_json_line(line, "codex", session.source_path, line_no, parse_errors)
            if obj is None:
                continue
            metrics["raw_event_count"] += 1
            update_time_bounds(metrics, obj.get("timestamp"))
            event_type = obj.get("type")
            payload = obj.get("payload", {})

            if event_type == "session_meta":
                writer.write(
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor="system",
                        event_kind="session_meta",
                        phase=None,
                        tool_name=None,
                        text=None,
                        tool_input=None,
                        tool_output=None,
                        source_line=line_no,
                        metadata={
                            "originator": payload.get("originator"),
                            "source": payload.get("source"),
                            "model_provider": payload.get("model_provider"),
                            "cli_version": payload.get("cli_version"),
                            "thread_name": session.thread_name,
                            "has_base_instructions": bool(payload.get("base_instructions")),
                            "has_developer_instructions": bool(payload.get("developer_instructions")),
                        },
                    )
                )
                continue

            if event_type in {"turn_context", "compacted"}:
                continue

            if event_type == "event_msg":
                inner_type = payload.get("type")
                if inner_type in {"token_count", "agent_message", "user_message"}:
                    continue
                writer.write(
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor="system",
                        event_kind=f"event_msg:{inner_type or 'unknown'}",
                        phase=None,
                        tool_name=None,
                        text=payload.get("message"),
                        tool_input=None,
                        tool_output=None,
                        source_line=line_no,
                        metadata={key: value for key, value in payload.items() if key != "message"},
                    )
                )
                continue

            if event_type != "response_item":
                writer.write(
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor="system",
                        event_kind=str(event_type),
                        phase=None,
                        tool_name=None,
                        text=None,
                        tool_input=None,
                        tool_output=None,
                        source_line=line_no,
                        metadata=payload if isinstance(payload, dict) else {"payload": payload},
                    )
                )
                continue

            inner_type = payload.get("type")
            if inner_type == "message":
                text = extract_codex_message_text(payload.get("content"))
                emit_message_event(
                    writer,
                    metrics,
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor=str(payload.get("role") or "assistant"),
                        event_kind="message",
                        phase=payload.get("phase"),
                        tool_name=None,
                        text=text,
                        tool_input=None,
                        tool_output=None,
                        source_line=line_no,
                        metadata={"content_types": content_types(payload.get("content"))},
                    ),
                )
                continue

            if inner_type in {"function_call", "custom_tool_call"}:
                tool_input = payload.get("arguments") if inner_type == "function_call" else payload.get("input")
                writer.write(
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor="assistant",
                        event_kind="tool_call",
                        phase=None,
                        tool_name=payload.get("name"),
                        text=None,
                        tool_input=maybe_json(tool_input),
                        tool_output=None,
                        source_line=line_no,
                        metadata={"call_id": payload.get("call_id"), "raw_type": inner_type},
                    )
                )
                continue

            if inner_type in {"function_call_output", "custom_tool_call_output"}:
                writer.write(
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor="tool",
                        event_kind="tool_result",
                        phase=None,
                        tool_name=None,
                        text=None,
                        tool_input=None,
                        tool_output=payload.get("output"),
                        source_line=line_no,
                        metadata={"call_id": payload.get("call_id"), "raw_type": inner_type},
                    )
                )
                continue

            if inner_type == "reasoning":
                encrypted = bool(payload.get("encrypted_content"))
                event_kind = "reasoning_encrypted" if encrypted and not payload.get("content") else "reasoning"
                writer.write(
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor="assistant",
                        event_kind=event_kind,
                        phase=None,
                        tool_name=None,
                        text=payload.get("content"),
                        tool_input=None,
                        tool_output=None,
                        source_line=line_no,
                        metadata={
                            "summary": payload.get("summary"),
                            "encrypted": encrypted,
                        },
                    )
                )
                continue

            writer.write(
                make_event(
                    session,
                    timestamp=obj.get("timestamp"),
                    actor="assistant",
                    event_kind=str(inner_type or "response_item"),
                    phase=payload.get("phase"),
                    tool_name=None,
                    text=None,
                    tool_input=None,
                    tool_output=None,
                    source_line=line_no,
                    metadata=payload,
                )
            )


def normalize_claude_session(
    session: SessionFile,
    writer: JsonlWriter,
    metrics: dict[str, object],
    parse_errors: list[dict[str, object]],
) -> None:
    tool_names: dict[str, object] = {}
    with session.source_path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            obj = parse_json_line(line, "claude", session.source_path, line_no, parse_errors)
            if obj is None:
                continue
            metrics["raw_event_count"] += 1
            update_time_bounds(metrics, obj.get("timestamp"))
            event_type = obj.get("type")
            if event_type in {"progress", "queue-operation"}:
                continue

            message = obj.get("message")
            role = event_type
            if isinstance(message, dict):
                role = str(message.get("role") or role)
                content = message.get("content")
            else:
                content = None

            if event_type == "system" and not message:
                continue

            if isinstance(content, str):
                emit_message_event(
                    writer,
                    metrics,
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor=role,
                        event_kind="message",
                        phase=None,
                        tool_name=None,
                        text=content,
                        tool_input=None,
                        tool_output=None,
                        source_line=line_no,
                        metadata={},
                    ),
                )
                continue

            if isinstance(content, list):
                text_parts: list[str] = []
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "text" and item.get("text"):
                        text_parts.append(str(item.get("text")))
                        continue
                    if item_type == "tool_use":
                        tool_id = str(item.get("id") or "")
                        tool_name = item.get("name")
                        if tool_id:
                            tool_names[tool_id] = tool_name
                        writer.write(
                            make_event(
                                session,
                                timestamp=obj.get("timestamp"),
                                actor="assistant",
                                event_kind="tool_call",
                                phase=None,
                                tool_name=tool_name,
                                text=None,
                                tool_input=item.get("input"),
                                tool_output=None,
                                source_line=line_no,
                                metadata={"tool_use_id": tool_id, "message_id": message.get("id")},
                            )
                        )
                        continue
                    if item_type == "tool_result":
                        tool_id = str(item.get("tool_use_id") or "")
                        writer.write(
                            make_event(
                                session,
                                timestamp=obj.get("timestamp"),
                                actor="tool",
                                event_kind="tool_result",
                                phase=None,
                                tool_name=tool_names.get(tool_id),
                                text=None,
                                tool_input=None,
                                tool_output=item.get("content"),
                                source_line=line_no,
                                metadata={
                                    "tool_use_id": tool_id,
                                    "is_error": item.get("is_error", False),
                                },
                            )
                        )
                if text_parts:
                    emit_message_event(
                        writer,
                        metrics,
                        make_event(
                            session,
                            timestamp=obj.get("timestamp"),
                            actor=role,
                            event_kind="message",
                            phase=None,
                            tool_name=None,
                            text="\n\n".join(text_parts),
                            tool_input=None,
                            tool_output=None,
                            source_line=line_no,
                            metadata={"content_types": content_types(content)},
                        ),
                    )
                continue

            if isinstance(message, dict) and message.get("content") is None and event_type in {"user", "assistant", "system"}:
                writer.write(
                    make_event(
                        session,
                        timestamp=obj.get("timestamp"),
                        actor=role,
                        event_kind="message",
                        phase=None,
                        tool_name=None,
                        text=None,
                        tool_input=None,
                        tool_output=None,
                        source_line=line_no,
                        metadata={"message_id": message.get("id"), "empty_content": True},
                    )
                )


def emit_message_event(writer: JsonlWriter, metrics: dict[str, object], event: dict[str, object]) -> None:
    writer.write(event)
    if event["actor"] == "user" and event["text"] and metrics["first_user_prompt_preview"] is None:
        metrics["first_user_prompt_preview"] = preview_text(str(event["text"]))


def make_event(
    session: SessionFile,
    *,
    timestamp: object,
    actor: str,
    event_kind: str,
    phase: object,
    tool_name: object,
    text: object,
    tool_input: object,
    tool_output: object,
    source_line: int,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "provider": session.provider,
        "session_id": session.session_id,
        "parent_session_id": session.parent_session_id,
        "top_level_session_id": session.top_level_session_id,
        "timestamp": timestamp,
        "actor": actor,
        "event_kind": event_kind,
        "phase": phase,
        "tool_name": tool_name,
        "text": text,
        "tool_input": tool_input,
        "tool_output": tool_output,
        "cwd": session.cwd,
        "git_branch": session.git_branch,
        "source_file": str(session.source_path),
        "source_line": source_line,
        "metadata": metadata or {},
    }


def parse_json_line(
    line: str,
    provider: str,
    source_path: Path,
    line_no: int,
    parse_errors: list[dict[str, object]],
) -> dict[str, object] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        add_parse_error(parse_errors, provider, source_path, line_no, f"JSON decode failed: {exc}")
        return None
    if not isinstance(obj, dict):
        add_parse_error(parse_errors, provider, source_path, line_no, "Line did not decode to an object")
        return None
    return obj


def update_time_bounds(metrics: dict[str, object], timestamp: object) -> None:
    if not timestamp or not isinstance(timestamp, str):
        return
    if metrics["start_timestamp"] is None:
        metrics["start_timestamp"] = timestamp
    metrics["end_timestamp"] = timestamp


def extract_codex_message_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n\n".join(parts) if parts else None


def content_types(content: object) -> list[str]:
    if not isinstance(content, list):
        return []
    values: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            values.append(str(item["type"]))
    return values


def maybe_json(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def preview_text(text: str, limit: int = 200) -> str:
    compact = " ".join(text.split())
    return compact[: limit - 3] + "..." if len(compact) > limit else compact


def write_json(path: Path, obj: dict[str, object]) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "provider",
        "session_id",
        "raw_session_id",
        "parent_session_id",
        "top_level_session_id",
        "source_path",
        "raw_relpath",
        "normalized_relpath",
        "start_timestamp",
        "end_timestamp",
        "cwd",
        "git_branch",
        "thread_name",
        "summary",
        "first_user_prompt_preview",
        "raw_event_count",
        "normalized_event_count",
        "is_subagent",
        "metadata",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            csv_row = row.copy()
            csv_row["metadata"] = json.dumps(csv_row["metadata"], ensure_ascii=False, sort_keys=True)
            writer.writerow(csv_row)


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def build_reasoning_readme(manifest: dict[str, object], rows: list[dict[str, object]]) -> str:
    providers = manifest["totals"]["providers"]
    lines = [
        "# RSI Econ AI Corpus",
        "",
        "This bundle contains local Codex and Claude history scoped to `/Users/ashton/code/rsi-econ`.",
        "",
        "## Read Order",
        "",
        "1. `manifest.json` for counts and export settings.",
        "2. `session_index.jsonl` or `session_index.csv` to choose sessions.",
        "3. `normalized/*.jsonl` for cross-tool analysis.",
        "4. `raw/` only when you need full-fidelity source records.",
        "",
        "## Blind Spots",
        "",
        "- Codex encrypted reasoning is represented as placeholder events only.",
        "- Low-signal operational noise was dropped from normalized files but preserved in raw copies.",
        "- Session scope is determined from local path metadata only, not from message text.",
        "",
        "## Totals",
        "",
        f"- Sessions: {manifest['totals']['session_count']}",
        f"- Raw events: {manifest['totals']['raw_event_count']}",
        f"- Normalized events: {manifest['totals']['normalized_event_count']}",
        f"- Parse errors: {manifest['parse_error_count']}",
        "",
        "## Providers",
        "",
    ]
    for provider, counts in sorted(providers.items()):
        lines.append(
            f"- {provider}: {counts['session_count']} sessions, "
            f"{counts['raw_event_count']} raw events, {counts['normalized_event_count']} normalized events"
        )
    if rows:
        lines.extend(
            [
                "",
                "## Fastest Starting Points",
                "",
            ]
        )
        for row in rows[:5]:
            title = row.get("thread_name") or row.get("summary") or row.get("session_id")
            lines.append(f"- {title}: `{row['normalized_relpath']}`")
    lines.append("")
    return "\n".join(lines)


def build_analysis_prompt(manifest: dict[str, object]) -> str:
    return "\n".join(
        [
            "# Analysis Prompt",
            "",
            "You are auditing an AI-assisted development workflow using local conversation history.",
            "",
            "Use this corpus to answer:",
            "- Where does the user loop between models without gaining certainty?",
            "- Where does context get recopied instead of referenced once?",
            "- Which prompts repeatedly re-orient the model to the same repo facts?",
            "- Which interaction patterns create confidence without verification, or verification without decision closure?",
            "- What operating protocol would reduce wasted turns, duplicated work, and cross-model drift?",
            "",
            "Constraints:",
            f"- Scope only the exported `rsi-econ` corpus with {manifest['totals']['session_count']} session files.",
            "- Treat `normalized/*.jsonl` as the primary dataset and consult `raw/` only when needed.",
            "- Note that Codex internal reasoning is unavailable except for explicit encrypted placeholders.",
            "",
            "Output format:",
            "1. A concise diagnosis of the main workflow failure modes.",
            "2. Evidence-backed examples with session ids or file paths.",
            "3. A concrete improved operating protocol for future work.",
            "4. A short list of prompts/templates to reuse.",
            "5. Any instrumentation or export improvements that would make the next audit stronger.",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    sources = normalize_sources(args.sources)
    result = export_corpus(
        repo_root,
        out_dir,
        sources,
        include_raw=args.include_raw,
        include_subagents=args.include_subagents,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "out_dir": str(out_dir),
                "dry_run": args.dry_run,
                "sources": sources,
                "session_count": result["manifest"]["totals"]["session_count"],
                "provider_counts": {
                    provider: counts["session_count"]
                    for provider, counts in result["manifest"]["totals"]["providers"].items()
                },
                "raw_event_count": result["manifest"]["totals"]["raw_event_count"],
                "normalized_event_count": result["manifest"]["totals"]["normalized_event_count"],
                "parse_error_count": result["manifest"]["parse_error_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
