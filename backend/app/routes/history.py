"""Outcome check-ins and server-side history (dark: needs history_active).

Typed 501 stubs freezing the API contract. Replace the ``not_implemented`` call of a route when it
is built; keep its path and models (the web client is generated from them).
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Request

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
from .common import not_implemented, require_history_active, stub_responses

router = APIRouter()


@router.post("/v1/outcomes", response_model=OutcomeAck, responses=stub_responses(False),)
def record_outcome(
    body: OutcomeCheckin,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


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
