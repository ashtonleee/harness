from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, TextIO
from urllib import error as urllib_error
from urllib import request as urllib_request


DEFAULT_BRIDGE_URL = os.getenv("RSI_BRIDGE_URL", "http://localhost:8081")


class BridgeError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def bridge_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    base_url: str = DEFAULT_BRIDGE_URL,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"content-type": "application/json"} if payload is not None else {}
    req = urllib_request.Request(
        f"{base_url}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise BridgeError(exc.code, detail) from exc


def cmd_list(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    proposals = bridge_request("/proposals", base_url=args.bridge_url)
    if not proposals:
        print("No proposals.", file=out)
        return 0
    for p in proposals:
        pid = p.get("id", "?")
        status = p.get("status", "?")
        method = p.get("method", "?")
        url = p.get("url", "?")
        print(f"  [{pid}] {status}  {method} {url}", file=out)
    return 0


def cmd_approve(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    result = bridge_request(
        f"/proposals/{args.id}/approve",
        method="POST",
        base_url=args.bridge_url,
    )
    print(f"Approved: {result}", file=out)
    return 0


def cmd_deny(args: argparse.Namespace, out: TextIO = sys.stdout) -> int:
    result = bridge_request(
        f"/proposals/{args.id}/deny",
        method="POST",
        base_url=args.bridge_url,
    )
    print(f"Denied: {result}", file=out)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RSI-Econ proposal approval CLI")
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL, help="Bridge API URL")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List pending proposals")

    approve_p = sub.add_parser("approve", help="Approve a proposal")
    approve_p.add_argument("id", help="Proposal ID")

    deny_p = sub.add_parser("deny", help="Deny a proposal")
    deny_p.add_argument("id", help="Proposal ID")

    args = parser.parse_args()
    commands = {"list": cmd_list, "approve": cmd_approve, "deny": cmd_deny}

    if args.command not in commands:
        parser.print_help()
        return 1
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
