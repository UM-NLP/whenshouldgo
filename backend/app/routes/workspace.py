"""The caller's workspace (live): ``GET/PATCH /v1/workspace`` on ``app.state.workspace_service``;
None is a typed 503. The tenant is the verified principal; the view never carries Maps content.
Trip activation lives in ``routes/actions.py``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from ..auth import current_principal
from ..domain import (
    ErrorBody,
    Principal,
    WorkspaceEditRequest,
    WorkspaceView,
)
from ..failures import TurnFailure
from ..workspaces import WorkspaceError, WorkspaceNotConfigured, WorkspaceService, to_view
from .common import ERROR_RESPONSES, access_principal, error

router = APIRouter()


def service(request: Request) -> WorkspaceService:
    found = getattr(request.app.state, "workspace_service", None)
    if not isinstance(found, WorkspaceService):
        raise typed(WorkspaceNotConfigured())
    return found


def typed(failure: WorkspaceError):
    return error(failure.status, failure.code, failure.message, failure.reason,
                 current_version=getattr(failure, "current_version", None))


_RESPONSES = {**ERROR_RESPONSES, 404: {"model": ErrorBody, "description": "NOT_FOUND"}}


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
