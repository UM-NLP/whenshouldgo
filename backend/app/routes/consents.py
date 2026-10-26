"""POST/GET /v1/me/consents and DELETE /v1/me/consents/{purpose}."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ..auth import current_principal
from ..consent import (
    HISTORY_PURPOSES,
    SUPPORTED_PURPOSES,
    ConsentDisabled,
    ConsentLedgerFull,
    ConsentStore,
    WrongNoticeVersion,
)
from ..domain import (
    ConsentProcessor,
    ConsentPurpose,
    ConsentRecord,
    ErrorBody,
    ErrorCode,
    ErrorReason,
    Principal,
    PrincipalKind,
    history_active,
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
    if principal.kind == PrincipalKind.GUEST and body.purpose in HISTORY_PURPOSES:
        # A guest's history and saved places stay on the device (decision 19): nothing is written.
        if not history_active(getattr(request.app.state, "runtime_config", None)):
            raise error(404, ErrorCode.VALIDATION, "feature disabled", ErrorReason.DISABLED)
        raise error(403, ErrorCode.POLICY, "not available for guest accounts",
                    ErrorReason.GUEST_NOT_ALLOWED)
    try:
        view = ConsentView.of(store.grant(principal.uid, body.purpose, body.notice_version))
        accounts = getattr(request.app.state, "account_service", None)
        if accounts is not None and accounts.is_deleted(principal.uid):
            # Post-write recheck: the deletion landed (and swept) while this grant was written.
            accounts.sweep(principal.uid)
            raise error(403, ErrorCode.POLICY, "account unavailable")
        return view
    except ConsentDisabled:  # history purposes while history is not active (decision 25)
        raise error(404, ErrorCode.VALIDATION, "feature disabled", ErrorReason.DISABLED) from None
    except WrongNoticeVersion:
        raise error(422, ErrorCode.VALIDATION, "unsupported purpose or notice version") from None
    except ConsentLedgerFull:
        raise error(409, ErrorCode.CONFLICT, "limit reached") from None


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
    if parsed not in SUPPORTED_PURPOSES:
        raise error(404, ErrorCode.VALIDATION, "no such consent")
    withdrawn = store.withdraw(principal.uid, parsed)
    if parsed in HISTORY_PURPOSES:
        # Withdrawal deletes the data it covered (decisions 19, 24, 25). The cascade runs on every
        # call, even when the consent was already withdrawn, so a failed cleanup is retryable.
        hooks = getattr(request.app.state, "consent_withdrawal_hooks", {}).get(parsed, ())
        try:
            for hook in hooks:
                hook(principal.uid)
        except Exception:  # noqa: BLE001 - static answer; the consent is already withdrawn
            raise error(503, ErrorCode.PROVIDER, "withdrawal cleanup incomplete: retry",
                        ErrorReason.PROVIDER_UNAVAILABLE) from None
    if not withdrawn:
        raise error(404, ErrorCode.VALIDATION, "no such consent")
    return Response(status_code=204)
