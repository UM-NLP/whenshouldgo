"""Helpers shared by the route modules: typed errors, the limiter guard, stub and flag guards."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..consent import OperationLimiter
from ..feature_errors import FeatureError, FeatureFailure
from ..domain import (
    ErrorBody,
    ErrorCode,
    ErrorReason,
    Principal,
    PrincipalKind,
    history_active,
)
from ..auth import current_principal
from ..quota import QuotaService

_RETRY_AFTER_HEADER = {
    "Retry-After": {
        "description": "Seconds to wait before retrying.",
        "schema": {"type": "integer", "minimum": 1},
    }
}
ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: {"model": ErrorBody, "description": str(code)}
    for status, code in (
        (401, "POLICY"), (403, "POLICY"), (409, "CONFLICT"), (422, "VALIDATION"), (429, "QUOTA"),
        (502, "VALIDATION"), (503, "PROVIDER"), (504, "PROVIDER"),
    )
}
ERROR_RESPONSES[429]["headers"] = _RETRY_AFTER_HEADER


APP_CHECK_HEADER = "X-Firebase-AppCheck"


def app_check_gate(request: Request) -> None:
    """Router-level App Check (decision 26). ``app.state.app_check_policy`` is None unless the
    composition root configured one; the policy itself decides: enforced means a missing or
    invalid token is a 401, monitor mode only records the signal. No import of the verifier here,
    so the optional ``auth`` extra is not needed to serve."""
    policy = getattr(request.app.state, "app_check_policy", None)
    if policy is not None:
        policy(request, request.headers.get(APP_CHECK_HEADER))


def error(
    status: int, code: ErrorCode, message: str, reason: ErrorReason | None = None,
    retry_after: int | None = None, current_version: int | None = None,
) -> HTTPException:
    body = ErrorBody(code=code, message=message, reason=reason, current_version=current_version)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(status_code=status, detail=body.model_dump(mode="json", exclude_none=True),
                         headers=headers)


def error_response(
    status: int, code: ErrorCode, message: str, headers: dict[str, str] | None = None,
    reason: ErrorReason | None = None, current_version: int | None = None,
) -> JSONResponse:
    body = ErrorBody(code=code, message=message[:500], reason=reason,
                     current_version=current_version)
    return JSONResponse(
        status_code=status, content=body.model_dump(mode="json", exclude_none=True), headers=headers
    )


def access_principal(request: Request, principal: Principal, limiter: OperationLimiter) -> QuotaService:
    if principal.kind == PrincipalKind.DISABLED:
        raise error(403, ErrorCode.POLICY, "account unavailable")
    wait = limiter.check(principal.uid)
    if wait is not None:
        raise error(429, ErrorCode.QUOTA, "too many requests", ErrorReason.RATE_LIMITED, wait)
    return request.app.state.orchestrator.quotas


def _unverified(request: Request) -> bool:
    """App Check ABSENT or INVALID on this request (decision 26, monitor mode). A request the
    policy never looked at (no policy configured) is not unverified here."""
    signal = getattr(request.state, "app_check_signal", None)
    return signal is not None and str(getattr(signal, "value", signal)) in ("ABSENT", "INVALID")


def paid_route_gate(request: Request, principal: Principal, *, general: bool = True) -> None:
    """Per-process, per-principal sliding windows on the routes that spend money (Places, routes,
    turns). ``paid_limiter`` bounds every caller; ``unverified_paid_limiter`` is a TIGHTER window
    for requests whose App Check signal is ABSENT or INVALID. Never a ban: the caller is told when
    to retry. Turns pass ``general=False``: their allowance is the quota meter."""
    state = request.app.state
    limiters = []
    if _unverified(request):
        limiters.append(getattr(state, "unverified_paid_limiter", None))
    if general:
        limiters.append(getattr(state, "paid_limiter", None))
    for limiter in limiters:
        if limiter is None:
            continue
        wait = limiter.check(principal.uid)
        if wait is not None:
            raise error(429, ErrorCode.QUOTA, "too many requests", ErrorReason.RATE_LIMITED, wait)


def require_history_active(
    request: Request, principal: Principal = Depends(current_principal)
) -> None:
    """Route dependency for history and saved-place routes (decision 25).

    Runs right after authentication and before body validation, the rate limiter, quota or any
    store access: while ``history_enabled`` is off (or no approved notice is configured) the
    answer is a typed 404 DISABLED and nothing is read or written. Every route that touches
    history or saved places must declare ``dependencies=[Depends(require_history_active)]``."""
    if not history_active(getattr(request.app.state, "runtime_config", None)):
        raise error(404, ErrorCode.VALIDATION, "feature disabled", ErrorReason.DISABLED)


# failure -> (status, code, message, reason). Static, server-authored text.
_FEATURE_ERRORS: dict[FeatureFailure, tuple[int, ErrorCode, str, ErrorReason | None]] = {
    FeatureFailure.DISABLED: (404, ErrorCode.VALIDATION, "feature disabled", ErrorReason.DISABLED),
    FeatureFailure.GUEST: (403, ErrorCode.POLICY, "not available for guest accounts",
                           ErrorReason.GUEST_NOT_ALLOWED),
    FeatureFailure.CONSENT_REQUIRED: (403, ErrorCode.POLICY, "consent required",
                                      ErrorReason.CONSENT_REQUIRED),
    FeatureFailure.NOT_FOUND: (404, ErrorCode.VALIDATION, "not found", None),
    FeatureFailure.INVALID: (422, ErrorCode.VALIDATION, "request failed validation", None),
    FeatureFailure.CONFLICT: (409, ErrorCode.CONFLICT,
                              "idempotency key reused with a different request", None),
    FeatureFailure.LIMIT: (409, ErrorCode.CONFLICT, "limit reached", None),
    FeatureFailure.UNAVAILABLE: (403, ErrorCode.POLICY, "account unavailable", None),
}


def feature_error(failure: FeatureError) -> HTTPException:
    """The typed HTTP answer of a history or saved-place refusal (static text)."""
    status, code, message, reason = _FEATURE_ERRORS[failure.failure]
    return error(status, code, message, reason)


def history_responses() -> dict[int | str, dict[str, object]]:
    """Error responses of a built, history-gated route: the shared set plus 404 (DISABLED or
    NOT_FOUND). The route can also answer a typed 503 until its service is attached."""
    responses = dict(ERROR_RESPONSES)
    responses[404] = {"model": ErrorBody, "description": "DISABLED or NOT_FOUND"}
    return responses


