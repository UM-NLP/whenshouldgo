"""Helpers shared by the route modules: typed errors, the limiter guard, stub and flag guards."""
from __future__ import annotations

from typing import NoReturn

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..consent import OperationLimiter
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


def stub_responses(history_gated: bool = False) -> dict[int | str, dict[str, object]]:
    """Error responses of a planned route: the shared set plus the typed 501 (and 404 DISABLED)."""
    responses = dict(ERROR_RESPONSES)
    responses[501] = {"model": ErrorBody, "description": "NOT_IMPLEMENTED"}
    if history_gated:
        responses[404] = {"model": ErrorBody, "description": "DISABLED"}
    return responses


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


def not_implemented(request: Request, principal: Principal) -> NoReturn:
    """Handler body of a planned route: authenticated, rate limited, then a typed 501.

    The tenant is the verified principal; nothing from the request is read or stored. Replace the
    call in the route's own module when the route is built."""
    access_principal(request, principal, request.app.state.access_limiter)
    raise error(501, ErrorCode.PROVIDER, "not implemented", ErrorReason.NOT_IMPLEMENTED)
