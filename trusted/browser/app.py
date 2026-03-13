from contextlib import asynccontextmanager
import asyncio
import base64
import hashlib
from typing import Any

from fastapi import FastAPI, HTTPException

from shared.config import browser_settings
from shared.schemas import BrowserRenderInternalResponse, BrowserRenderRequest, HealthReport
from trusted.browser.policy import (
    download_violation,
    popup_violation,
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


async def execute_render(url: str) -> BrowserRenderInternalResponse:
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
        await page.wait_for_timeout(settings.settle_time_ms)
        if event_tasks:
            await asyncio.gather(*event_tasks, return_exceptions=True)

        final_url = page.url or target.normalized_url
        final_target = validate_browser_target(final_url, policy)
        final_ips = validate_resolved_ips(final_target, resolve_target_ips(final_target), policy)
        observed_hosts.add(final_target.host)
        resolved_ips.update(final_ips)

        if violation is not None:
            raise violation

        page_title = _limited_text(await page.title(), 256)
        meta_description = await _extract_meta_description(page)
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


def startup_checks(app: FastAPI):
    settings = browser_settings()
    app.state.settings = settings
    app.state.policy = build_policy()


async def _launch_browser_runtime():
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--no-sandbox",
        ],
    )
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
        },
    )


@app.post("/internal/render", response_model=BrowserRenderInternalResponse)
async def render(payload: BrowserRenderRequest) -> BrowserRenderInternalResponse:
    return await execute_render(payload.url)
