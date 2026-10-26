"""The caller's workspace (live) and trip activation (typed 501 stub).

``GET/PATCH /v1/workspace`` run on ``app.state.workspace_service``; None is a typed 503. The tenant
is the verified principal; the view never carries Maps content. Replace the ``not_implemented``
call of activation when it is built; keep its path and models.
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Header, Path, Request

from ..auth import current_principal
from ..domain import (
    ActivateTripRequest,
    ErrorCode,
    ErrorReason,
    TripActivation,
    WorkspaceEditRequest,
    WorkspaceView,
    Principal,
)
from ..failures import TurnFailure
from ..workspaces import WorkspaceError, WorkspaceNotConfigured, WorkspaceService, to_view
from .common import access_principal, error, not_implemented, stub_responses

router = APIRouter()
IDENT = r"^[A-Za-z0-9_.:-]{1,128}$"


@router.post("/v1/trips/{id}:activate", response_model=TripActivation, responses=stub_responses(False),)
def activate_trip(
    body: ActivateTripRequest,
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


def service(request: Request) -> WorkspaceService:
    found = getattr(request.app.state, "workspace_service", None)
    if not isinstance(found, WorkspaceService):
        raise typed(WorkspaceNotConfigured())
    return found


def typed(failure: WorkspaceError):
    return error(failure.status, failure.code, failure.message, failure.reason)


# The 501 entry stays in the documented responses so the checked-in OpenAPI document changes only
# with the contract (regenerate with ``npm run api:generate`` to drop it).
_RESPONSES = stub_responses(False)


@router.get("/v1/workspace", response_model=WorkspaceView, responses=_RESPONSES)
def get_workspace(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> WorkspaceView:
    access_principal(request, principal, request.app.state.access_limiter)
    try:
        return to_view(service(request).read(principal))
    except WorkspaceError as failure:
        raise typed(failure) from None
    except TurnFailure as failure:
        raise error(failure.status, failure.code, failure.message, failure.reason,
                    failure.retry_after) from None


@router.patch("/v1/workspace", response_model=WorkspaceView, responses=_RESPONSES)
def edit_workspace(
    body: WorkspaceEditRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    idempotency_key: str = Header(min_length=8, max_length=200),
) -> WorkspaceView:
    access_principal(request, principal, request.app.state.access_limiter)
    try:
        return to_view(service(request).edit(principal, body, idempotency_key))
    except WorkspaceError as failure:
        raise typed(failure) from None
    except TurnFailure as failure:  # the shared per-tenant lock says a turn is running
        raise error(failure.status, failure.code, failure.message, failure.reason,
                    failure.retry_after) from None
