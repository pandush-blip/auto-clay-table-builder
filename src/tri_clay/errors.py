"""Typed errors for tri-clay."""

from __future__ import annotations

import re


# Clay's workspace limits and flaky infrastructure produce these; they are worth retrying,
# never worth failing a table/job over.
TRANSIENT_ERROR = re.compile(
    r"too many concurrent|too many requests|\b429\b|rate limit|try again shortly|"
    r"timed out|timeout|gateway time-out|bad gateway|service unavailable|"
    r"\b50[234]\b|temporarily unavailable|connection reset|connection aborted|"
    r"read timeout|remotedisconnected",
    re.I,
)


def is_transient_error(error: object) -> bool:
    return bool(TRANSIENT_ERROR.search(str(error or "")))


def summarize_clay_error(error: object) -> str:
    """Turn Clay's error blobs into one readable line (prefers `statusMessage`)."""
    text = str(error or "")
    match = re.search(r"'statusMessage':\s*'([^']+)'", text) or re.search(
        r'"statusMessage":\s*"([^"]+)"', text
    )
    if match:
        return match.group(1).strip()
    match = re.search(r'"message":\s*"([^"]+)"', text)
    if match:
        return match.group(1).strip()
    if "'ERROR'" in text or '"ERROR"' in text:
        return "Clay run ended with ERROR"
    return text.strip()[:300]


class TriClayError(Exception):
    """Base class for all tri-clay errors."""


class ConfigError(TriClayError):
    """Missing or invalid configuration."""


class EndpointNotAllowed(TriClayError):
    """A request was attempted against an endpoint outside the allowlist."""


class PayloadNotAllowed(TriClayError):
    """A request payload contains fields/types that could spend credits."""


class CreditSpentError(TriClayError):
    """The credit tripwire detected a balance change."""


class SessionExpiredError(TriClayError):
    """The Clay web-app session cookie is missing, expired or rejected."""


class OfficialApiError(TriClayError):
    """The official Public API returned an unexpected error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class InputError(TriClayError):
    """The audiences CSV is malformed."""
