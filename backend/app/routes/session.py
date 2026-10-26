"""POST /v1/session:bootstrap, GET /v1/me/usage, GET /v1/me/capabilities."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..access import AccountUnavailable, capabilities_for, session_bootstrap, usage_view
from ..auth import current_principal
from ..domain import Capabilities, ErrorCode, Principal, SessionBootstrap, UsageView
from .common import ERROR_RESPONSES, access_principal, error

router = APIRouter()


def _runtime_config(request: Request):
    return getattr(request.app.state, "runtime_config", None)


def _account_service(request: Request):
    return getattr(request.app.state, "account_service", None)


@router.post("/v1/session:bootstrap", response_model=SessionBootstrap, responses=ERROR_RESPONSES)
def bootstrap(request: Request, principal: Principal = Depends(current_principal)) -> SessionBootstrap:
    quotas = access_principal(request, principal, request.app.state.bootstrap_limiter)
    try:
        return session_bootstrap(quotas, principal, request.app.state.clock(),
                                 _runtime_config(request),
                                 account_service=_account_service(request))
    except AccountUnavailable:
        raise error(403, ErrorCode.POLICY, "account unavailable") from None


@router.get("/v1/me/usage", response_model=UsageView, responses=ERROR_RESPONSES)
def my_usage(request: Request, principal: Principal = Depends(current_principal)) -> UsageView:
    quotas = access_principal(request, principal, request.app.state.access_limiter)
    try:
        return usage_view(quotas, principal)
    except AccountUnavailable:
        raise error(403, ErrorCode.POLICY, "account unavailable") from None


@router.get("/v1/me/capabilities", response_model=Capabilities, responses=ERROR_RESPONSES)
def my_capabilities(request: Request, principal: Principal = Depends(current_principal)) -> Capabilities:
    quotas = access_principal(request, principal, request.app.state.access_limiter)
    try:
        return capabilities_for(principal, usage_view(quotas, principal), _runtime_config(request),
                                account_service=_account_service(request))
    except AccountUnavailable:
        raise error(403, ErrorCode.POLICY, "account unavailable") from None
