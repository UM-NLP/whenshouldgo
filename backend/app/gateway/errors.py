"""Typed gateway failures. Messages are static and server-authored: they never carry
prompt text, provider output, validation details that echo input, or credentials.

Each error maps onto an existing ``ErrorCode`` so a caller can surface it without a new
API contract. ``usage`` is what the caller must charge against the run budget; it is the
provider-reported usage when trustworthy, otherwise the worst-case reservation.
"""
from __future__ import annotations

from enum import StrEnum

from ..domain import ErrorCode
from .types import Usage


class GatewayErrorKind(StrEnum):
    CONFIG = "CONFIG"
    PRIVACY_VIOLATION = "PRIVACY_VIOLATION"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    TAINTED_CONTEXT = "TAINTED_CONTEXT"
    BUDGET = "BUDGET"
    RATE_LIMIT = "RATE_LIMIT"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    TRUNCATION = "TRUNCATION"
    CONTENT_FILTER = "CONTENT_FILTER"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    USAGE_MISMATCH = "USAGE_MISMATCH"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class GatewayError(Exception):
    kind: GatewayErrorKind = GatewayErrorKind.CONFIG
    code: ErrorCode = ErrorCode.PROVIDER
    retryable: bool = False
    default_message = "model gateway failure"
    # True only when the provider said (HTTP 429 or 503) it rejected the request before
    # processing it. Such a call costs no tokens, but it still counts as a call.
    rejected_before_processing = False

    def __init__(
        self, message: str | None = None, *, usage: Usage | None = None, calls: int = 0
    ) -> None:
        super().__init__(message or self.default_message)
        self.usage = usage
        # Provider calls actually sent (including any repair). 0 means nothing was sent.
        self.calls = calls


class GatewayConfigError(GatewayError):
    kind = GatewayErrorKind.CONFIG
    default_message = "gateway configuration is invalid"


class PrivacyViolationError(GatewayError):
    kind = GatewayErrorKind.PRIVACY_VIOLATION
    code = ErrorCode.POLICY
    default_message = "privacy parameters are missing or weakened"


class ToolNotAllowedError(GatewayError):
    kind = GatewayErrorKind.TOOL_NOT_ALLOWED
    code = ErrorCode.POLICY
    default_message = "tool is not in the server allowlist"


class TaintedContextError(GatewayError):
    kind = GatewayErrorKind.TAINTED_CONTEXT
    code = ErrorCode.POLICY
    default_message = "context content class is not permitted for this call"


class BudgetExceededError(GatewayError):
    kind = GatewayErrorKind.BUDGET
    code = ErrorCode.QUOTA
    default_message = "run budget would be exceeded"


class ProviderRateLimitError(GatewayError):
    kind = GatewayErrorKind.RATE_LIMIT
    retryable = True
    default_message = "provider rate limit"


class ProviderQuotaError(GatewayError):
    kind = GatewayErrorKind.PROVIDER_QUOTA
    default_message = "provider credit or quota exhausted"


class ProviderTimeoutError(GatewayError):
    kind = GatewayErrorKind.TIMEOUT
    retryable = True
    default_message = "provider timed out"


class ProviderUnavailableError(GatewayError):
    kind = GatewayErrorKind.PROVIDER_UNAVAILABLE
    retryable = True
    default_message = "provider unavailable"


class ProviderRejectedError(GatewayError):
    """A non-retryable provider-side failure (bad request, auth, unknown finish reason)."""

    kind = GatewayErrorKind.PROVIDER_UNAVAILABLE
    default_message = "provider rejected the request"


class TruncationError(GatewayError):
    kind = GatewayErrorKind.TRUNCATION
    code = ErrorCode.VALIDATION
    default_message = "provider output was truncated"


class ContentFilterError(GatewayError):
    kind = GatewayErrorKind.CONTENT_FILTER
    code = ErrorCode.VALIDATION
    default_message = "provider content filter stopped the output"


class MalformedOutputError(GatewayError):
    kind = GatewayErrorKind.MALFORMED_OUTPUT
    code = ErrorCode.VALIDATION
    default_message = "provider output is malformed or violates the schema"


class UsageMismatchError(GatewayError):
    kind = GatewayErrorKind.USAGE_MISMATCH
    default_message = "provider usage metadata is missing or inconsistent"


class CircuitOpenError(GatewayError):
    kind = GatewayErrorKind.CIRCUIT_OPEN
    default_message = "provider circuit breaker is open"


class TransportTimeout(Exception):
    """Raised by a transport when the HTTP exchange timed out."""


class TransportFailure(Exception):
    """Raised by a transport for connection-level failures. Carries no provider text."""
