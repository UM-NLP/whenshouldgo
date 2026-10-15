"""Typed discovery failures. Messages are static and server-authored: they never carry tool
results, provider text, arguments, URLs or credentials, and they are raised outside
``except`` blocks so no exception context can hold such content either."""
from __future__ import annotations

from enum import StrEnum

from ..domain import ErrorCode


class RejectionReason(StrEnum):
    """Why the validator rejected a whole grounded block."""

    FORMAT = "FORMAT"
    CLASS = "CLASS"
    STORAGE = "STORAGE"
    SIZE = "SIZE"
    HASH = "HASH"
    URI = "URI"
    CACHE_EXPIRY = "CACHE_EXPIRY"
    PROVENANCE = "PROVENANCE"


class DiscoveryError(Exception):
    code: ErrorCode = ErrorCode.PROVIDER
    retryable = False
    default_message = "discovery failure"

    def __init__(self, message: str | None = None, *, calls: int = 0) -> None:
        super().__init__(message or self.default_message)
        # Provider calls actually sent for this failure (0 means nothing went out).
        self.calls = calls
        self.call_id: str | None = None  # run-local tool_call_id, for evidence references


class DiscoveryConfigError(DiscoveryError):
    default_message = "discovery configuration is invalid"


class DiscoveryDisabledError(DiscoveryError):
    default_message = "discovery is switched off"


class ToolNotAllowed(DiscoveryError):
    code = ErrorCode.POLICY
    default_message = "tool is not in the discovery allowlist"


class InvalidToolArguments(DiscoveryError):
    code = ErrorCode.POLICY
    default_message = "tool arguments are invalid"


class ToolBudgetExceeded(DiscoveryError):
    code = ErrorCode.QUOTA
    default_message = "run tool-call budget exhausted"


class DiscoveryRateLimited(DiscoveryError):
    retryable = True
    default_message = "discovery provider rate limit"


class DiscoveryAccessDenied(DiscoveryError):
    default_message = "discovery provider refused the credentials or billing"


class DiscoveryTimeout(DiscoveryError):
    retryable = True
    default_message = "discovery provider timed out"


class DiscoveryUnavailable(DiscoveryError):
    retryable = True
    default_message = "discovery provider unavailable"


class MalformedToolResult(DiscoveryError):
    code = ErrorCode.VALIDATION
    default_message = "tool result is malformed"


class DiscoveryProtocolError(MalformedToolResult):
    """The reply was a JSON-RPC error object. Its message is untrusted and dropped."""

    default_message = "discovery protocol error"


class DiscoveryToolError(MalformedToolResult):
    """The tool reported ``isError``. Its text is untrusted and dropped."""

    default_message = "discovery tool reported an error"


class DiscoveryBadRequest(DiscoveryError):
    default_message = "discovery provider rejected the request"


class DiscoverySessionRequired(DiscoveryError):
    """HTTP 404 (or a session-style refusal). TODO(verify-live): the docs describe no
    initialize handshake or session id, but the MCP spec requires one. Fail closed."""

    default_message = "discovery provider wants a session; the stateless path is refused"


class OversizeResult(DiscoveryError):
    code = ErrorCode.VALIDATION
    default_message = "tool result is too large"


class GroundedAnswerRejected(DiscoveryError):
    """The validator refused the whole block. It never edits, trims or redacts."""

    code = ErrorCode.VALIDATION
    default_message = "grounded answer rejected"

    def __init__(self, reason: RejectionReason) -> None:
        super().__init__()
        self.reason = reason


class CacheError(DiscoveryError):
    code = ErrorCode.VALIDATION
    default_message = "display cache refused the entry"
