from contextlib import asynccontextmanager
import asyncio
import base64
import hashlib
import os
from typing import Any
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI, HTTPException

from shared.config import browser_settings
from shared.schemas import (
    BrowserFollowHrefInternalResponse,
    BrowserFollowHrefRequest,
    BrowserFollowLink,
    BrowserRenderInternalResponse,
    BrowserRenderRequest,
    HealthReport,
)
from trusted.browser.policy import (
    download_violation,
    popup_violation,
    select_followable_link,
    top_level_navigation_violation,
    validate_browser_target,
)
from trusted.web.policy import (
    WebPolicy,
    WebPolicyError,
    resolve_target_ips,
    validate_resolved_ips,
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
    }


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


async def _render_page(
    url: str,
    *,
    strict_top_level_after_load: bool = False,
    include_followable_links: bool = True,
) -> BrowserRenderInternalResponse:
    settings = app.state.settings
    policy = app.state.policy
    target = validate_browser_target(url, policy)
    initial_ips = validate_resolved_ips(target, resolve_target_ips(target), policy)

    redirect_chain: list[str] = []
    observed_hosts = {target.host}
    resolved_ips = set(initial_ips)
    violation: WebPolicyError | None = None
    http_status: int | None = None
    page_title = ""
    text_bytes = 0
    text_truncated = False
    event_tasks: list[asyncio.Task] = []
    locked_main_url: str | None = None

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

    async def record_violation(exc: WebPolicyError):
        nonlocal violation
        if violation is None:
            violation = exc

    async def handle_route(route):
        request = route.request
        request_url = request.url
        try:
            request_target = validate_browser_target(request_url, policy)
            request_ips = validate_resolved_ips(
                request_target,
                resolve_target_ips(request_target),
                policy,
            )
        except WebPolicyError as exc:
            await record_violation(exc)
            await route.abort("blockedbyclient")
            return

        observed_hosts.add(request_target.host)
        resolved_ips.update(request_ips)
        if request.is_navigation_request() and request.frame == page.main_frame:
            if locked_main_url is not None and request_target.normalized_url != locked_main_url:
                await record_violation(top_level_navigation_violation(request_target.normalized_url))
                await route.abort("blockedbyclient")
                return
        if request.is_navigation_request() and request_target.normalized_url != target.normalized_url:
            if len(redirect_chain) >= policy.max_redirects:
                await record_violation(
                    WebPolicyError(
                        "too_many_redirects",
                        request_target.normalized_url,
                    )
                )
                await route.abort("blockedbyclient")
                return
            if request_target.normalized_url not in redirect_chain:
                redirect_chain.append(request_target.normalized_url)
        await route.continue_()

    async def handle_popup(popup):
        await record_violation(popup_violation(popup.url or page.url))
        try:
            await popup.close()
        except Exception:
            pass

    async def handle_download(download):
        await record_violation(
            download_violation(
                page.url or target.normalized_url,
                suggested_filename=getattr(download, "suggested_filename", None),
            )
        )
        try:
            await download.cancel()
        except Exception:
            pass

    page.on(
        "popup",
        lambda popup: event_tasks.append(asyncio.create_task(handle_popup(popup))),
    )
    page.on(
        "download",
        lambda download: event_tasks.append(asyncio.create_task(handle_download(download))),
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
            locked_target = validate_browser_target(page.url or target.normalized_url, policy)
            locked_ips = validate_resolved_ips(
                locked_target,
                resolve_target_ips(locked_target),
                policy,
            )
            locked_main_url = locked_target.normalized_url
            observed_hosts.add(locked_target.host)
            resolved_ips.update(locked_ips)
        await page.wait_for_timeout(settings.settle_time_ms)
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)

        if violation is not None:
            raise violation

        final_url = page.url or target.normalized_url
        final_target = validate_browser_target(final_url, policy)
        final_ips = validate_resolved_ips(final_target, resolve_target_ips(final_target), policy)
        observed_hosts.add(final_target.host)
        resolved_ips.update(final_ips)

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
        "reason": detail.get("reason", detail.get("detail", "browser_follow_href_failed")),
    }


async def execute_render(url: str) -> BrowserRenderInternalResponse:
    return await _render_page(url, strict_top_level_after_load=False, include_followable_links=True)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_checks(app)
    playwright, browser = await _launch_browser_runtime()
    app.state.playwright = playwright
    app.state.browser = browser
    try:
        yield
    finally:
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
