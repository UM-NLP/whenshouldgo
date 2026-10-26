"""Server-side identity: verified token -> Principal.

Identity and tier are derived only from a verified bearer token. A production
FirebaseVerifier (verify_id_token with check_revoked, reading the
firebase.sign_in_provider claim) implements IdentityVerifier without changing callers.
App Check is a separate follow-up signal and is not handled here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from fastapi import Header, HTTPException, Request

from .domain import Principal, PrincipalKind
from .entitlements import PolicyRegistry, default_registry

ANONYMOUS_PROVIDER = "anonymous"
_AUTH_ERROR = "authentication required"


class InvalidToken(Exception):
    """Raised for any token that cannot be verified. Never carries the token text."""


@dataclass(frozen=True)
class VerifiedIdentity:
    uid: str
    sign_in_provider: str
    # Set by a verifier whose revocation check found this token's account disabled or revoked.
    # Resolved PER REQUEST from the token itself: no shared mutable state, so one token's success
    # can never un-disable another's.
    revoked: bool = False


class IdentityVerifier(Protocol):
    def verify(self, token: str) -> VerifiedIdentity: ...


class StaticTokenVerifier:
    """Deterministic dev/test verifier: exact lookup, no crypto, network or secrets.

    Only installed by explicit construction (create_app(verifier=...)); never selected
    by environment or request input.
    """

    def __init__(self, mapping: Mapping[str, VerifiedIdentity]) -> None:
        self._mapping = dict(mapping)

    def verify(self, token: str) -> VerifiedIdentity:
        if not token or not token.strip():
            raise InvalidToken()
        identity = self._mapping.get(token)
        if identity is None:
            raise InvalidToken()
        return identity


@dataclass
class PrincipalResolver:
    """Maps a verified identity to a Principal. The tier is never read from the client."""

    policies: PolicyRegistry = field(default_factory=default_registry)
    disabled_uids: set[str] = field(default_factory=set)

    def resolve(self, identity: VerifiedIdentity) -> Principal:
        if identity.revoked or identity.uid in self.disabled_uids:
            kind = PrincipalKind.DISABLED
        elif identity.sign_in_provider == ANONYMOUS_PROVIDER:
            kind = PrincipalKind.GUEST
        else:
            kind = PrincipalKind.REGISTERED_FREE
        return Principal(identity.uid, kind, self.policies.current().version)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail=_AUTH_ERROR, headers={"WWW-Authenticate": "Bearer"}
    )


def current_principal(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    """FastAPI dependency. Fails closed (401) when no verifier is configured."""
    verifier: IdentityVerifier | None = getattr(request.app.state, "verifier", None)
    resolver: PrincipalResolver | None = getattr(request.app.state, "resolver", None)
    if verifier is None or resolver is None or not authorization:
        raise _unauthorized()
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    try:
        identity = verifier.verify(token)
    except InvalidToken:
        raise _unauthorized() from None
    principal = resolver.resolve(identity)
    # A deleted uid is never served again: its tombstone turns it into a DISABLED principal, so
    # every route refuses it (403) and a late request cannot recreate its data. The account
    # service fails closed when the tombstone cannot be read. Only POST /v1/me:delete looks past
    # it, to let an interrupted deletion be retried.
    account_service = getattr(request.app.state, "account_service", None)
    if (account_service is not None and principal.kind != PrincipalKind.DISABLED
            and account_service.is_deleted(principal.uid)):
        return Principal(principal.uid, PrincipalKind.DISABLED, principal.policy_version)
    return principal
