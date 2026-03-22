#!/usr/bin/env python3
"""RSI-Econ session management CLI.

Usage:
    python cli/session.py status           # Current session state
    python cli/session.py pause            # Pause agent
    python cli/session.py resume           # Resume agent
    python cli/session.py new [--name X]   # Reset to seed, new git branch
    python cli/session.py list             # List all session branches
    python cli/session.py fork <branch>    # Fork from another session's state
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib import request as urllib_request


SEED_DIR = Path(__file__).resolve().parents[1] / "sandbox" / "seed"
COMPOSE_FILE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
WALLET_URL = "http://localhost:8081"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=str(SEED_DIR))


def docker_compose(*args: str) -> subprocess.CompletedProcess[str]:
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args])


def cmd_status(_args: argparse.Namespace) -> int:
    # Sandbox running?
    ps = docker_compose("ps", "--format", "json", "sandbox")
    running = False
    if ps.returncode == 0 and ps.stdout.strip():
        try:
            for line in ps.stdout.strip().split("\n"):
                info = json.loads(line)
                state = info.get("State", "")
                running = state == "running"
        except (json.JSONDecodeError, KeyError):
            pass

    # Git info
    branch = git("branch", "--show-current")
    branch_name = branch.stdout.strip() or "(detached)"
    log = git("rev-list", "--count", "HEAD")
    commit_count = log.stdout.strip() or "?"
    last_edit = git("log", "-1", "--format=%ci", "--diff-filter=M", "--", "main.py")
    last_edit_time = last_edit.stdout.strip() or "never"

    # Paused?
    paused_path = SEED_DIR / ".paused"
    paused = paused_path.exists()

    # Wallet
    remaining = "?"
    try:
        req = urllib_request.Request(f"{WALLET_URL}/wallet", method="GET")
        with urllib_request.urlopen(req, timeout=5) as resp:
            wallet = json.loads(resp.read().decode("utf-8"))
            remaining = f"${wallet.get('remaining_usd', 0):.2f}"
    except Exception:
        pass

    state = "paused" if paused else ("running" if running else "stopped")
    print(f"Session:       {branch_name}")
    print(f"State:         {state}")
    print(f"Commits:       {commit_count}")
    print(f"Budget:        {remaining}")
    print(f"Last self-edit: {last_edit_time}")
    return 0


def cmd_pause(_args: argparse.Namespace) -> int:
    result = docker_compose("exec", "sandbox", "touch", "/workspace/agent/.paused")
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print("Agent paused.")
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    r1 = docker_compose("exec", "sandbox", "touch", "/workspace/agent/.resume")
    r2 = docker_compose("exec", "sandbox", "rm", "-f", "/workspace/agent/.paused")
    if r1.returncode != 0:
        print(f"Error: {r1.stderr.strip()}", file=sys.stderr)
        return 1
    if r2.returncode != 0:
        print(f"Warning: {r2.stderr.strip()}", file=sys.stderr)
    print("Agent resumed.")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name or f"session-{int(time.time())}"
    if not name.startswith("session-"):
        name = f"session-{name}"

    # Stop sandbox
    print("Stopping sandbox...")
    docker_compose("stop", "sandbox")

    # Save current branch and create new
    current = git("branch", "--show-current").stdout.strip()
    print(f"Current branch: {current}")

    # Create new branch
    result = git("checkout", "-b", name)
    if result.returncode != 0:
        print(f"Error creating branch: {result.stderr.strip()}", file=sys.stderr)
        return 1

    # Reset to seed state
    git("checkout", "seed", "--", ".")
    git("clean", "-fd")
    git("add", "-A")
    git("commit", "-m", "session start (from seed)")

    # Start sandbox
    print("Starting sandbox...")
    docker_compose("start", "sandbox")
    print(f"New session: {name}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    result = git("branch", "--list", "session-*", "--format=%(refname:short)")
    if result.returncode != 0 or not result.stdout.strip():
        print("No session branches found.")
        return 0

    branches = result.stdout.strip().split("\n")
    for branch in branches:
        info = git("log", "-1", "--format=%h %s (%cr)", branch)
        count = git("rev-list", "--count", branch)
        commits = count.stdout.strip() or "?"
        detail = info.stdout.strip() or ""
        print(f"  {branch}  [{commits} commits]  {detail}")
    return 0


def cmd_fork(args: argparse.Namespace) -> int:
    source = args.branch
    name = args.name or f"fork-{int(time.time())}"
    if not name.startswith("session-"):
        name = f"session-{name}"

    # Verify source branch exists
    check = git("rev-parse", "--verify", source)
    if check.returncode != 0:
        print(f"Error: branch '{source}' not found.", file=sys.stderr)
        return 1

    # Stop sandbox
    print("Stopping sandbox...")
    docker_compose("stop", "sandbox")

    result = git("checkout", "-b", name, source)
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}", file=sys.stderr)
        return 1

    # Start sandbox
    print("Starting sandbox...")
    docker_compose("start", "sandbox")
    print(f"Forked {source} → {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RSI-Econ session management")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current session state")
    sub.add_parser("pause", help="Pause the agent")
    sub.add_parser("resume", help="Resume the agent")

    new_p = sub.add_parser("new", help="Start a new session from seed")
    new_p.add_argument("--name", help="Session name (default: session-<timestamp>)")

    sub.add_parser("list", help="List all session branches")

    fork_p = sub.add_parser("fork", help="Fork from another session")
    fork_p.add_argument("branch", help="Source branch to fork from")
    fork_p.add_argument("--name", help="New session name")

    args = parser.parse_args()
    commands = {
        "status": cmd_status,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "new": cmd_new,
        "list": cmd_list,
        "fork": cmd_fork,
    }

    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
