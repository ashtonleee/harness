from trusted.web.policy import (
    NormalizedWebTarget as NormalizedFetchTarget,
    WebPolicy as FetchPolicy,
    WebPolicyError as FetchPolicyError,
    normalize_web_redirect_target as normalize_redirect_target,
    normalize_web_target as normalize_fetch_target,
    resolve_target_ips,
    validate_resolved_ips,
)


def content_type_allowed(content_type: str, policy: FetchPolicy) -> bool:
    allowed_types = getattr(policy, "allowed_content_types", ())
    value = content_type.split(";", 1)[0].strip().lower()
    return value in allowed_types
