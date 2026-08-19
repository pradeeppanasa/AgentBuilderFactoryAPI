"""Error message mapping (Observability — Runs Feature, Phase 2, Section 6).

Never show a raw AWS exception name or stack trace to an end user — map it
to a business-first reason and a recommended action first; the raw code is
still kept (in StepError.raw_error_code), but only inside the collapsed
"Technical details" section.
"""

from __future__ import annotations

import re

_ERROR_MESSAGE_MAP: dict[str, tuple[str, str]] = {
    "UnrecognizedClientException": (
        "AWS credentials are invalid or not configured.",
        "Verify Bedrock IAM permissions and confirm the model is enabled in the "
        "customer's AWS account and region.",
    ),
    "ResourceNotFoundException": (
        "The model or guardrail was not found in this region.",
        "Confirm the model ID and guardrail are correct and available in the "
        "deployed region.",
    ),
    "ThrottlingException": (
        "Request rate limit exceeded.",
        "Retry in a few seconds. If this recurs, request a Bedrock quota increase.",
    ),
    "ValidationException": (
        "The request payload was invalid.",
        "Check the agent configuration — a field is likely missing or malformed.",
    ),
    "ServiceUnavailableException": (
        "AWS service is temporarily unavailable.",
        "Retry shortly. If this persists, check the AWS Service Health Dashboard.",
    ),
    "ConnectTimeoutError": (
        "Could not reach the tool endpoint.",
        "Check network connectivity and the tool endpoint's availability.",
    ),
}

_DEFAULT_REASON = "An unexpected error occurred."
_DEFAULT_ACTION = "Reference the error ID when contacting support."


def map_error(raw_error_code: str) -> tuple[str, str]:
    """Returns (business_reason, recommended_action) for a raw error code.
    Unknown codes get the generic default — never a bare passthrough of the
    raw code as if it were already a business-readable message."""
    return _ERROR_MESSAGE_MAP.get(raw_error_code, (_DEFAULT_REASON, _DEFAULT_ACTION))


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")
_CATEGORY_SUFFIXES = ("_exception", "_error")


def error_category(raw_error_code: str) -> str:
    """Short, stable slug derived from a raw AWS error code (CLAUDE.md
    Section 40.4) — e.g. "UnrecognizedClientException" -> "unrecognized_client".
    Used only for RunRecord.error_category (filtering/grouping runs by
    failure type). Never the user-facing message — that's map_error()'s
    business_reason above."""
    slug = _CAMEL_BOUNDARY.sub("_", raw_error_code).lower()
    for suffix in _CATEGORY_SUFFIXES:
        if slug.endswith(suffix):
            return slug[: -len(suffix)]
    return slug
