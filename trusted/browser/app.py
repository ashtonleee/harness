from contextlib import asynccontextmanager
import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import httpx
import os
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException

from shared.config import browser_settings
from shared.schemas import (
    BrowserFollowHrefInternalResponse,
    BrowserFollowHrefRequest,
    BrowserFollowLink,
    BrowserInteractable,
    BrowserSessionClickRequest,
    BrowserSessionOpenRequest,
    BrowserSessionSelectRequest,
    BrowserSessionSetCheckedRequest,
    BrowserSessionSnapshotInternalResponse,
    BrowserSessionTypeRequest,
    BrowserSubmitExecuteInternalResponse,
    BrowserSubmitExecuteRequest,
    BrowserSubmitFieldPreview,
    BrowserSubmitPreviewInternalResponse,
    BrowserSubmitProposalRequest,
    BrowserRenderInternalResponse,
    BrowserRenderRequest,
    EgressFetchRequest,
    EgressFetchResponse,
    HealthReport,
)
from trusted.browser.policy import (
    browser_channel_violation,
    classify_browser_channel,
    download_violation,
    filechooser_violation,
    popup_violation,
    select_followable_link,
    top_level_navigation_violation,
    validate_browser_target,
)
from trusted.web.mediation import (
    channel_disposition,
    channel_record,
)
from trusted.web.policy import (
    WebPolicy,
    WebPolicyError,
    web_policy_status_code,
)


def build_policy() -> WebPolicy:
    settings = browser_settings()
    return WebPolicy(
        allowlist_hosts=settings.allowlist_hosts,
        private_test_hosts=settings.private_test_hosts,
        max_redirects=settings.max_redirects,
        timeout_seconds=settings.timeout_seconds,
        enable_private_test_hosts=settings.enable_private_test_hosts,
    )


def browser_launch_args() -> list[str]:
    return [
        "--disable-dev-shm-usage",
    ]


def browser_launch_kwargs() -> dict[str, Any]:
    return {
        "headless": True,
        "chromium_sandbox": True,
        "args": browser_launch_args(),
    }


def _browser_status_code(reason: str) -> int:
    if reason in {"screenshot_too_large"}:
        return 413
    return web_policy_status_code(reason)


def _truncate_utf8(text: str, limit_bytes: int) -> tuple[str, int, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit_bytes:
        return text, len(raw), False
    truncated = raw[:limit_bytes]
    while True:
        try:
            return truncated.decode("utf-8"), len(raw), True
        except UnicodeDecodeError:
            truncated = truncated[:-1]


def _limited_text(value: str, limit_chars: int) -> str:
    value = (value or "").strip()
    return value[:limit_chars]


def _fulfill_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in blocked and not key.lower().startswith("x-rsi-")
    }


def _violation_detail(
    *,
    exc: WebPolicyError,
    normalized_url: str,
    final_url: str,
    host: str,
    redirect_chain: list[str],
    observed_hosts: list[str],
    resolved_ips: list[str],
    http_status: int | None,
    page_title: str,
    text_bytes: int,
    text_truncated: bool,
    channel_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "reason": exc.reason,
        "detail": exc.detail,
        "normalized_url": normalized_url,
        "final_url": final_url,
        "host": host,
        "allowlist_decision": "denied",
        "redirect_chain": redirect_chain,
        "observed_hosts": observed_hosts,
        "resolved_ips": resolved_ips,
        "http_status": http_status,
        "page_title": page_title,
        "text_bytes": text_bytes,
        "text_truncated": text_truncated,
        "screenshot_bytes": 0,
        "screenshot_sha256": "",
        "channel_records": channel_records,
    }


def _error_detail(
    *,
    reason: str,
    detail: str,
    normalized_url: str,
    final_url: str,
    host: str,
    redirect_chain: list[str],
    observed_hosts: list[str],
    resolved_ips: list[str],
    http_status: int | None,
    channel_records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "reason": reason,
        "detail": detail,
        "normalized_url": normalized_url,
        "final_url": final_url,
        "host": host,
        "allowlist_decision": "unknown",
        "redirect_chain": redirect_chain,
        "observed_hosts": observed_hosts,
        "resolved_ips": resolved_ips,
        "http_status": http_status,
        "page_title": "",
        "text_bytes": 0,
        "text_truncated": False,
        "screenshot_bytes": 0,
        "screenshot_sha256": "",
        "channel_records": channel_records,
    }


def _browser_channel_guards_script() -> str:
    return """
(() => {
  const root = window;
  root.__RSI_BLOCKED_CHANNELS = [];
  const record = (channel, requestedUrl, reason) => {
    root.__RSI_BLOCKED_CHANNELS.push({
      channel,
      requested_url: String(requestedUrl || ""),
      reason: String(reason || channel),
    });
  };
  const reject = (channel, requestedUrl, reason) => {
    record(channel, requestedUrl, reason);
    throw new Error(reason || channel);
  };

  const originalFetch = root.fetch ? root.fetch.bind(root) : null;
  if (originalFetch) {
    root.fetch = (...args) => {
      const target = args[0] && typeof args[0] === "object" && "url" in args[0]
        ? args[0].url
        : args[0];
      record("fetch_xhr", target, "fetch_xhr_not_allowed");
      return Promise.reject(new Error("fetch_xhr_not_allowed"));
    };
  }

  const xhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__rsiUrl = url;
    return xhrOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    reject("fetch_xhr", this.__rsiUrl || "", "fetch_xhr_not_allowed");
  };

  const formSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function() {
    reject("form_submission", this.action || "", "form_submission_not_allowed");
  };
  if (HTMLFormElement.prototype.requestSubmit) {
    HTMLFormElement.prototype.requestSubmit = function() {
      reject("form_submission", this.action || "", "form_submission_not_allowed");
    };
  }

  if (root.WebSocket) {
    const OriginalWebSocket = root.WebSocket;
    root.WebSocket = function(url) {
      reject("websocket", url, "websocket_not_allowed");
    };
    root.WebSocket.prototype = OriginalWebSocket.prototype;
  }

  if (root.EventSource) {
    const OriginalEventSource = root.EventSource;
    root.EventSource = function(url) {
      reject("eventsource", url, "eventsource_not_allowed");
    };
    root.EventSource.prototype = OriginalEventSource.prototype;
  }

  if (navigator.sendBeacon) {
    const originalSendBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function(url) {
      record("send_beacon", url, "send_beacon_not_allowed");
      return false;
    };
  }

  const originalWindowOpen = root.open ? root.open.bind(root) : null;
  if (originalWindowOpen) {
    root.open = function(url) {
      record("popup", url, "popup_not_allowed");
      return null;
    };
  }

  if (root.location && root.location.assign) {
    const originalAssign = root.location.assign.bind(root.location);
    root.location.assign = function(url) {
      reject("external_protocol", url, "external_protocol_not_allowed");
    };
  }
  if (root.location && root.location.replace) {
    const originalReplace = root.location.replace.bind(root.location);
    root.location.replace = function(url) {
      reject("external_protocol", url, "external_protocol_not_allowed");
    };
  }

  const appendChild = Element.prototype.appendChild;
  Element.prototype.appendChild = function(node) {
    if (node && node.tagName === "LINK") {
      const rel = String(node.rel || "").toLowerCase();
      if (rel === "prefetch" || rel === "preconnect") {
        record("prefetch_preconnect", node.href || "", "prefetch_preconnect_not_allowed");
        return node;
      }
    }
    return appendChild.call(this, node);
  };

  const click = HTMLElement.prototype.click;
  HTMLElement.prototype.click = function() {
    if (this && this.tagName === "A") {
      const href = this.href || this.getAttribute("href") || "";
      if (href && !href.startsWith("http://") && !href.startsWith("https://")) {
        reject("external_protocol", href, "external_protocol_not_allowed");
      }
    }
    if (this && this.tagName === "INPUT" && String(this.type || "").toLowerCase() === "file") {
      reject("upload", "", "upload_not_allowed");
    }
    return click.call(this);
  };
  if (root.HTMLInputElement && HTMLInputElement.prototype.showPicker) {
    const showPicker = HTMLInputElement.prototype.showPicker;
    HTMLInputElement.prototype.showPicker = function() {
      if (this && String(this.type || "").toLowerCase() === "file") {
        reject("upload", "", "upload_not_allowed");
      }
      return showPicker.call(this);
    };
  }

  if (root.Worker) {
    const OriginalWorker = root.Worker;
    root.Worker = function(url) {
      reject("worker", url, "worker_not_allowed");
    };
    root.Worker.prototype = OriginalWorker.prototype;
  }
  if (root.SharedWorker) {
    const OriginalSharedWorker = root.SharedWorker;
    root.SharedWorker = function(url) {
      reject("worker", url, "worker_not_allowed");
    };
    root.SharedWorker.prototype = OriginalSharedWorker.prototype;
  }
  if (navigator.serviceWorker && navigator.serviceWorker.register) {
    const originalRegister = navigator.serviceWorker.register.bind(navigator.serviceWorker);
    navigator.serviceWorker.register = function(url) {
      reject("worker", url, "worker_not_allowed");
    };
  }
})();
""".strip()


async def _extract_js_channel_events(page) -> list[dict[str, Any]]:
    return await page.evaluate(
        "() => {"
        "  const events = Array.isArray(window.__RSI_BLOCKED_CHANNELS) ? [...window.__RSI_BLOCKED_CHANNELS] : [];"
        "  window.__RSI_BLOCKED_CHANNELS = [];"
        "  return events;"
        "}"
    )


def _plain_channel_records(records: list[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for record in records:
        if hasattr(record, "model_dump"):
            payload.append(record.model_dump())
        else:
            payload.append(dict(record))
    return payload


async def _extract_meta_description(page) -> str:
    return _limited_text(
        await page.evaluate(
            "() => {"
            "  const el = document.querySelector('meta[name=\"description\"]');"
            "  return el ? (el.getAttribute('content') || '') : '';"
            "}"
        ),
        512,
    )


async def _extract_rendered_text(page, *, limit_bytes: int) -> tuple[str, str, int, bool]:
    raw_text = await page.evaluate("() => document.body ? document.body.innerText || '' : ''")
    text, text_bytes, truncated = _truncate_utf8(raw_text, limit_bytes)
    return text, hashlib.sha256(raw_text.encode("utf-8")).hexdigest(), text_bytes, truncated


async def _extract_followable_links(
    page,
    *,
    base_url: str,
    policy: WebPolicy,
    max_links: int,
) -> list[BrowserFollowLink]:
    base_target = validate_browser_target(base_url, policy)
    raw_links = await page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href]')).map((anchor) => ({"
        "  href: anchor.getAttribute('href') || '',"
        "  text: (anchor.innerText || anchor.textContent || '').trim(),"
        "}));"
    )
    seen: set[str] = set()
    followable_links: list[BrowserFollowLink] = []
    for entry in raw_links:
        href = str(entry.get("href", "")).strip()
        if not href:
            continue
        try:
            target = validate_browser_target(urljoin(base_url, href), policy)
        except WebPolicyError:
            continue
        if target.normalized_url in seen:
            continue
        seen.add(target.normalized_url)
        label = _limited_text(str(entry.get("text", "")).strip(), 120) or target.normalized_url
        followable_links.append(
            BrowserFollowLink(
                text=label,
                target_url=target.normalized_url,
                same_origin=(
                    target.scheme == base_target.scheme
                    and target.host == base_target.host
                    and target.port == base_target.port
                ),
            )
        )
        if len(followable_links) >= max_links:
            break
    return followable_links


async def _extract_interactable_elements(
    page,
    *,
    max_items: int = 64,
) -> list[BrowserInteractable]:
    raw_items = await page.evaluate(
        """
(maxItems) => {
  const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const visible = (el) => {
    if (!el || el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return el.getClientRects().length > 0;
  };
  const labelText = (el) => {
    if (el.labels && el.labels.length) {
      for (const label of el.labels) {
        const text = normalize(label.innerText || label.textContent || "");
        if (text) return text;
      }
    }
    const closest = el.closest("label");
    if (closest) {
      const text = normalize(closest.innerText || closest.textContent || "");
      if (text) return text;
    }
    return normalize(el.getAttribute("aria-label") || "");
  };
  const previewValue = (el, kind) => {
    if (kind === "select") {
      const selected = Array.from(el.selectedOptions || []).map((option) => normalize(option.text || option.value || ""));
      return normalize(selected.join(", "));
    }
    if (kind === "checkbox" || kind === "radio") {
      return normalize(el.value || "");
    }
    if ("value" in el) {
      return normalize(el.value || "").slice(0, 120);
    }
    return "";
  };
  const determineKind = (el) => {
    const tag = el.tagName.toLowerCase();
    const inputType = normalize(el.getAttribute("type") || el.type || "").toLowerCase();
    if (tag === "a") return el.href ? "link" : "";
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    if (tag === "button") {
      if (!inputType || inputType === "submit") return "submit";
      if (inputType === "reset") return "";
      return "button";
    }
    if (tag !== "input") return "";
    if (["text", "search", "email", "url", "tel", "number", "password", ""].includes(inputType)) return "text_input";
    if (inputType === "checkbox") return "checkbox";
    if (inputType === "radio") return "radio";
    if (inputType === "submit" || inputType === "image") return "submit";
    if (inputType === "button") return "button";
    return "";
  };
  for (const stale of document.querySelectorAll("[data-rsi-element-id]")) {
    stale.removeAttribute("data-rsi-element-id");
  }
  const candidates = Array.from(document.querySelectorAll("a[href], button, input, textarea, select"));
  const results = [];
  let index = 1;
  for (const el of candidates) {
    if (results.length >= maxItems) break;
    if (!visible(el)) continue;
    const kind = determineKind(el);
    if (!kind) continue;
    const href = el.tagName.toLowerCase() === "a" ? String(el.href || "") : "";
    if (kind === "link" && href && !href.startsWith("http://") && !href.startsWith("https://")) continue;
    const elementId = `el_${String(index).padStart(3, "0")}`;
    index += 1;
    el.setAttribute("data-rsi-element-id", elementId);
    const text = normalize(el.innerText || el.textContent || el.value || "").slice(0, 120);
    const label = labelText(el) || normalize(el.getAttribute("placeholder") || "") || normalize(el.getAttribute("name") || "");
    results.push({
      element_id: elementId,
      kind,
      label,
      text,
      name: normalize(el.getAttribute("name") || ""),
      input_type: normalize(el.getAttribute("type") || el.type || "").toLowerCase(),
      placeholder: normalize(el.getAttribute("placeholder") || ""),
      href,
      disabled: Boolean(el.disabled),
      checked: Boolean(el.checked),
      value_preview: previewValue(el, kind),
    });
  }
  return results;
}
        """,
        max_items,
    )
    return [BrowserInteractable.model_validate(item) for item in raw_items]


def _session_error(
    status_code: int,
    *,
    reason: str,
    detail: str,
    session_id: str = "",
    snapshot_id: str = "",
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "reason": reason,
            "detail": detail,
            "session_id": session_id,
            "snapshot_id": snapshot_id,
        },
    )


def _normalize_form_method(raw: str) -> str:
    method = (raw or "get").strip().upper() or "GET"
    if method not in {"GET", "POST"}:
        raise WebPolicyError("form_method_not_allowed", raw or method)
    return method


async def _request_body_bytes(request) -> bytes:
    for attr_name in ("post_data_buffer", "post_data"):
        candidate = getattr(request, attr_name, None)
        if candidate is None:
            continue
        value = candidate() if callable(candidate) else candidate
        if asyncio.iscoroutine(value):
            value = await value
        if value in {None, ""}:
            continue
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)
    return b""


@dataclass
class BrowserSessionObservation:
    http_status: int | None = None
    top_level_started: bool = False
    redirect_chain: list[str] = field(default_factory=list)
    observed_hosts: set[str] = field(default_factory=set)
    resolved_ips: set[str] = field(default_factory=set)
    channel_records: list[dict[str, Any]] = field(default_factory=list)
    violation: WebPolicyError | None = None
    event_tasks: list[asyncio.Task] = field(default_factory=list)


@dataclass
class BrowserSessionState:
    session_id: str
    context: Any
    page: Any
    created_at: float
    updated_at: float
    current_snapshot_id: str = ""
    current_interactables: dict[str, BrowserInteractable] = field(default_factory=dict)
    observation: BrowserSessionObservation = field(default_factory=BrowserSessionObservation)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserSessionManager:
    def __init__(self, *, browser, egress_client, policy: WebPolicy, settings):
        self.browser = browser
        self.egress_client = egress_client
        self.policy = policy
        self.settings = settings
        self.sessions: dict[str, BrowserSessionState] = {}
        self.lock = asyncio.Lock()

    async def prune_expired(self):
        expired: list[BrowserSessionState] = []
        now = monotonic()
        async with self.lock:
            for session_id, session in list(self.sessions.items()):
                if now - session.updated_at <= self.settings.session_ttl_seconds:
                    continue
                expired.append(self.sessions.pop(session_id))
        for session in expired:
            await self._close_session(session)

    async def close_all(self):
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            await self._close_session(session)

    async def open_session(self, url: str) -> BrowserSessionSnapshotInternalResponse:
        await self.prune_expired()
        session_id = uuid4().hex
        async with self.lock:
            if len(self.sessions) >= self.settings.session_max_concurrent:
                raise _session_error(
                    409,
                    reason="browser_session_limit_reached",
                    detail="interactive browser session limit reached",
                )
        context = await self.browser.new_context(
            viewport={
                "width": self.settings.viewport_width,
                "height": self.settings.viewport_height,
            },
            accept_downloads=False,
            service_workers="block",
        )
        page = await context.new_page()
        await page.add_init_script(_browser_channel_guards_script())
        session = BrowserSessionState(
            session_id=session_id,
            context=context,
            page=page,
            created_at=monotonic(),
            updated_at=monotonic(),
        )
        async def route_handler(route):
            await self._handle_route(session, route)

        await page.route("**/*", route_handler)
        page.on(
            "popup",
            lambda popup: session.observation.event_tasks.append(
                asyncio.create_task(self._handle_popup(session, popup))
            ),
        )
        page.on(
            "download",
            lambda download: session.observation.event_tasks.append(
                asyncio.create_task(self._handle_download(session, download))
            ),
        )
        page.on(
            "filechooser",
            lambda chooser: session.observation.event_tasks.append(
                asyncio.create_task(self._handle_filechooser(session, chooser))
            ),
        )
        async with self.lock:
            self.sessions[session_id] = session
        try:
            async with session.lock:
                self._reset_observation(session, base_url=url)
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.settings.timeout_seconds * 1000),
                )
                if response is not None:
                    session.observation.http_status = response.status
                return await self._finish_action_and_snapshot(session)
        except Exception:
            async with self.lock:
                self.sessions.pop(session_id, None)
            await self._close_session(session)
            raise

    async def snapshot(self, session_id: str) -> BrowserSessionSnapshotInternalResponse:
        session = await self._require_session(session_id)
        async with session.lock:
            return await self._capture_snapshot(session)

    async def click(
        self,
        session_id: str,
        payload: BrowserSessionClickRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        session = await self._require_session(session_id)
        async with session.lock:
            interactable = self._require_interactable(session, payload.snapshot_id, payload.element_id)
            if interactable.kind == "submit":
                raise _session_error(
                    409,
                    reason="browser_submit_requires_proposal",
                    detail="submit elements require submit_proposal, not click",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            if interactable.kind not in {"link", "button"}:
                raise _session_error(
                    409,
                    reason="interactable_kind_mismatch",
                    detail=f"{interactable.kind} cannot be clicked through this route",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            locator = self._locator_for(payload.element_id, session)
            self._reset_observation(session, base_url=session.page.url or "")
            await locator.click(timeout=int(self.settings.timeout_seconds * 1000))
            return await self._finish_action_and_snapshot(session)

    async def type_text(
        self,
        session_id: str,
        payload: BrowserSessionTypeRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        session = await self._require_session(session_id)
        async with session.lock:
            interactable = self._require_interactable(session, payload.snapshot_id, payload.element_id)
            if interactable.kind not in {"text_input", "textarea"}:
                raise _session_error(
                    409,
                    reason="interactable_kind_mismatch",
                    detail=f"{interactable.kind} cannot be typed into",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            locator = self._locator_for(payload.element_id, session)
            self._reset_observation(session, base_url=session.page.url or "")
            await locator.fill(payload.text, timeout=int(self.settings.timeout_seconds * 1000))
            return await self._finish_action_and_snapshot(session)

    async def select_value(
        self,
        session_id: str,
        payload: BrowserSessionSelectRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        session = await self._require_session(session_id)
        async with session.lock:
            interactable = self._require_interactable(session, payload.snapshot_id, payload.element_id)
            if interactable.kind != "select":
                raise _session_error(
                    409,
                    reason="interactable_kind_mismatch",
                    detail=f"{interactable.kind} cannot be used with select",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            locator = self._locator_for(payload.element_id, session)
            actual_value = await self._resolve_select_value(locator, payload.value)
            self._reset_observation(session, base_url=session.page.url or "")
            await locator.select_option(value=actual_value, timeout=int(self.settings.timeout_seconds * 1000))
            return await self._finish_action_and_snapshot(session)

    async def set_checked(
        self,
        session_id: str,
        payload: BrowserSessionSetCheckedRequest,
    ) -> BrowserSessionSnapshotInternalResponse:
        session = await self._require_session(session_id)
        async with session.lock:
            interactable = self._require_interactable(session, payload.snapshot_id, payload.element_id)
            if interactable.kind not in {"checkbox", "radio"}:
                raise _session_error(
                    409,
                    reason="interactable_kind_mismatch",
                    detail=f"{interactable.kind} cannot be checked",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            if interactable.kind == "radio" and not payload.checked:
                raise _session_error(
                    409,
                    reason="radio_uncheck_not_allowed",
                    detail="radio inputs cannot be unchecked explicitly",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            locator = self._locator_for(payload.element_id, session)
            self._reset_observation(session, base_url=session.page.url or "")
            if payload.checked:
                await locator.check(timeout=int(self.settings.timeout_seconds * 1000))
            else:
                await locator.uncheck(timeout=int(self.settings.timeout_seconds * 1000))
            return await self._finish_action_and_snapshot(session)

    async def prepare_submit(
        self,
        session_id: str,
        payload: BrowserSubmitProposalRequest,
    ) -> BrowserSubmitPreviewInternalResponse:
        session = await self._require_session(session_id)
        async with session.lock:
            interactable = self._require_interactable(session, payload.snapshot_id, payload.element_id)
            if interactable.kind != "submit":
                raise _session_error(
                    409,
                    reason="interactable_kind_mismatch",
                    detail=f"{interactable.kind} cannot submit a form",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            preview = await self._submit_preview(session, payload.element_id)
            session.updated_at = monotonic()
            return preview

    async def execute_submit(
        self,
        session_id: str,
        payload: BrowserSubmitExecuteRequest,
    ) -> BrowserSubmitExecuteInternalResponse:
        session = await self._require_session(session_id)
        async with session.lock:
            interactable = self._require_interactable(session, payload.snapshot_id, payload.element_id)
            if interactable.kind != "submit":
                raise _session_error(
                    409,
                    reason="interactable_kind_mismatch",
                    detail=f"{interactable.kind} cannot submit a form",
                    session_id=session_id,
                    snapshot_id=payload.snapshot_id,
                )
            preview = await self._submit_preview(session, payload.element_id)
            locator = self._locator_for(payload.element_id, session)
            self._reset_observation(session, base_url=session.page.url or preview.target_url)
            await locator.click(timeout=int(self.settings.timeout_seconds * 1000))
            snapshot = await self._finish_action_and_snapshot(session)
            return BrowserSubmitExecuteInternalResponse(
                session_id=session_id,
                snapshot=snapshot,
                target_url=preview.target_url,
                method=preview.method,
                field_preview=list(preview.field_preview),
            )

    async def _require_session(self, session_id: str) -> BrowserSessionState:
        await self.prune_expired()
        async with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise _session_error(
                404,
                reason="browser_session_missing",
                detail=session_id,
                session_id=session_id,
            )
        return session

    async def _close_session(self, session: BrowserSessionState):
        try:
            await session.context.close()
        except Exception:
            pass

    def _reset_observation(self, session: BrowserSessionState, *, base_url: str):
        observed_hosts: set[str] = set()
        if base_url:
            host = urlsplit(base_url).hostname or ""
            if host:
                observed_hosts.add(host)
        session.observation = BrowserSessionObservation(observed_hosts=observed_hosts)

    def _require_interactable(
        self,
        session: BrowserSessionState,
        snapshot_id: str,
        element_id: str,
    ) -> BrowserInteractable:
        if snapshot_id != session.current_snapshot_id:
            raise _session_error(
                409,
                reason="browser_snapshot_stale",
                detail=f"expected {session.current_snapshot_id}, got {snapshot_id}",
                session_id=session.session_id,
                snapshot_id=snapshot_id,
            )
        interactable = session.current_interactables.get(element_id)
        if interactable is None:
            raise _session_error(
                404,
                reason="interactable_not_found",
                detail=element_id,
                session_id=session.session_id,
                snapshot_id=snapshot_id,
            )
        return interactable

    def _locator_for(self, element_id: str, session: BrowserSessionState):
        return session.page.locator(f'[data-rsi-element-id="{element_id}"]').first

    async def _resolve_select_value(self, locator, requested: str) -> str:
        options = await locator.evaluate(
            """(el) => Array.from(el.options || []).map((option) => ({
              value: String(option.value || ""),
              label: String(option.label || option.text || ""),
            }))"""
        )
        for option in options:
            if option["value"] == requested or option["label"] == requested:
                return str(option["value"])
        raise _session_error(404, reason="select_option_not_found", detail=requested)

    async def _submit_preview(
        self,
        session: BrowserSessionState,
        element_id: str,
    ) -> BrowserSubmitPreviewInternalResponse:
        locator = self._locator_for(element_id, session)
        raw_preview = await locator.evaluate(
            """
(el) => {
  const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const form = el.form || el.closest("form");
  if (!form) {
    return {error: "submit_form_missing"};
  }
  const action = el.getAttribute("formaction") || form.getAttribute("action") || window.location.href;
  const method = el.getAttribute("formmethod") || form.getAttribute("method") || "get";
  const fields = [];
  for (const field of Array.from(form.elements || [])) {
    if (!field || !field.name || field.disabled) continue;
    const tag = (field.tagName || "").toLowerCase();
    const type = normalize(field.getAttribute("type") || field.type || "").toLowerCase();
    if (type === "file") continue;
    if (["submit", "button", "reset", "image"].includes(type)) continue;
    if ((type === "checkbox" || type === "radio") && !field.checked) continue;
    let value = "";
    if (tag === "select") {
      const selected = Array.from(field.selectedOptions || []).map((option) => normalize(option.text || option.value || ""));
      value = normalize(selected.join(", "));
    } else {
      value = normalize(field.value || "");
    }
    fields.push({
      name: normalize(field.name || ""),
      kind: tag === "select" ? "select" : (type || tag || "field"),
      value_preview: value.slice(0, 160),
      checked: Boolean(field.checked),
    });
  }
  return {
    action: new URL(action, window.location.href).toString(),
    method: method,
    fields,
  };
}
            """
        )
        if raw_preview.get("error") == "submit_form_missing":
            raise _session_error(
                409,
                reason="submit_form_missing",
                detail=element_id,
                session_id=session.session_id,
                snapshot_id=session.current_snapshot_id,
            )
        method = _normalize_form_method(str(raw_preview.get("method", "get")))
        target = validate_browser_target(str(raw_preview.get("action", "")), self.policy)
        return BrowserSubmitPreviewInternalResponse(
            session_id=session.session_id,
            snapshot_id=session.current_snapshot_id,
            submit_element_id=element_id,
            target_url=target.normalized_url,
            method=method,
            field_preview=[
                BrowserSubmitFieldPreview.model_validate(item)
                for item in raw_preview.get("fields", [])
            ],
        )

    async def _finish_action_and_snapshot(
        self,
        session: BrowserSessionState,
    ) -> BrowserSessionSnapshotInternalResponse:
        try:
            await session.page.wait_for_load_state(
                "domcontentloaded",
                timeout=int(self.settings.timeout_seconds * 1000),
            )
        except Exception:
            pass
        await session.page.wait_for_timeout(self.settings.settle_time_ms)
        if session.observation.event_tasks:
            await asyncio.gather(*session.observation.event_tasks, return_exceptions=True)
        session.observation.event_tasks.clear()
        return await self._capture_snapshot(session)

    async def _capture_snapshot(self, session: BrowserSessionState) -> BrowserSessionSnapshotInternalResponse:
        page_title = ""
        text_bytes = 0
        text_truncated = False
        try:
            for js_event in await _extract_js_channel_events(session.page):
                requested_url = str(js_event.get("requested_url", "")).strip()
                reason = str(js_event.get("reason", "browser_channel_not_allowed"))
                channel = str(js_event.get("channel", "subresource"))
                session.observation.channel_records.append(
                    channel_record(
                        channel=channel,
                        requested_url=requested_url,
                        disposition="denied",
                        reason=reason,
                    )
                )
                if session.observation.violation is None:
                    session.observation.violation = browser_channel_violation(channel, requested_url or reason)

            if session.observation.violation is not None:
                raise session.observation.violation

            current_url = session.page.url or "about:blank"
            if current_url.startswith(("http://", "https://")):
                current_target = validate_browser_target(current_url, self.policy)
                current_url = current_target.normalized_url
                session.observation.observed_hosts.add(current_target.host)
            meta_description = await _extract_meta_description(session.page)
            page_title = _limited_text(await session.page.title(), 256)
            rendered_text, rendered_text_sha256, text_bytes, text_truncated = await _extract_rendered_text(
                session.page,
                limit_bytes=self.settings.max_rendered_text_bytes,
            )
            screenshot = await session.page.screenshot(type="png")
            if len(screenshot) > self.settings.max_screenshot_bytes:
                raise WebPolicyError(
                    "screenshot_too_large",
                    f"{len(screenshot)} > {self.settings.max_screenshot_bytes}",
                )
            interactables = await _extract_interactable_elements(session.page)
            session.current_snapshot_id = uuid4().hex
            session.current_interactables = {
                item.element_id: item
                for item in interactables
            }
            session.updated_at = monotonic()
            return BrowserSessionSnapshotInternalResponse(
                session_id=session.session_id,
                snapshot_id=session.current_snapshot_id,
                current_url=current_url,
                http_status=session.observation.http_status,
                page_title=page_title,
                meta_description=meta_description,
                rendered_text=rendered_text,
                rendered_text_sha256=rendered_text_sha256,
                text_bytes=text_bytes,
                text_truncated=text_truncated,
                screenshot_png_base64=base64.b64encode(screenshot).decode("ascii"),
                screenshot_sha256=hashlib.sha256(screenshot).hexdigest(),
                screenshot_bytes=len(screenshot),
                observed_hosts=sorted(session.observation.observed_hosts),
                resolved_ips=sorted(session.observation.resolved_ips),
                channel_records=_plain_channel_records(list(session.observation.channel_records)),
                interactable_elements=interactables,
            )
        except WebPolicyError as exc:
            current_url = session.page.url or "about:blank"
            host = urlsplit(current_url).hostname or ""
            raise HTTPException(
                status_code=_browser_status_code(exc.reason),
                detail=_violation_detail(
                    exc=exc,
                    normalized_url=current_url,
                    final_url=current_url,
                    host=host,
                    redirect_chain=list(session.observation.redirect_chain),
                    observed_hosts=sorted(session.observation.observed_hosts),
                    resolved_ips=sorted(session.observation.resolved_ips),
                    http_status=session.observation.http_status,
                    page_title=page_title,
                    text_bytes=text_bytes,
                    text_truncated=text_truncated,
                    channel_records=_plain_channel_records(list(session.observation.channel_records)),
                ),
            ) from exc

    async def _handle_route(self, session: BrowserSessionState, route):
        request = route.request
        request_url = request.url
        observation = session.observation
        channel = classify_browser_channel(
            resource_type=request.resource_type,
            is_navigation_request=request.is_navigation_request(),
            is_main_frame=request.frame == session.page.main_frame,
            headers=dict(request.headers),
            top_level_started=observation.top_level_started,
        )
        is_top_level = request.is_navigation_request() and request.frame == session.page.main_frame
        is_navigation = request.is_navigation_request()
        if is_top_level:
            observation.top_level_started = True
        try:
            normalized = validate_browser_target(request_url, self.policy)
        except WebPolicyError as exc:
            observation.channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=is_top_level,
                    navigation=is_navigation,
                )
            )
            if observation.violation is None:
                observation.violation = exc
            await route.abort("blockedbyclient")
            return

        if channel not in {"top_level_navigation", "redirect"}:
            exc = browser_channel_violation(channel, request_url)
            observation.channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=is_top_level,
                    navigation=is_navigation,
                )
            )
            if observation.violation is None:
                observation.violation = exc
            await route.abort("blockedbyclient")
            return

        record = channel_record(
            channel=channel,
            requested_url=request_url,
            disposition="allowed",
            reason="pre_connect_pending",
            top_level=is_top_level,
            navigation=is_navigation,
        )
        request_body = await _request_body_bytes(request)
        try:
            response = await self.egress_client.post(
                "/internal/fetch",
                json=EgressFetchRequest(
                    url=normalized.normalized_url,
                    channel=channel,
                    headers=dict(request.headers),
                    method=request.method,
                    request_body_base64=base64.b64encode(request_body).decode("ascii") if request_body else "",
                    request_content_type=dict(request.headers).get("content-type", ""),
                    max_body_bytes=2 * 1024 * 1024,
                ).model_dump(),
            )
            response.raise_for_status()
            egress = EgressFetchResponse.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json()["detail"]
            record.update(
                {
                    "normalized_url": detail.get("normalized_url", normalized.normalized_url),
                    "host": detail.get("host", normalized.host),
                    "approved_ips": list(detail.get("approved_ips", [])),
                    "actual_peer_ip": detail.get("actual_peer_ip"),
                    "dialed_ip": detail.get("dialed_ip"),
                    "disposition": "denied",
                    "reason": detail.get("reason", "egress_denied"),
                    "enforcement_stage": detail.get("enforcement_stage", "unknown"),
                    "request_forwarded": bool(detail.get("request_forwarded", False)),
                }
            )
            observation.observed_hosts.add(detail.get("host", normalized.host))
            observation.resolved_ips.update(detail.get("approved_ips", []))
            observation.channel_records.append(record)
            if observation.violation is None:
                observation.violation = WebPolicyError(
                    detail.get("reason", "egress_denied"),
                    detail.get("detail", detail.get("reason", "egress_denied")),
                )
            await route.fulfill(
                status=exc.response.status_code,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=detail.get("reason", "egress_denied"),
            )
            return

        record.update(
            {
                "normalized_url": egress.normalized_url,
                "host": egress.host,
                "approved_ips": list(egress.approved_ips),
                "actual_peer_ip": egress.actual_peer_ip,
                "dialed_ip": egress.dialed_ip,
                "reason": "redirect_hop_allowed"
                if egress.http_status in {301, 302, 303, 307, 308}
                else "pre_connect_pinned",
                "enforcement_stage": egress.enforcement_stage,
                "request_forwarded": egress.request_forwarded,
            }
        )
        if is_top_level:
            observation.http_status = egress.http_status
        observation.observed_hosts.add(egress.host)
        observation.resolved_ips.update(egress.approved_ips)
        if egress.http_status in {301, 302, 303, 307, 308}:
            location = egress.headers.get("location", "").strip()
            if location:
                observation.redirect_chain.append(urljoin(normalized.normalized_url, location))
        observation.channel_records.append(record)
        await route.fulfill(
            status=egress.http_status,
            headers=_fulfill_headers(egress.headers),
            body=base64.b64decode(egress.body_base64),
        )

    async def _handle_popup(self, session: BrowserSessionState, popup):
        exc = popup_violation(popup.url or session.page.url)
        session.observation.channel_records.append(
            channel_record(
                channel="popup",
                requested_url=popup.url or session.page.url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        if session.observation.violation is None:
            session.observation.violation = exc
        try:
            await popup.close()
        except Exception:
            pass

    async def _handle_download(self, session: BrowserSessionState, download):
        exc = download_violation(
            session.page.url,
            suggested_filename=getattr(download, "suggested_filename", None),
        )
        session.observation.channel_records.append(
            channel_record(
                channel="download",
                requested_url=session.page.url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        if session.observation.violation is None:
            session.observation.violation = exc
        try:
            await download.cancel()
        except Exception:
            pass

    async def _handle_filechooser(self, session: BrowserSessionState, file_chooser):
        exc = filechooser_violation(file_chooser.page.url or session.page.url)
        session.observation.channel_records.append(
            channel_record(
                channel="upload",
                requested_url=file_chooser.page.url or session.page.url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        if session.observation.violation is None:
            session.observation.violation = exc


async def _preflight_navigation(
    url: str,
    *,
    policy: WebPolicy,
) -> tuple[Any, list[str], set[str], set[str], list[dict[str, Any]]]:
    current_target = validate_browser_target(url, policy)
    current_channel = "top_level_navigation"
    redirect_chain: list[str] = []
    observed_hosts = {current_target.host}
    resolved_ips: set[str] = set()
    channel_records: list[dict[str, Any]] = []

    for _ in range(policy.max_redirects + 1):
        try:
            response = await app.state.egress_client.post(
                "/internal/fetch",
                json=EgressFetchRequest(
                    url=current_target.normalized_url,
                    channel=current_channel,
                    headers={},
                    max_body_bytes=1,
                ).model_dump(),
            )
            response.raise_for_status()
            egress = EgressFetchResponse.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json()["detail"]
            observed_hosts.add(detail.get("host", current_target.host))
            resolved_ips.update(detail.get("approved_ips", []))
            denied = channel_record(
                channel=current_channel,
                requested_url=current_target.normalized_url,
                disposition="denied",
                reason=detail.get("reason", "egress_denied"),
                top_level=current_channel in {"top_level_navigation", "redirect"},
                navigation=True,
            )
            denied.update(
                {
                    "normalized_url": detail.get("normalized_url", current_target.normalized_url),
                    "host": detail.get("host", current_target.host),
                    "approved_ips": list(detail.get("approved_ips", [])),
                    "actual_peer_ip": detail.get("actual_peer_ip"),
                    "dialed_ip": detail.get("dialed_ip"),
                    "enforcement_stage": detail.get("enforcement_stage", "unknown"),
                    "request_forwarded": bool(detail.get("request_forwarded", False)),
                }
            )
            channel_records.append(denied)
            status_code = (
                403
                if detail.get("reason") in {"connect_failed", "peer_binding_mismatch", "peer_binding_missing"}
                else exc.response.status_code
            )
            raise HTTPException(
                status_code=status_code,
                detail=_error_detail(
                    reason=detail.get("reason", "egress_denied"),
                    detail=detail.get("detail", detail.get("reason", "egress_denied")),
                    normalized_url=current_target.normalized_url,
                    final_url=current_target.normalized_url,
                    host=current_target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=detail.get("http_status"),
                    channel_records=list(channel_records),
                ),
            ) from exc

        allowed = channel_record(
            channel=current_channel,
            requested_url=current_target.normalized_url,
            disposition="allowed",
            reason="pre_connect_pinned",
            top_level=current_channel in {"top_level_navigation", "redirect"},
            navigation=True,
        )
        allowed.update(
            {
                "normalized_url": egress.normalized_url,
                "host": egress.host,
                "approved_ips": list(egress.approved_ips),
                "actual_peer_ip": egress.actual_peer_ip,
                "dialed_ip": egress.dialed_ip,
                "enforcement_stage": egress.enforcement_stage,
                "request_forwarded": egress.request_forwarded,
                "reason": "redirect_hop_allowed"
                if egress.http_status in {301, 302, 303, 307, 308}
                else "pre_connect_pinned",
            }
        )
        channel_records.append(allowed)
        observed_hosts.add(egress.host)
        resolved_ips.update(egress.approved_ips)

        if egress.http_status not in {301, 302, 303, 307, 308}:
            return current_target, redirect_chain, observed_hosts, resolved_ips, channel_records

        location = egress.headers.get("location", "").strip()
        if not location:
            raise HTTPException(
                status_code=400,
                detail=_error_detail(
                    reason="redirect_missing_location",
                    detail=current_target.normalized_url,
                    normalized_url=current_target.normalized_url,
                    final_url=current_target.normalized_url,
                    host=current_target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=egress.http_status,
                    channel_records=list(channel_records),
                ),
            )

        try:
            next_target = validate_browser_target(urljoin(current_target.normalized_url, location), policy)
        except WebPolicyError as exc:
            denied = channel_record(
                channel="redirect",
                requested_url=urljoin(current_target.normalized_url, location),
                disposition="denied",
                reason=exc.reason,
                top_level=True,
                navigation=True,
            )
            channel_records.append(denied)
            raise HTTPException(
                status_code=_browser_status_code(exc.reason),
                detail=_violation_detail(
                    exc=exc,
                    normalized_url=current_target.normalized_url,
                    final_url=current_target.normalized_url,
                    host=current_target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=egress.http_status,
                    page_title="",
                    text_bytes=0,
                    text_truncated=False,
                    channel_records=list(channel_records),
                ),
            ) from exc

        redirect_chain.append(next_target.normalized_url)
        observed_hosts.add(next_target.host)
        current_target = next_target
        current_channel = "redirect"

    raise HTTPException(
        status_code=403,
        detail=_error_detail(
            reason="too_many_redirects",
            detail=url.strip(),
            normalized_url=url.strip(),
            final_url=url.strip(),
            host=urlsplit(url).hostname or "",
            redirect_chain=list(redirect_chain),
            observed_hosts=sorted(observed_hosts),
            resolved_ips=sorted(resolved_ips),
            http_status=None,
            channel_records=list(channel_records),
        ),
    )


async def _render_page(
    url: str,
    *,
    strict_top_level_after_load: bool = False,
    include_followable_links: bool = True,
) -> BrowserRenderInternalResponse:
    settings = app.state.settings
    policy = app.state.policy
    try:
        target = validate_browser_target(url, policy)
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail={
                "reason": exc.reason,
                "detail": exc.detail,
                "normalized_url": url.strip(),
                "final_url": url.strip(),
                "host": "",
                "allowlist_decision": "denied",
                "redirect_chain": [],
                "observed_hosts": [],
                "resolved_ips": [],
                "http_status": None,
                "page_title": "",
                "meta_description": "",
                "rendered_text_sha256": "",
                "text_bytes": 0,
                "text_truncated": False,
                "screenshot_sha256": "",
                "screenshot_bytes": 0,
                "channel_records": [],
            },
        ) from exc

    target, redirect_chain, observed_hosts, resolved_ips, channel_records = await _preflight_navigation(
        target.normalized_url,
        policy=policy,
    )
    violation: WebPolicyError | None = None
    http_status: int | None = None
    page_title = ""
    text_bytes = 0
    text_truncated = False
    event_tasks: list[asyncio.Task] = []
    locked_main_url: str | None = None
    top_level_started = False

    browser = app.state.browser
    context = await browser.new_context(
        viewport={
            "width": settings.viewport_width,
            "height": settings.viewport_height,
        },
        accept_downloads=False,
        service_workers="block",
    )
    page = await context.new_page()
    await page.add_init_script(_browser_channel_guards_script())

    async def record_violation(exc: WebPolicyError):
        nonlocal violation
        if violation is None:
            violation = exc

    async def handle_route(route):
        nonlocal locked_main_url, top_level_started
        request = route.request
        request_url = request.url
        channel = classify_browser_channel(
            resource_type=request.resource_type,
            is_navigation_request=request.is_navigation_request(),
            is_main_frame=request.frame == page.main_frame,
            headers=dict(request.headers),
            top_level_started=top_level_started,
        )
        is_top_level = request.is_navigation_request() and request.frame == page.main_frame
        is_navigation = request.is_navigation_request()

        if is_top_level and locked_main_url is not None:
            exc = top_level_navigation_violation(request_url)
            channel_records.append(
                channel_record(
                    channel="top_level_navigation",
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=True,
                    navigation=True,
                )
            )
            await record_violation(exc)
            await route.abort("blockedbyclient")
            return

        try:
            normalized = validate_browser_target(request_url, policy)
        except WebPolicyError as exc:
            channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=is_top_level,
                    navigation=is_navigation,
                )
            )
            await record_violation(exc)
            await route.abort("blockedbyclient")
            return

        if channel not in {"top_level_navigation", "redirect"} and channel_disposition(channel):
            exc = browser_channel_violation(channel, request_url)
            channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=request_url,
                    disposition="denied",
                    reason=exc.reason,
                    top_level=is_top_level,
                    navigation=is_navigation,
                )
            )
            await record_violation(exc)
            await route.abort("blockedbyclient")
            return

        observed_hosts.add(normalized.host)
        if channel == "redirect":
            if len(redirect_chain) >= policy.max_redirects:
                exc = WebPolicyError("too_many_redirects", normalized.normalized_url)
                channel_records.append(
                    channel_record(
                        channel=channel,
                        requested_url=request_url,
                        disposition="denied",
                        reason=exc.reason,
                        top_level=is_top_level,
                        navigation=is_navigation,
                    )
                )
                await record_violation(exc)
                await route.abort("blockedbyclient")
                return
            if normalized.normalized_url not in redirect_chain:
                redirect_chain.append(normalized.normalized_url)
        if is_top_level:
            top_level_started = True

        record = channel_record(
            channel=channel,
            requested_url=request_url,
            disposition="allowed",
            reason="pre_connect_pending",
            top_level=is_top_level,
            navigation=is_navigation,
        )
        try:
            response = await app.state.egress_client.post(
                "/internal/fetch",
                json=EgressFetchRequest(
                    url=normalized.normalized_url,
                    channel=channel,
                    headers=dict(request.headers),
                    max_body_bytes=2 * 1024 * 1024,
                ).model_dump(),
            )
            response.raise_for_status()
            egress = EgressFetchResponse.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json()["detail"]
            record.update(
                {
                    "normalized_url": detail.get("normalized_url", normalized.normalized_url),
                    "host": detail.get("host", normalized.host),
                    "approved_ips": list(detail.get("approved_ips", [])),
                    "actual_peer_ip": detail.get("actual_peer_ip"),
                    "dialed_ip": detail.get("dialed_ip"),
                    "disposition": "denied",
                    "reason": detail.get("reason", "egress_denied"),
                    "enforcement_stage": detail.get("enforcement_stage", "unknown"),
                    "request_forwarded": bool(detail.get("request_forwarded", False)),
                }
            )
            observed_hosts.add(detail.get("host", normalized.host))
            resolved_ips.update(detail.get("approved_ips", []))
            channel_records.append(record)
            await record_violation(
                WebPolicyError(
                    detail.get("reason", "egress_denied"),
                    detail.get("detail", detail.get("reason", "egress_denied")),
                )
            )
            await route.fulfill(
                status=exc.response.status_code,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=detail.get("reason", "egress_denied"),
            )
            return

        record.update(
            {
                "normalized_url": egress.normalized_url,
                "host": egress.host,
                "approved_ips": list(egress.approved_ips),
                "actual_peer_ip": egress.actual_peer_ip,
                "dialed_ip": egress.dialed_ip,
                "reason": "pre_connect_pinned",
                "enforcement_stage": egress.enforcement_stage,
                "request_forwarded": egress.request_forwarded,
            }
        )
        if egress.http_status in {301, 302, 303, 307, 308}:
            location = egress.headers.get("location", "").strip()
            if location:
                redirect_chain.append(urljoin(normalized.normalized_url, location))
            exc = top_level_navigation_violation(location or normalized.normalized_url)
            record.update(
                {
                    "disposition": "denied",
                    "reason": exc.reason,
                }
            )
            channel_records.append(record)
            await record_violation(exc)
            await route.fulfill(
                status=403,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=exc.reason,
            )
            return
        observed_hosts.add(egress.host)
        resolved_ips.update(egress.approved_ips)
        channel_records.append(record)
        await route.fulfill(
            status=egress.http_status,
            headers=_fulfill_headers(egress.headers),
            body=base64.b64decode(egress.body_base64),
        )

    async def handle_popup(popup):
        exc = popup_violation(popup.url or page.url)
        channel_records.append(
            channel_record(
                channel="popup",
                requested_url=popup.url or page.url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        await record_violation(exc)
        try:
            await popup.close()
        except Exception:
            pass

    async def handle_download(download):
        exc = download_violation(
            page.url or target.normalized_url,
            suggested_filename=getattr(download, "suggested_filename", None),
        )
        channel_records.append(
            channel_record(
                channel="download",
                requested_url=page.url or target.normalized_url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        await record_violation(exc)
        try:
            await download.cancel()
        except Exception:
            pass

    async def handle_filechooser(file_chooser):
        exc = filechooser_violation(file_chooser.page.url or page.url)
        channel_records.append(
            channel_record(
                channel="upload",
                requested_url=file_chooser.page.url or page.url,
                disposition="denied",
                reason=exc.reason,
            )
        )
        await record_violation(exc)

    page.on(
        "popup",
        lambda popup: event_tasks.append(asyncio.create_task(handle_popup(popup))),
    )
    page.on(
        "download",
        lambda download: event_tasks.append(asyncio.create_task(handle_download(download))),
    )
    page.on(
        "filechooser",
        lambda chooser: event_tasks.append(asyncio.create_task(handle_filechooser(chooser))),
    )
    await page.route("**/*", handle_route)

    try:
        response = await page.goto(
            target.normalized_url,
            wait_until="domcontentloaded",
            timeout=int(settings.timeout_seconds * 1000),
        )
        if response is not None:
            http_status = response.status
        if strict_top_level_after_load:
            locked_target = validate_browser_target(
                page.url or target.normalized_url,
                policy=policy,
            )
            locked_main_url = locked_target.normalized_url
            observed_hosts.add(locked_target.host)
        await page.wait_for_timeout(settings.settle_time_ms)
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)

        for js_event in await _extract_js_channel_events(page):
            requested_url = str(js_event.get("requested_url", "")).strip()
            reason = str(js_event.get("reason", "browser_channel_not_allowed"))
            channel = str(js_event.get("channel", "subresource"))
            channel_records.append(
                channel_record(
                    channel=channel,
                    requested_url=requested_url,
                    disposition="denied",
                    reason=reason,
                )
            )
            if violation is None:
                await record_violation(browser_channel_violation(channel, requested_url or reason))

        if violation is not None:
            raise violation

        final_url = page.url or target.normalized_url
        final_target = validate_browser_target(
            final_url,
            policy=policy,
        )
        observed_hosts.add(final_target.host)

        page_title = _limited_text(await page.title(), 256)
        meta_description = await _extract_meta_description(page)
        followable_links: list[BrowserFollowLink] = []
        if include_followable_links:
            followable_links = await _extract_followable_links(
                page,
                base_url=final_target.normalized_url,
                policy=policy,
                max_links=settings.max_followable_links,
            )
        rendered_text, rendered_text_sha256, text_bytes, text_truncated = await _extract_rendered_text(
            page,
            limit_bytes=settings.max_rendered_text_bytes,
        )
        screenshot = await page.screenshot(type="png")
        if len(screenshot) > settings.max_screenshot_bytes:
            raise WebPolicyError(
                "screenshot_too_large",
                f"{len(screenshot)} > {settings.max_screenshot_bytes}",
            )
        screenshot_sha256 = hashlib.sha256(screenshot).hexdigest()
        return BrowserRenderInternalResponse(
            normalized_url=target.normalized_url,
            final_url=final_target.normalized_url,
            http_status=http_status,
            page_title=page_title,
            meta_description=meta_description,
            rendered_text=rendered_text,
            rendered_text_sha256=rendered_text_sha256,
            text_bytes=text_bytes,
            text_truncated=text_truncated,
            screenshot_png_base64=base64.b64encode(screenshot).decode("ascii"),
            screenshot_sha256=screenshot_sha256,
            screenshot_bytes=len(screenshot),
            redirect_chain=list(redirect_chain),
            observed_hosts=sorted(observed_hosts),
            resolved_ips=sorted(resolved_ips),
            channel_records=list(channel_records),
            followable_links=followable_links,
        )
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail=_violation_detail(
                exc=exc,
                normalized_url=target.normalized_url,
                final_url=page.url or target.normalized_url,
                host=target.host,
                redirect_chain=list(redirect_chain),
                observed_hosts=sorted(observed_hosts),
                resolved_ips=sorted(resolved_ips),
                http_status=http_status,
                page_title=page_title,
                text_bytes=text_bytes,
                text_truncated=text_truncated,
                channel_records=list(channel_records),
            ),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        if violation is not None:
            raise HTTPException(
                status_code=_browser_status_code(violation.reason),
                detail=_violation_detail(
                    exc=violation,
                    normalized_url=target.normalized_url,
                    final_url=page.url or target.normalized_url,
                    host=target.host,
                    redirect_chain=list(redirect_chain),
                    observed_hosts=sorted(observed_hosts),
                    resolved_ips=sorted(resolved_ips),
                    http_status=http_status,
                    page_title=page_title,
                    text_bytes=text_bytes,
                    text_truncated=text_truncated,
                    channel_records=list(channel_records),
                ),
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=_error_detail(
                reason=type(exc).__name__,
                detail=str(exc),
                normalized_url=target.normalized_url,
                final_url=page.url or target.normalized_url,
                host=target.host,
                redirect_chain=list(redirect_chain),
                observed_hosts=sorted(observed_hosts),
                resolved_ips=sorted(resolved_ips),
                http_status=http_status,
                channel_records=list(channel_records),
            ),
        ) from exc
    finally:
        await context.close()


def _follow_detail(
    *,
    source_render: BrowserRenderInternalResponse,
    requested_target_url: str,
    matched_link_text: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    normalized_target = detail.get("normalized_url", requested_target_url)
    final_url = detail.get("final_url", normalized_target)
    navigation_history = [source_render.final_url, requested_target_url]
    if final_url and final_url not in navigation_history:
        navigation_history.append(final_url)
    source_channel_records = _plain_channel_records(list(source_render.channel_records))
    target_channel_records = _plain_channel_records(list(detail.get("channel_records", [])))
    return {
        "source_url": source_render.normalized_url,
        "source_final_url": source_render.final_url,
        "requested_target_url": requested_target_url,
        "matched_link_text": matched_link_text,
        "follow_hop_count": 1,
        "navigation_history": navigation_history,
        "normalized_url": normalized_target,
        "final_url": final_url,
        "host": detail.get("host", urlsplit(normalized_target).hostname or ""),
        "allowlist_decision": detail.get("allowlist_decision", "denied"),
        "redirect_chain": list(detail.get("redirect_chain", [])),
        "observed_hosts": list(detail.get("observed_hosts", [])),
        "resolved_ips": list(detail.get("resolved_ips", [])),
        "http_status": detail.get("http_status"),
        "page_title": detail.get("page_title", ""),
        "meta_description": detail.get("meta_description", ""),
        "rendered_text_sha256": detail.get("rendered_text_sha256", ""),
        "text_bytes": int(detail.get("text_bytes", 0)),
        "text_truncated": bool(detail.get("text_truncated", False)),
        "screenshot_sha256": detail.get("screenshot_sha256", ""),
        "screenshot_bytes": int(detail.get("screenshot_bytes", 0)),
        "channel_records": source_channel_records + target_channel_records,
        "reason": detail.get("reason", detail.get("detail", "browser_follow_href_failed")),
    }


async def execute_render(url: str) -> BrowserRenderInternalResponse:
    return await _render_page(url, strict_top_level_after_load=True, include_followable_links=True)


async def execute_follow_href(
    source_url: str,
    target_url: str,
) -> BrowserFollowHrefInternalResponse:
    policy = app.state.policy
    try:
        requested_target = validate_browser_target(target_url, policy)
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail={
                "source_url": source_url,
                "source_final_url": source_url,
                "requested_target_url": target_url,
                "matched_link_text": "",
                "follow_hop_count": 1,
                "navigation_history": [source_url],
                "normalized_url": target_url,
                "final_url": target_url,
                "host": "",
                "allowlist_decision": "denied",
                "redirect_chain": [],
                "observed_hosts": [],
                "resolved_ips": [],
                "http_status": None,
                "page_title": "",
                "meta_description": "",
                "rendered_text_sha256": "",
                "text_bytes": 0,
                "text_truncated": False,
                "screenshot_sha256": "",
                "screenshot_bytes": 0,
                "channel_records": [],
                "reason": exc.reason,
                "detail": exc.detail,
            },
        ) from exc
    source_render = await execute_render(source_url)
    try:
        matched_link = select_followable_link(
            requested_target.normalized_url,
            source_render.followable_links,
        )
    except WebPolicyError as exc:
        raise HTTPException(
            status_code=_browser_status_code(exc.reason),
            detail=_follow_detail(
                source_render=source_render,
                requested_target_url=requested_target.normalized_url,
                matched_link_text="",
                detail={
                    "normalized_url": requested_target.normalized_url,
                    "final_url": requested_target.normalized_url,
                    "host": requested_target.host,
                    "allowlist_decision": "denied",
                    "redirect_chain": [],
                    "observed_hosts": list(source_render.observed_hosts),
                    "resolved_ips": list(source_render.resolved_ips),
                    "http_status": None,
                    "page_title": "",
                    "meta_description": "",
                    "rendered_text_sha256": "",
                    "text_bytes": 0,
                    "text_truncated": False,
                    "screenshot_sha256": "",
                    "screenshot_bytes": 0,
                    "channel_records": _plain_channel_records(list(source_render.channel_records)),
                    "reason": exc.reason,
                    "detail": exc.detail,
                },
            ),
        ) from exc

    try:
        target_render = await _render_page(
            matched_link.target_url,
            strict_top_level_after_load=True,
            include_followable_links=False,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"reason": str(exc.detail)}
        raise HTTPException(
            status_code=exc.status_code,
            detail=_follow_detail(
                source_render=source_render,
                requested_target_url=matched_link.target_url,
                matched_link_text=matched_link.text,
                detail=detail,
            ),
        ) from exc

    navigation_history = [source_render.final_url, matched_link.target_url]
    if target_render.final_url not in navigation_history:
        navigation_history.append(target_render.final_url)

    return BrowserFollowHrefInternalResponse(
        source_url=source_render.normalized_url,
        source_final_url=source_render.final_url,
        requested_target_url=matched_link.target_url,
        matched_link_text=matched_link.text,
        follow_hop_count=1,
        navigation_history=navigation_history,
        normalized_url=target_render.normalized_url,
        final_url=target_render.final_url,
        http_status=target_render.http_status,
        page_title=target_render.page_title,
        meta_description=target_render.meta_description,
        rendered_text=target_render.rendered_text,
        rendered_text_sha256=target_render.rendered_text_sha256,
        text_bytes=target_render.text_bytes,
        text_truncated=target_render.text_truncated,
        screenshot_png_base64=target_render.screenshot_png_base64,
        screenshot_sha256=target_render.screenshot_sha256,
        screenshot_bytes=target_render.screenshot_bytes,
        redirect_chain=list(target_render.redirect_chain),
        observed_hosts=sorted(
            set(source_render.observed_hosts) | set(target_render.observed_hosts)
        ),
        resolved_ips=sorted(set(source_render.resolved_ips) | set(target_render.resolved_ips)),
        channel_records=_plain_channel_records(list(source_render.channel_records))
        + _plain_channel_records(list(target_render.channel_records)),
    )


def startup_checks(app: FastAPI):
    settings = browser_settings()
    app.state.settings = settings
    app.state.policy = build_policy()


async def _launch_browser_runtime():
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(**browser_launch_kwargs())
    return playwright, browser


async def _session_cleanup_loop(session_manager: BrowserSessionManager):
    while True:
        await asyncio.sleep(5.0)
        await session_manager.prune_expired()


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_checks(app)
    playwright, browser = await _launch_browser_runtime()
    egress_client = httpx.AsyncClient(
        base_url=app.state.settings.egress_url,
        timeout=app.state.settings.timeout_seconds + 1.0,
        trust_env=False,
    )
    app.state.playwright = playwright
    app.state.browser = browser
    app.state.egress_client = egress_client
    app.state.session_manager = BrowserSessionManager(
        browser=browser,
        egress_client=egress_client,
        policy=app.state.policy,
        settings=app.state.settings,
    )
    cleanup_task = asyncio.create_task(_session_cleanup_loop(app.state.session_manager))
    try:
        yield
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await app.state.session_manager.close_all()
        await egress_client.aclose()
        await browser.close()
        await playwright.stop()


app = FastAPI(title="trusted-browser", lifespan=lifespan)


@app.get("/healthz", response_model=HealthReport)
async def healthz() -> HealthReport:
    settings = app.state.settings
    launch_kwargs = browser_launch_kwargs()
    return HealthReport(
        service=settings.service_name,
        status="ok",
        stage=settings.stage,
        details={
            "allowlist_hosts": list(settings.allowlist_hosts),
            "private_test_hosts": list(settings.private_test_hosts),
            "max_redirects": settings.max_redirects,
            "timeout_seconds": settings.timeout_seconds,
            "viewport_width": settings.viewport_width,
            "viewport_height": settings.viewport_height,
            "settle_time_ms": settings.settle_time_ms,
            "max_rendered_text_bytes": settings.max_rendered_text_bytes,
            "max_screenshot_bytes": settings.max_screenshot_bytes,
            "max_followable_links": settings.max_followable_links,
            "max_follow_hops": settings.max_follow_hops,
            "session_max_concurrent": settings.session_max_concurrent,
            "session_ttl_seconds": settings.session_ttl_seconds,
            "egress_url": settings.egress_url,
            "running_as_root": os.geteuid() == 0,
            "chromium_sandbox": bool(launch_kwargs["chromium_sandbox"]),
            "launch_args": list(launch_kwargs["args"]),
        },
    )


@app.post("/internal/render", response_model=BrowserRenderInternalResponse)
async def render(payload: BrowserRenderRequest) -> BrowserRenderInternalResponse:
    return await execute_render(payload.url)


@app.post("/internal/follow-href", response_model=BrowserFollowHrefInternalResponse)
async def follow_href(payload: BrowserFollowHrefRequest) -> BrowserFollowHrefInternalResponse:
    return await execute_follow_href(payload.source_url, payload.target_url)


@app.post("/internal/sessions/open", response_model=BrowserSessionSnapshotInternalResponse)
async def open_session(payload: BrowserSessionOpenRequest) -> BrowserSessionSnapshotInternalResponse:
    return await app.state.session_manager.open_session(payload.url)


@app.get("/internal/sessions/{session_id}", response_model=BrowserSessionSnapshotInternalResponse)
async def session_snapshot(session_id: str) -> BrowserSessionSnapshotInternalResponse:
    return await app.state.session_manager.snapshot(session_id)


@app.post("/internal/sessions/{session_id}/click", response_model=BrowserSessionSnapshotInternalResponse)
async def session_click(
    session_id: str,
    payload: BrowserSessionClickRequest,
) -> BrowserSessionSnapshotInternalResponse:
    return await app.state.session_manager.click(session_id, payload)


@app.post("/internal/sessions/{session_id}/type", response_model=BrowserSessionSnapshotInternalResponse)
async def session_type(
    session_id: str,
    payload: BrowserSessionTypeRequest,
) -> BrowserSessionSnapshotInternalResponse:
    return await app.state.session_manager.type_text(session_id, payload)


@app.post("/internal/sessions/{session_id}/select", response_model=BrowserSessionSnapshotInternalResponse)
async def session_select(
    session_id: str,
    payload: BrowserSessionSelectRequest,
) -> BrowserSessionSnapshotInternalResponse:
    return await app.state.session_manager.select_value(session_id, payload)


@app.post("/internal/sessions/{session_id}/set-checked", response_model=BrowserSessionSnapshotInternalResponse)
async def session_set_checked(
    session_id: str,
    payload: BrowserSessionSetCheckedRequest,
) -> BrowserSessionSnapshotInternalResponse:
    return await app.state.session_manager.set_checked(session_id, payload)


@app.post("/internal/sessions/{session_id}/prepare-submit", response_model=BrowserSubmitPreviewInternalResponse)
async def prepare_submit(
    session_id: str,
    payload: BrowserSubmitProposalRequest,
) -> BrowserSubmitPreviewInternalResponse:
    return await app.state.session_manager.prepare_submit(session_id, payload)


@app.post("/internal/sessions/{session_id}/execute-submit", response_model=BrowserSubmitExecuteInternalResponse)
async def execute_submit(
    session_id: str,
    payload: BrowserSubmitExecuteRequest,
) -> BrowserSubmitExecuteInternalResponse:
    return await app.state.session_manager.execute_submit(session_id, payload)
