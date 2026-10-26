"""Account linking, merge, export and deletion.

Every route: authenticated first (401), a disabled principal refused (403), then the account
service must be configured (``app.state.account_service``; None is a typed 503
SERVER_NOT_CONFIGURED), then the dedicated per-principal limiter (429 with Retry-After). The
tenant is always the verified principal. Export and delete never consume allowance, so they work
while the user is gated.

Typed errors: the message starts with a stable upper-case token (for example ``COLLISION:``) until
``ErrorReason`` gains matching values (see the report); clients switch on the token, not the text.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Path, Request

from ..account import (
    AccountError,
    AccountErrorKind,
    AccountExportBundle,
    AccountOp,
    AccountService,
)
from ..auth import current_principal
from ..domain import (
    AccountLinkCompleteRequest,
    AccountLinkIntent,
    AccountLinkResult,
    AccountLinkStartRequest,
    DeleteReceipt,
    ErrorBody,
    ErrorCode,
    ErrorReason,
    Principal,
    PrincipalKind,
)
from ..migration import (
    MigrationError,
    MigrationFailure,
    MigrationRunRequest,
    MigrationStartRequest,
    MigrationView,
)
from .common import ERROR_RESPONSES, error

router = APIRouter()
IDENT = r"^[A-Za-z0-9_.:-]{1,128}$"
RESPONSES = {**ERROR_RESPONSES, 404: {"model": ErrorBody, "description": "NOT_FOUND"}}

_ACCOUNT_ERRORS: dict[AccountErrorKind, tuple[int, ErrorCode, str, ErrorReason | None]] = {
    AccountErrorKind.NOT_CONFIGURED: (
        503, ErrorCode.PROVIDER, "NOT_CONFIGURED: account linking is not available",
        ErrorReason.SERVER_NOT_CONFIGURED),
    AccountErrorKind.NOT_FOUND: (404, ErrorCode.VALIDATION, "NOT_FOUND: unknown link intent", None),
    AccountErrorKind.EXPIRED: (409, ErrorCode.CONFLICT, "EXPIRED: the link intent expired", None),
    AccountErrorKind.CONSUMED: (
        409, ErrorCode.CONFLICT, "CONSUMED: the link intent was already used", None),
    AccountErrorKind.WRONG_UID: (
        403, ErrorCode.POLICY, "WRONG_UID: the token is not the linking identity", None),
    AccountErrorKind.ANONYMOUS_PROVIDER: (
        403, ErrorCode.POLICY, "ANONYMOUS_PROVIDER: sign in with a permanent provider first", None),
    AccountErrorKind.PROVIDER_MISMATCH: (
        403, ErrorCode.POLICY, "PROVIDER_MISMATCH: the provider differs from the intent", None),
    AccountErrorKind.COLLISION: (
        409, ErrorCode.CONFLICT,
        "COLLISION: this credential already has an account; use the explicit merge flow", None),
    AccountErrorKind.ALREADY_REGISTERED: (
        409, ErrorCode.CONFLICT, "ALREADY_REGISTERED: the account is already registered", None),
    AccountErrorKind.RATE_LIMITED: (
        429, ErrorCode.QUOTA, "too many requests", ErrorReason.RATE_LIMITED),
}
_MIGRATION_ERRORS: dict[MigrationFailure, tuple[int, ErrorCode, str, ErrorReason | None]] = {
    MigrationFailure.NOT_FOUND: (404, ErrorCode.VALIDATION, "NOT_FOUND: unknown migration", None),
    MigrationFailure.NOT_REGISTERED: (
        403, ErrorCode.POLICY, "NOT_REGISTERED: sign in to the existing account first", None),
    MigrationFailure.NOT_COLLISION: (
        409, ErrorCode.CONFLICT, "NOT_COLLISION: the link intent was not parked for this account",
        None),
    MigrationFailure.EXPIRED: (409, ErrorCode.CONFLICT, "EXPIRED: the link intent expired", None),
    MigrationFailure.CONFLICT: (
        409, ErrorCode.CONFLICT, "CONFLICT: another merge is unfinished for this guest", None),
    MigrationFailure.UNKNOWN_ITEM: (
        422, ErrorCode.VALIDATION, "UNKNOWN_ITEM: the selection is not in the manifest", None),
    MigrationFailure.INTERRUPTED: (
        503, ErrorCode.PROVIDER, "INTERRUPTED: the merge is saved; call again to resume it",
        ErrorReason.PROVIDER_UNAVAILABLE),
}


def _account_error(failure: AccountError):
    status, code, message, reason = _ACCOUNT_ERRORS[failure.kind]
    return error(status, code, message, reason, failure.retry_after)


def _service(request: Request, principal: Principal, *, allow_deleted: bool = False
             ) -> AccountService:
    """Disabled principals are refused; an unconfigured service is a typed 503."""
    service: AccountService | None = getattr(request.app.state, "account_service", None)
    if principal.kind == PrincipalKind.DISABLED:
        # A principal marked DISABLED only because its deletion is under way may retry the delete.
        if not (allow_deleted and service is not None and service.is_deleted(principal.uid)):
            raise error(403, ErrorCode.POLICY, "account unavailable")
    if service is None:
        raise error(503, ErrorCode.PROVIDER, "account service not configured",
                    ErrorReason.SERVER_NOT_CONFIGURED)
    return service


def _throttle(service: AccountService, op: AccountOp, principal: Principal) -> None:
    try:
        service.throttle(op, principal.uid)
    except AccountError as failure:
        raise _account_error(failure) from None


@router.post("/v1/account/link-intents", response_model=AccountLinkIntent, responses=RESPONSES)
def start_account_link(
    body: AccountLinkStartRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
) -> AccountLinkIntent:
    service = _service(request, principal)
    _throttle(service, AccountOp.LINK, principal)
    try:
        return service.start_link(principal, body.provider, idempotency_key)
    except AccountError as failure:
        raise _account_error(failure) from None


@router.post("/v1/account/link-intents/{id}:complete", response_model=AccountLinkResult,
             responses=RESPONSES)
def complete_account_link(
    body: AccountLinkCompleteRequest,
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> AccountLinkResult:
    service = _service(request, principal)
    _throttle(service, AccountOp.COMPLETE, principal)
    try:
        return service.complete_link(principal, id, body.nonce)
    except AccountError as failure:
        raise _account_error(failure) from None


@router.post("/v1/me:export", response_model=AccountExportBundle, responses=RESPONSES)
def export_me(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> AccountExportBundle:
    service = _service(request, principal)
    _throttle(service, AccountOp.EXPORT, principal)
    return service.export(principal)


@router.post("/v1/me:delete", response_model=DeleteReceipt, responses=RESPONSES)
def delete_me(
    request: Request,
    principal: Principal = Depends(current_principal),
) -> DeleteReceipt:
    service = _service(request, principal, allow_deleted=True)
    _throttle(service, AccountOp.DELETE, principal)
    return service.delete(principal)


# --- explicit merge (the frozen contract has no account-migrations route: provisional) ----------
def _migration_error(failure: MigrationError):
    status, code, message, reason = _MIGRATION_ERRORS[failure.failure]
    return error(status, code, message, reason)


@router.post("/v1/account-migrations", response_model=MigrationView, responses=RESPONSES)
def start_migration(
    body: MigrationStartRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=200),
) -> MigrationView:
    service = _service(request, principal)
    _throttle(service, AccountOp.MIGRATION, principal)
    try:
        return service.migrations.start(
            principal.uid, principal.kind == PrincipalKind.REGISTERED_FREE, body.intent_id,
            body.nonce, idempotency_key)
    except MigrationError as failure:
        raise _migration_error(failure) from None


@router.get("/v1/account-migrations/{id}", response_model=MigrationView, responses=RESPONSES)
def get_migration(
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> MigrationView:
    service = _service(request, principal)
    _throttle(service, AccountOp.MIGRATION, principal)
    try:
        return service.migrations.get(principal.uid, id)
    except MigrationError as failure:
        raise _migration_error(failure) from None


@router.post("/v1/account-migrations/{id}:run", response_model=MigrationView, responses=RESPONSES)
def run_migration(
    body: MigrationRunRequest,
    request: Request,
    id: str = Path(pattern=IDENT),
    principal: Principal = Depends(current_principal),
) -> MigrationView:
    service = _service(request, principal)
    _throttle(service, AccountOp.MIGRATION, principal)
    try:
        return service.migrations.run(principal.uid, id, body.item_ids, body.preference)
    except MigrationError as failure:
        raise _migration_error(failure) from None
