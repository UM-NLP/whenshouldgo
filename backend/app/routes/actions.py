"""PendingAction protocol: propose, confirm, cancel, and trip activation.

Live over ``app.state.action_service`` (``app.actions.ActionService``); None is a typed 503
SERVER_NOT_CONFIGURED. The tenant is only the verified principal. Confirmation needs the
authenticated user's request carrying the nonce; no model-facing path reaches these routes'
service. The Maps handoff URL is returned in the confirm response and never stored or logged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Path, Request

from ..access import AccountUnavailable, usage_view
from ..actions import ActionError, ActionFailure, ActionService
from ..auth import current_principal
from ..domain import (
    CancelActionResponse,
    ConfirmActionRequest,
    ConfirmActionResponse,
    ProposeActionRequest,
    ErrorBody,
    ErrorCode,
    ErrorReason,
    PendingAction,
    Principal,
    ActivateTripRequest,
    TripActivation,
)
from .common import ERROR_RESPONSES, access_principal, error

router = APIRouter()
IDENT = r"^[A-Za-z0-9_.:-]{1,128}$"

_RESPONSES: dict[int | str, dict[str, object]] = {
    **ERROR_RESPONSES,
    404: {"model": ErrorBody, "description": "NOT_FOUND or DISABLED"},
}

# failure -> (status, code, message, reason). Static, server-authored text.
_ERRORS: dict[ActionFailure, tuple[int, ErrorCode, str, ErrorReason | None]] = {
    ActionFailure.NOT_FOUND: (404, ErrorCode.VALIDATION, "action not found", None),
    ActionFailure.EXPIRED: (409, ErrorCode.CONFLICT, "action expired", ErrorReason.RESULT_EXPIRED),
    ActionFailure.NONCE_MISMATCH: (403, ErrorCode.POLICY, "confirmation refused", None),
    ActionFailure.CANCELLED: (409, ErrorCode.CONFLICT, "action cancelled", None),
    ActionFailure.ALREADY_CONFIRMED: (409, ErrorCode.CONFLICT, "action already confirmed", None),
    ActionFailure.DISABLED: (404, ErrorCode.VALIDATION, "feature disabled", ErrorReason.DISABLED),
    ActionFailure.NOT_ALLOWED: (403, ErrorCode.POLICY, "not available for guest accounts",
                                ErrorReason.GUEST_NOT_ALLOWED),
    ActionFailure.CONSENT_REQUIRED: (403, ErrorCode.POLICY, "consent required",
                                     ErrorReason.CONSENT_REQUIRED),
    ActionFailure.STALE_PLAN: (409, ErrorCode.CONFLICT, "plan changed", None),
    ActionFailure.IDEMPOTENCY_CONFLICT: (
        409, ErrorCode.CONFLICT, "idempotency key reused with a different request", None),
    ActionFailure.INVALID: (422, ErrorCode.VALIDATION, "request failed validation", None),
}


def _service(request: Request, principal: Principal) -> ActionService:
    """Authenticated and rate limited first; then the service (None: typed 503)."""
    access_principal(request, principal, request.app.state.access_limiter)
    service = getattr(request.app.state, "action_service", None)
    if service is None:
        raise error(503, ErrorCode.PROVIDER, "actions not configured",
                    ErrorReason.SERVER_NOT_CONFIGURED)
    return service


def _typed(exc: ActionError):  # noqa: ANN202
    status, code, message, reason = _ERRORS[exc.failure]
    return error(status, code, message, reason)


@router.post("/v1/actions/propose", response_model=PendingAction, responses=_RESPONSES)
def propose_action(
    body: ProposeActionRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    idempotency_key: str = Header(min_length=8, max_length=200),
) -> PendingAction:
    service = _service(request, principal)
    try:
        return service.propose(principal, body, idempotency_key)
    except ActionError as exc:
        raise _typed(exc) from None


@router.post("/v1/actions/{id}/confirm", response_model=ConfirmActionResponse, responses=_RESPONSES)
def confirm_action(
    body: ConfirmActionRequest,
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
    idempotency_key: str | None = Header(default=None, min_length=8, max_length=200),
) -> ConfirmActionResponse:
    """``Idempotency-Key`` is optional: when sent it must be the action's own key (a retry repeats
    the same key and nonce and gets the same typed result)."""
    service = _service(request, principal)
    try:
        return service.confirm(principal, id, body.nonce, idempotency_key)
    except ActionError as exc:
        raise _typed(exc) from None


@router.post("/v1/actions/{id}/cancel", response_model=CancelActionResponse, responses=_RESPONSES)
def cancel_action(
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> CancelActionResponse:
    service = _service(request, principal)
    try:
        cancelled = service.cancel(principal, id)
    except ActionError as exc:
        raise _typed(exc) from None
    return CancelActionResponse(action_id=id, cancelled=cancelled)


@router.post("/v1/trips/{id}:activate", response_model=TripActivation, responses=_RESPONSES)
def activate_trip(
    body: ActivateTripRequest,
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> TripActivation:
    """Counts the workspace's trip once. At the trip limit the answer is a 200 whose usage meter
    is GATED with ``gate_reason`` TRIPS_LIMIT (the typed gate); the workspace is not counted."""
    service = _service(request, principal)
    try:
        outcome = service.activate(principal, id, body.plan_version)
        usage = usage_view(service.quotas, principal)
    except ActionError as exc:
        raise _typed(exc) from None
    except AccountUnavailable:
        raise error(403, ErrorCode.POLICY, "account unavailable") from None
    return TripActivation(workspace_id=outcome.workspace_id, status=outcome.status, usage=usage)
