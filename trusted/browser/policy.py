from shared.schemas import BrowserFollowLink
from trusted.web.policy import WebPolicy, WebPolicyError, normalize_web_target


def validate_browser_target(url: str, policy: WebPolicy):
    return normalize_web_target(url, policy)


def popup_violation(url: str) -> WebPolicyError:
    return WebPolicyError("popup_not_allowed", url or "about:blank")


def download_violation(url: str, *, suggested_filename: str | None) -> WebPolicyError:
    detail = url or "download"
    if suggested_filename:
        detail = f"{detail} -> {suggested_filename}"
    return WebPolicyError("download_not_allowed", detail)


def top_level_navigation_violation(url: str) -> WebPolicyError:
    return WebPolicyError("top_level_navigation_not_allowed", url or "about:blank")


def select_followable_link(
    target_url: str,
    followable_links: list[BrowserFollowLink],
) -> BrowserFollowLink:
    for link in followable_links:
        if link.target_url == target_url:
            return link
    raise WebPolicyError("requested_target_not_present", target_url)
