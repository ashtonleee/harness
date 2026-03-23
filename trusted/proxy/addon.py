from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

try:
    from mitmproxy import http as mitm_http
except ImportError:  # pragma: no cover - exercised inside the container
    mitm_http = None


class _FallbackResponse:
    def __init__(self, status_code: int, content: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self.content = content
        self.headers = headers


def _make_response(status_code: int, content: bytes, headers: dict[str, str]) -> Any:
    if mitm_http is not None:
        return mitm_http.Response.make(status_code, content, headers)
    return _FallbackResponse(status_code, content, headers)


_NOISE_PATTERNS = (
    "analytics", "tracking", "pixel", "improving.duckduckgo.com",
    ".ads.", "posthog", "stytch", "stripe.com", "segment.io",
    "cookieyes", "fonts.googleapis.com", "fonts.gstatic.com",
    ".fbcdn.net", "cloudflare", "google-analytics", "googletagmanager",
    "doubleclick.net", "linkedin.com/li/", "px.ads.", "adservice.",
    "sentry.io", "hotjar.com", "fullstory.com", "intercom.io",
    "optimizely.com", "mixpanel.com", "amplitude.com",
)


def _is_noise_domain(host: str) -> bool:
    """Check if a domain is known browser tracking/analytics noise."""
    host = host.lower()
    for pattern in _NOISE_PATTERNS:
        if pattern in host:
            return True
    return False


class PolicyProxy:
    def __init__(
        self,
        *,
        allowlist_env: str | None = None,
        allowlist_path: str | os.PathLike[str] = "/etc/rsi/proxy_allowlist.txt",
        log_path: str | os.PathLike[str] | None = None,
        proposal_url: str | None = None,
        time_fn: Any = None,
    ) -> None:
        self.allowlist_env = allowlist_env if allowlist_env is not None else os.getenv("PROXY_ALLOWLIST", "")
        self.allowlist_path = Path(allowlist_path)
        default_log_path = os.getenv("PROXY_LOG_PATH", "/var/log/rsi/web_egress.jsonl")
        self.log_path = Path(log_path or default_log_path)
        self.proposal_url = proposal_url or os.getenv("PROXY_PROPOSAL_URL", "http://bridge:8081/proposals")
        self._time_fn = time_fn or time.time
        self._allowlist: list[str] = []
        self._allowlist_mtime_ns: int | None = None
        self._log_lock = threading.Lock()
        self._reload_allowlist(force=True)

    def _env_allowlist(self) -> list[str]:
        return [item.strip().lower() for item in self.allowlist_env.split(",") if item.strip()]

    def _reload_allowlist(self, *, force: bool = False) -> None:
        file_entries: list[str] | None = None
        current_mtime_ns: int | None = None
        if self.allowlist_path.exists():
            stat = self.allowlist_path.stat()
            current_mtime_ns = stat.st_mtime_ns
            if force or current_mtime_ns != self._allowlist_mtime_ns:
                lines = self.allowlist_path.read_text(encoding="utf-8").splitlines()
                file_entries = [line.strip().lower() for line in lines if line.strip()]
        elif self._allowlist_mtime_ns is not None:
            file_entries = []

        if file_entries is None and not force:
            return

        fallback_entries = self._env_allowlist()
        self._allowlist = file_entries if file_entries is not None else fallback_entries
        self._allowlist_mtime_ns = current_mtime_ns

    def _domain_allowed(self, host: str) -> bool:
        host = host.lower()
        for suffix in self._allowlist:
            if host == suffix or host.endswith(f".{suffix}"):
                return True
        return False

    def _metadata(self, flow: Any) -> dict[str, Any]:
        metadata = getattr(flow, "metadata", None)
        if metadata is None:
            metadata = {}
            flow.metadata = metadata
        return metadata.setdefault("rsi_policy_proxy", {})

    def _request_url(self, flow: Any) -> str:
        request = flow.request
        return getattr(request, "pretty_url", None) or f"{request.scheme}://{request.host}{request.path}"

    def _create_proposal(self, flow: Any) -> tuple[str | None, str | None]:
        payload = {
            "kind": "http_egress",
            "url": self._request_url(flow),
            "method": flow.request.method.upper(),
            "domain": getattr(flow.request, "pretty_host", flow.request.host),
            "path": flow.request.path,
        }
        encoded_payload = json.dumps(payload).encode("utf-8")
        request = urllib_request.Request(
            self.proposal_url,
            data=encoded_payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        except (urllib_error.URLError, TimeoutError, ValueError) as exc:
            return None, str(exc)
        try:
            proposal_id = json.loads(body)["proposal_id"]
        except (KeyError, json.JSONDecodeError) as exc:
            return None, f"bad proposal response: {exc}"
        return str(proposal_id), None

    def requestheaders(self, flow: Any) -> None:
        self._reload_allowlist()
        request = flow.request
        host = getattr(request, "pretty_host", request.host).lower()
        method = request.method.upper()
        metadata = self._metadata(flow)
        metadata.update(
            start=self._time_fn(),
            domain=host,
            method=method,
            path=request.path,
            policy="allowed_read",
            proposal_id=None,
            error=None,
            logged=False,
        )

        # All traffic is allowed and logged. The containment boundary is Docker
        # networking (internal_net only), not per-request gating.
        # The agent uses POST /proposals on the bridge to deliberately request actions.
        if method in {"GET", "HEAD"}:
            metadata["policy"] = "allowed_read"
        else:
            metadata["policy"] = "allowed_write"
        return

    def _log_record(self, flow: Any, *, status: int, size: int, error: str | None) -> dict[str, Any]:
        metadata = self._metadata(flow)
        timestamp = datetime.now(timezone.utc).isoformat()
        timing_ms = int((self._time_fn() - metadata.get("start", self._time_fn())) * 1000)
        return {
            "timestamp": timestamp,
            "domain": metadata.get("domain"),
            "method": metadata.get("method"),
            "path": metadata.get("path"),
            "status": int(status),
            "size": int(size),
            "timing_ms": timing_ms,
            "policy": metadata.get("policy"),
            "proposal_id": metadata.get("proposal_id"),
            "error": error,
        }

    def _write_log(self, record: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True)
        with self._log_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")

    def response(self, flow: Any) -> None:
        metadata = self._metadata(flow)
        if metadata.get("logged"):
            return
        response = flow.response
        content = getattr(response, "content", None)
        if content is None:
            content = getattr(response, "raw_content", b"")
        record = self._log_record(
            flow,
            status=getattr(response, "status_code", 0) or 0,
            size=len(content or b""),
            error=metadata.get("error"),
        )
        self._write_log(record)
        metadata["logged"] = True

    def error(self, flow: Any) -> None:
        metadata = self._metadata(flow)
        if metadata.get("logged"):
            return
        flow_error = metadata.get("error") or str(getattr(flow, "error", ""))
        record = self._log_record(flow, status=0, size=0, error=flow_error or None)
        self._write_log(record)
        metadata["logged"] = True


addons = [PolicyProxy()]
