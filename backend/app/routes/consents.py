"""POST/GET /v1/me/consents and DELETE /v1/me/consents/{purpose}."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import current_principal
from ..consent import SUPPORTED_PURPOSES, ConsentDisabled, ConsentStore, WrongNoticeVersion
from ..domain import (
    ConsentProcessor,
    ConsentPurpose,
    ConsentRecord,
    ErrorBody,
    ErrorCode,
    ErrorReason,
    Principal,
    PrincipalKind,
)
from .common import ERROR_RESPONSES, error

router = APIRouter()


class ConsentGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: ConsentPurpose
    notice_version: str = Field(min_length=1, max_length=40)


class ConsentView(BaseModel):
    """An active consent as the client sees it (never a coordinate; withdrawn ones are not listed)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    purpose: ConsentPurpose
    notice_version: str
    processors: tuple[ConsentProcessor, ...]
    granted_at: datetime

    @classmethod
    def of(cls, record: ConsentRecord) -> "ConsentView":
        return cls(id=record.id, purpose=record.purpose, notice_version=record.notice_version,
                   processors=record.processors, granted_at=record.granted_at)


class ConsentList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consents: tuple[ConsentView, ...]


def _consent_principal(request: Request, principal: Principal) -> ConsentStore:
    if principal.kind == PrincipalKind.DISABLED:
        raise error(403, ErrorCode.POLICY, "account unavailable")
    wait = request.app.state.consent_limiter.check(principal.uid)
    if wait is not None:
        raise error(429, ErrorCode.QUOTA, "too many requests", ErrorReason.RATE_LIMITED, wait)
    return request.app.state.consents


@router.post("/v1/me/consents", response_model=ConsentView, responses={
    **ERROR_RESPONSES, 404: {"model": ErrorBody, "description": "DISABLED"}})
def grant_consent(
    body: ConsentGrantRequest, request: Request,
    principal: Principal = Depends(current_principal),
) -> ConsentView:
    store = _consent_principal(request, principal)
    if body.purpose not in SUPPORTED_PURPOSES:
        raise error(422, ErrorCode.VALIDATION, "unsupported purpose or notice version")
    try:
        return ConsentView.of(store.grant(principal.uid, body.purpose, body.notice_version))
    except ConsentDisabled:  # history purposes while history is not active (decision 25)
        raise error(404, ErrorCode.VALIDATION, "feature disabled", ErrorReason.DISABLED) from None
    except WrongNoticeVersion:
        raise error(422, ErrorCode.VALIDATION, "unsupported purpose or notice version") from None


@router.get("/v1/me/consents", response_model=ConsentList, responses=ERROR_RESPONSES)
def list_consents(
    request: Request, principal: Principal = Depends(current_principal)
) -> ConsentList:
    store = _consent_principal(request, principal)
    return ConsentList(consents=tuple(ConsentView.of(r) for r in store.active(principal.uid)))


@router.delete("/v1/me/consents/{purpose}", status_code=204, response_class=Response,
               responses=ERROR_RESPONSES)
def withdraw_consent(
    purpose: str, request: Request, principal: Principal = Depends(current_principal)
) -> Response:
    store = _consent_principal(request, principal)
    try:
        parsed = ConsentPurpose(purpose)
    except ValueError:
        raise error(404, ErrorCode.VALIDATION, "no such consent") from None
    if parsed not in SUPPORTED_PURPOSES or not store.withdraw(principal.uid, parsed):
        raise error(404, ErrorCode.VALIDATION, "no such consent")
    return Response(status_code=204)
