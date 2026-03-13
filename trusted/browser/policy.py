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
