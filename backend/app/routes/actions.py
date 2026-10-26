"""PendingAction protocol: propose, confirm, cancel.

Typed 501 stubs freezing the API contract. Replace the ``not_implemented`` call of a route when it
is built; keep its path and models (the web client is generated from them).
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Path, Request

from ..auth import current_principal
from ..domain import (
    CancelActionResponse,
    ConfirmActionRequest,
    ConfirmActionResponse,
    PendingAction,
    ProposeActionRequest,
    Principal,
)
from .common import not_implemented, stub_responses

router = APIRouter()
IDENT = r"^[A-Za-z0-9_.:-]{1,128}$"


@router.post("/v1/actions/propose", response_model=PendingAction, responses=stub_responses(False),)
def propose_action(
    body: ProposeActionRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.post("/v1/actions/{id}/confirm", response_model=ConfirmActionResponse, responses=stub_responses(False),)
def confirm_action(
    body: ConfirmActionRequest,
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.post("/v1/actions/{id}/cancel", response_model=CancelActionResponse, responses=stub_responses(False),)
def cancel_action(
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)
