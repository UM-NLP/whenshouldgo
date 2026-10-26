"""Typed Places/Routes failures. Messages are static and server-authored: they never carry a
provider payload, a place name, an address, a coordinate, a URL or a credential, and every error
is raised outside ``except`` blocks so no exception context holds such content either."""
from __future__ import annotations

from ..domain import ErrorCode, ErrorReason
from ..telemetry import ToolOutcome


class PlacesError(Exception):
    status = 502
    code: ErrorCode = ErrorCode.PROVIDER
    reason: ErrorReason | None = None
    outcome: ToolOutcome = ToolOutcome.ERROR
    default_message = "places failure"
    # True when the provider (our side of the line) failed, so the spend is not the user's.
    provider_side = True

    def __init__(self, message: str | None = None, *, calls: int = 0,
                 retry_after: int | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.calls = calls  # provider requests actually sent for this failure
        self.retry_after = retry_after


class PlacesConfigError(PlacesError):
    default_message = "places configuration is invalid"
    status = 503
    reason = ErrorReason.SERVER_NOT_CONFIGURED
    outcome = ToolOutcome.DENIED


class PlacesNotConfigured(PlacesConfigError):
    default_message = "place details are not available"


class PlacesDisabled(PlacesError):
    """Kill switch tripped: nothing is sent."""

    status = 503
    reason = ErrorReason.DISCOVERY_UNAVAILABLE
    outcome = ToolOutcome.DENIED
    default_message = "place details are switched off"


class PlacesInvalidRequest(PlacesError):
    status = 422
    code = ErrorCode.VALIDATION
    outcome = ToolOutcome.DENIED
    default_message = "request failed validation"
    provider_side = False


class LocationRequired(PlacesInvalidRequest):
    default_message = "A current location is needed for a route check. Share your location and try again."


class PlacesBudgetExceeded(PlacesError):
    status = 429
    code = ErrorCode.QUOTA
    reason = ErrorReason.OVER_BUDGET
    outcome = ToolOutcome.DENIED
    default_message = "request tool-call budget exhausted"
    provider_side = False


class PlacesRateLimited(PlacesError):
    """Our own per-user daily call or cost ceiling."""

    status = 429
    code = ErrorCode.QUOTA
    reason = ErrorReason.RATE_LIMITED
    outcome = ToolOutcome.DENIED
    default_message = "daily place lookup limit reached"
    provider_side = False


class PlacesNotFound(PlacesError):
    status = 404
    code = ErrorCode.VALIDATION
    default_message = "place not found"
    provider_side = False


class PlacesProviderRateLimited(PlacesError):
    status = 503
    reason = ErrorReason.PROVIDER_RATE_LIMITED
    default_message = "place provider is busy"


class PlacesAccessDenied(PlacesError):
    status = 503
    reason = ErrorReason.PROVIDER_UNAVAILABLE
    default_message = "place provider refused the credentials or billing"
    # 401 and 402 mean OUR key or billing is broken (global): trip the kill switch at once. A 403
    # needs a streak (one tenant's request must not switch the feature off for everyone).
    trips_at_once = False


class PlacesBadRequest(PlacesError):
    status = 502
    code = ErrorCode.VALIDATION
    reason = ErrorReason.OUTPUT_REJECTED
    default_message = "place provider rejected the request"


class PlacesTimeout(PlacesError):
    status = 504
    reason = ErrorReason.PROVIDER_TIMEOUT
    outcome = ToolOutcome.TIMEOUT
    default_message = "place provider timed out"


class PlacesUnavailable(PlacesError):
    status = 503
    reason = ErrorReason.PROVIDER_UNAVAILABLE
    default_message = "place provider unavailable"


class MalformedPlacesResult(PlacesError):
    status = 502
    code = ErrorCode.VALIDATION
    reason = ErrorReason.OUTPUT_REJECTED
    outcome = ToolOutcome.REJECTED
    default_message = "place provider result is malformed"


class OversizePlacesResult(MalformedPlacesResult):
    default_message = "place provider result is too large"


def error_for_status(status: int) -> PlacesError | None:
    """The typed failure for a non-2xx provider status (None for 2xx)."""
    if 200 <= status < 300:
        return None
    if status == 404:
        return PlacesNotFound(calls=1)
    if status == 429:
        return PlacesProviderRateLimited(calls=1, retry_after=30)
    if status in (401, 402, 403):
        denied = PlacesAccessDenied(calls=1)
        denied.trips_at_once = status != 403
        return denied
    if status in (400, 405, 409, 413, 422):
        return PlacesBadRequest(calls=1)
    return PlacesUnavailable(calls=1)
