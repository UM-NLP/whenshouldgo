"""Places Details and route checks (server-rendered, display-only).

Typed 501 stubs freezing the API contract. Replace the ``not_implemented`` call of a route when it
is built; keep its path and models (the web client is generated from them).
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Request

from ..auth import current_principal
from ..domain import (
    PlaceDetailsRequest,
    PlaceDetailsResponse,
    RouteCheckRequest,
    RouteCheckResponse,
    Principal,
)
from .common import not_implemented, stub_responses

router = APIRouter()


@router.post("/v1/places/details", response_model=PlaceDetailsResponse, responses=stub_responses(False),)
def place_details(
    body: PlaceDetailsRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.post("/v1/routes/check", response_model=RouteCheckResponse, responses=stub_responses(False),)
def route_check(
    body: RouteCheckRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)
