"""The caller's workspace and trip activation.

Typed 501 stubs freezing the API contract. Replace the ``not_implemented`` call of a route when it
is built; keep its path and models (the web client is generated from them).
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Path, Request

from ..auth import current_principal
from ..domain import (
    ActivateTripRequest,
    TripActivation,
    WorkspaceEditRequest,
    WorkspaceView,
    Principal,
)
from .common import not_implemented, stub_responses

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


@router.get("/v1/workspace", response_model=WorkspaceView, responses=stub_responses(False),)
def get_workspace(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.patch("/v1/workspace", response_model=WorkspaceView, responses=stub_responses(False),)
def edit_workspace(
    body: WorkspaceEditRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)
