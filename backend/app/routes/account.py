"""Account linking, export and deletion.

Typed 501 stubs freezing the API contract. Replace the ``not_implemented`` call of a route when it
is built; keep its path and models (the web client is generated from them).
"""
from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Path, Request

from ..auth import current_principal
from ..domain import (
    AccountLinkCompleteRequest,
    AccountLinkIntent,
    AccountLinkResult,
    AccountLinkStartRequest,
    DeleteReceipt,
    ExportBundle,
    Principal,
)
from .common import not_implemented, stub_responses

router = APIRouter()
IDENT = r"^[A-Za-z0-9_.:-]{1,128}$"


@router.post("/v1/account/link-intents", response_model=AccountLinkIntent, responses=stub_responses(False),)
def start_account_link(
    body: AccountLinkStartRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.post("/v1/account/link-intents/{id}:complete", response_model=AccountLinkResult, responses=stub_responses(False),)
def complete_account_link(
    body: AccountLinkCompleteRequest,
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.post("/v1/me:export", response_model=ExportBundle, responses=stub_responses(False),)
def export_me(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)


@router.post("/v1/me:delete", response_model=DeleteReceipt, responses=stub_responses(False),)
def delete_me(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> NoReturn:
    not_implemented(request, principal)
