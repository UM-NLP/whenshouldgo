"""Saved places and Home/Work (dark: needs history_active).

Typed 501 stubs freezing the API contract. Replace the ``not_implemented`` call of a route when it
is built; keep its path and models (the web client is generated from them).
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Query, Request

from ..auth import current_principal
from ..domain import (
    SavedPlace,
    SavedPlaceDeleteResult,
    SavedPlaceList,
    SavedPlaceUpsert,
    Principal,
)
from .common import not_implemented, require_history_active, stub_responses

router = APIRouter()


@router.get("/v1/saved-places", response_model=SavedPlaceList, responses=stub_responses(True),
    dependencies=[Depends(require_history_active)])
def list_saved_places(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.put("/v1/saved-places", response_model=SavedPlace, responses=stub_responses(True),
    dependencies=[Depends(require_history_active)])
def put_saved_place(
    body: SavedPlaceUpsert,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.delete("/v1/saved-places", response_model=SavedPlaceDeleteResult, responses=stub_responses(True),
    dependencies=[Depends(require_history_active)])
def delete_saved_places(
    request: Request,
    place_id: str | None = Query(default=None, pattern=r"^[A-Za-z0-9_-]{1,300}$"),
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)
