"""Outcome check-ins and server-side history (dark: needs history_active).

Typed 501 stubs freezing the API contract. Replace the ``not_implemented`` call of a route when it
is built; keep its path and models (the web client is generated from them).
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Header, Request

from ..auth import current_principal
from ..domain import (
    HistoryDeleteResult,
    HistoryRecordCreate,
    HistoryRecordList,
    OutcomeAck,
    OutcomeCheckin,
    OutingRecord,
    Principal,
)
from ..workspaces import WorkspaceError
from .common import access_principal, not_implemented, require_history_active, stub_responses
from .workspace import service as workspace_service, typed

router = APIRouter()


@router.post("/v1/outcomes", response_model=OutcomeAck, responses=stub_responses(False))
def record_outcome(
    body: OutcomeCheckin,
    request: Request,
    principal: Principal = Depends(current_principal),
    idempotency_key: str | None = Header(default=None, min_length=8, max_length=200),
) -> OutcomeAck:
    """Stored server-side only for a REGISTERED principal with history active and consent; for a
    guest nothing durable is written and ``recorded`` is False (keep it on the device)."""
    access_principal(request, principal, request.app.state.access_limiter)
    try:
        return workspace_service(request).record_outcome(principal, body, idempotency_key)
    except WorkspaceError as failure:
        raise typed(failure) from None


@router.get("/v1/history/records", response_model=HistoryRecordList, responses=stub_responses(True),
    dependencies=[Depends(require_history_active)])
def list_history(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.post("/v1/history/records", response_model=OutingRecord, responses=stub_responses(True),
    dependencies=[Depends(require_history_active)])
def write_history(
    body: HistoryRecordCreate,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.delete("/v1/history/records", response_model=HistoryDeleteResult, responses=stub_responses(True),
    dependencies=[Depends(require_history_active)])
def delete_history(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)
