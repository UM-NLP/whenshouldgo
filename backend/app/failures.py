"""Typed turn failures that the HTTP layer turns into an ``ErrorBody`` (code, reason, message).

Messages are static and server-authored: they never carry model output, tool output, provider
text, arguments or user text, and failures are raised outside ``except`` blocks so no exception
context can hold such content either.
"""
from __future__ import annotations

from .domain import ErrorCode, ErrorReason

_MESSAGES: dict[ErrorReason, str] = {
    # Stable client contract (docs: see TurnResponse.discovery). A discovery turn is replayed
    # from the display cache; once that entry has expired or been evicted the original block can
    # no longer be shown, and the server will not call Google again under a used key.
    ErrorReason.RESULT_EXPIRED: "result expired, retry with a new idempotency key",
    ErrorReason.TURN_IN_PROGRESS: "another request for this account is still running; retry",
    ErrorReason.SERVER_NOT_CONFIGURED: "the assistant is not available on this server",
    ErrorReason.PROVIDER_RATE_LIMITED: "the service is busy; try again shortly",
    ErrorReason.PROVIDER_TIMEOUT: "the request took too long; try again",
    ErrorReason.PROVIDER_UNAVAILABLE: "the service is temporarily unavailable",
    ErrorReason.OUTPUT_TRUNCATED: "the assistant could not finish; try a shorter request",
    ErrorReason.OUTPUT_MALFORMED: "the assistant produced an unusable answer; try again",
    ErrorReason.OVER_BUDGET: "this request exceeded its processing budget; try a shorter request",
    ErrorReason.PATCH_REJECTED: "the proposed update was refused; nothing was changed",
    ErrorReason.TOOL_NOT_ALLOWED: "the requested lookup is not available",
    ErrorReason.TOOL_RESULT_INVALID: "the lookup result could not be used; try again",
    ErrorReason.DISCOVERY_UNAVAILABLE: "place search is temporarily unavailable",
    ErrorReason.DISCOVERY_NOT_CONFIGURED: "place search is not available",
}
GENERIC_MESSAGE = "the server could not complete this request"


class TurnFailure(Exception):
    """A turn that failed with a typed error: the reservation is released, no state changed."""

    def __init__(self, status: int, code: ErrorCode, reason: ErrorReason | None) -> None:
        """``reason=None`` is for faults a client must not learn the cause of (a server-side
        policy or configuration fault): a generic message and no reason in the body."""
        super().__init__(_MESSAGES[reason] if reason else GENERIC_MESSAGE)
        self.status = status
        self.code = code
        self.reason = reason

    @property
    def message(self) -> str:
        return _MESSAGES[self.reason] if self.reason else GENERIC_MESSAGE


class ResultExpired(TurnFailure):
    """Replay of a discovery turn whose cached block expired, was evicted or no longer matches
    the hashes recorded at validation. Stable code: CONFLICT / RESULT_EXPIRED (HTTP 409)."""

    def __init__(self) -> None:
        super().__init__(409, ErrorCode.CONFLICT, ErrorReason.RESULT_EXPIRED)


class MapsContentToModelNotImplemented(NotImplementedError):
    """``RuntimeConfig.maps_content_to_model`` is True, but sending Maps content to the model
    is not implemented in Phase 0 (decision 17, legal open question 2)."""

    def __init__(self) -> None:
        super().__init__("sending Maps content to the model is not implemented in Phase 0")
