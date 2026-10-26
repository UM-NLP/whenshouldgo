"""Outcome check-ins and server-side history (dark: needs history_active, decision 25).

Live over ``app.state.history_service`` (``app.history.HistoryService``); None is a typed 503
SERVER_NOT_CONFIGURED. Every history route runs ``require_history_active`` first: with the flag off
(or no approved notice) the answer is a typed 404 DISABLED before any work and nothing is written.
The tenant is only the verified principal; guests get a typed 403 (their history stays on the
device) and every route but delete needs the HISTORY_STORE consent. The records carry place IDs and
the user's own labels only: the client re-fetches details through ``/v1/places/details``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Path, Request

from ..auth import current_principal
from ..domain import (
    ErrorCode,
    ErrorReason,
    HistoryDeleteResult,
    HistoryRecordCreate,
    HistoryRecordEdit,
    HistoryRecordList,
    HistorySettings,
    HistorySettingsUpdate,
    OutcomeAck,
    OutcomeCheckin,
    OutingRecord,
    Principal,
)
from ..feature_errors import FeatureError
from ..history import HistoryService
from ..workspaces import WorkspaceError
from .common import (
    ERROR_RESPONSES,
    access_principal,
    error,
    feature_error,
    history_responses,
    require_history_active,
)
from .workspace import service as workspace_service, typed

router = APIRouter()
_GATE = [Depends(require_history_active)]
_RESPONSES = history_responses()
RECORD_ID = r"^[A-Za-z0-9_.:-]{1,128}$"


def _service(request: Request, principal: Principal) -> HistoryService:
    """Authenticated and rate limited first; then the service (None: typed 503)."""
    access_principal(request, principal, request.app.state.access_limiter)
    found = getattr(request.app.state, "history_service", None)
    if found is None:
        raise error(503, ErrorCode.PROVIDER, "history not configured",
                    ErrorReason.SERVER_NOT_CONFIGURED)
    return found


@router.post("/v1/outcomes", response_model=OutcomeAck, responses=ERROR_RESPONSES)
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


@router.get("/v1/history/records", response_model=HistoryRecordList, responses=_RESPONSES,
            dependencies=_GATE)
def list_history(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> HistoryRecordList:
    service = _service(request, principal)
    try:
        return HistoryRecordList(records=service.list(principal))
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.post("/v1/history/records", response_model=OutingRecord, responses=_RESPONSES,
             dependencies=_GATE)
def write_history(
    body: HistoryRecordCreate,
    request: Request,
    principal: Principal = Depends(current_principal),
    idempotency_key: str = Header(min_length=8, max_length=200),
) -> OutingRecord:
    """The user's explicit save of an outing (idempotent by ``Idempotency-Key``)."""
    service = _service(request, principal)
    try:
        return service.create(principal, body, idempotency_key)
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.delete("/v1/history/records", response_model=HistoryDeleteResult, responses=_RESPONSES,
               dependencies=_GATE)
def delete_history(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> HistoryDeleteResult:
    """Clear all of the caller's history."""
    service = _service(request, principal)
    try:
        return HistoryDeleteResult(deleted=service.clear(principal))
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.patch("/v1/history/records/{id}", response_model=OutingRecord, responses=_RESPONSES,
              dependencies=_GATE)
def edit_history_record(
    body: HistoryRecordEdit,
    request: Request,
    id: str = Path(pattern=RECORD_ID),
    principal: Principal = Depends(current_principal),
) -> OutingRecord:
    service = _service(request, principal)
    try:
        return service.edit(principal, id, body)
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.delete("/v1/history/records/{id}", response_model=HistoryDeleteResult,
               responses=_RESPONSES, dependencies=_GATE)
def delete_history_record(
    request: Request,
    id: str = Path(pattern=RECORD_ID),
    principal: Principal = Depends(current_principal),
) -> HistoryDeleteResult:
    service = _service(request, principal)
    try:
        return HistoryDeleteResult(deleted=service.delete(principal, id))
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.get("/v1/history/settings", response_model=HistorySettings, responses=_RESPONSES,
            dependencies=_GATE)
def get_history_settings(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> HistorySettings:
    service = _service(request, principal)
    try:
        return service.settings(principal)
    except FeatureError as failure:
        raise feature_error(failure) from None


@router.put("/v1/history/settings", response_model=HistorySettings, responses=_RESPONSES,
            dependencies=_GATE)
def put_history_settings(
    body: HistorySettingsUpdate,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> HistorySettings:
    """Choose 30 to 180 days. Shortening purges what is already past the new period, at once."""
    service = _service(request, principal)
    try:
        return service.set_retention(principal, body.retention_days)
    except FeatureError as failure:
        raise feature_error(failure) from None
