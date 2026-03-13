import pytest

from trusted.fetcher.app import build_policy as build_fetch_policy
from trusted.web.policy import (
    WebPolicyError,
    normalize_web_target,
    normalize_web_redirect_target,
)


def test_fetcher_and_browser_reuse_same_shared_policy(monkeypatch):
    monkeypatch.setenv("RSI_WEB_ALLOWLIST_HOSTS", "allowed.test")
    monkeypatch.setenv("RSI_FETCH_ALLOW_PRIVATE_TEST_HOSTS", "allowed.test")

    from trusted.browser.app import build_policy as build_browser_policy

    fetch_policy = build_fetch_policy()
    browser_policy = build_browser_policy()

    assert type(fetch_policy) is type(browser_policy)
    assert fetch_policy.allowlist_hosts == ("allowed.test",)
    assert browser_policy.allowlist_hosts == ("allowed.test",)
    assert fetch_policy.max_redirects == browser_policy.max_redirects


def test_shared_policy_blocks_hosts_redirects_and_private_ips():
    policy = build_fetch_policy()

    with pytest.raises(WebPolicyError, match="host_not_allowlisted"):
        normalize_web_target("http://blocked.test/", policy)

    start = normalize_web_target("http://example.com/", policy)
    with pytest.raises(WebPolicyError, match="host_not_allowlisted"):
        normalize_web_redirect_target(
            "http://blocked.test/redirected",
            current_url=start.normalized_url,
            policy=policy,
        )

    with pytest.raises(WebPolicyError, match="blocked_hostname"):
        normalize_web_target("http://localhost/secret", policy)


def test_browser_policy_denies_popup_download_and_local_file():
    from trusted.browser.policy import (
        download_violation,
        popup_violation,
        validate_browser_target,
    )

    policy = build_fetch_policy()

    with pytest.raises(WebPolicyError, match="unsupported_scheme"):
        validate_browser_target("file:///etc/passwd", policy)

    popup_error = popup_violation("http://allowed.test/popup")
    assert popup_error.reason == "popup_not_allowed"

    download_error = download_violation(
        "http://allowed.test/download.bin",
        suggested_filename="download.bin",
    )
    assert download_error.reason == "download_not_allowed"
