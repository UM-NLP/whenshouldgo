"""Internal, non-public routes. Today: ``POST /internal/purge`` (the scheduled retention sweep).

The route exists ONLY when ``PURGE_OIDC_AUDIENCE`` and ``PURGE_CALLER_SERVICE_ACCOUNT`` are both
set (otherwise it is not mounted: a plain 404). It is never in the OpenAPI document, is not behind
the App Check or Firebase-identity dependencies, and accepts exactly one credential: a Google-signed
OIDC ID token (the one Cloud Scheduler sends) whose audience is the configured one and whose
verified email is the configured service account. It returns counts only, deletes at most
``PURGE_MAX_DOCUMENTS`` documents per call, logs nothing about the token or the documents, and
answers failures with static text.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from .consent import OperationLimiter
from .domain import ErrorCode, ErrorReason
from .routes.common import error_response

PURGE_AUDIENCE_VAR = "PURGE_OIDC_AUDIENCE"
PURGE_CALLER_VAR = "PURGE_CALLER_SERVICE_ACCOUNT"
PURGE_LIMIT_VAR = "PURGE_MAX_DOCUMENTS"
DEFAULT_PURGE_LIMIT = 500
MAX_PURGE_LIMIT = 5000
# OWNER-TUNABLE PLACEHOLDERS (per process, per minute). The scheduler calls a few times a day.
UNAUTHENTICATED_PER_MINUTE = 60
AUTHENTICATED_PER_MINUTE = 5

# (token, audience) -> verified claims; raises on ANY failure (signature, expiry, issuer, audience).
OidcVerifier = Callable[[str, str], Mapping[str, Any]]


class PurgeConfigError(ValueError):
    """The purge settings are malformed. Static text; never a value."""


@dataclass(frozen=True, repr=False)
class PurgeSettings:
    audience: str
    caller: str
    limit: int

    def __repr__(self) -> str:
        return "PurgeSettings(...)"

    __str__ = __repr__


def purge_settings_from_env(env: Mapping[str, str]) -> PurgeSettings | None:
    """None (disabled) unless BOTH the audience and the caller service account are set."""
    audience = (env.get(PURGE_AUDIENCE_VAR) or "").strip()
    caller = (env.get(PURGE_CALLER_VAR) or "").strip()
    if not audience or not caller:
        return None
    raw = env.get(PURGE_LIMIT_VAR)
    limit = DEFAULT_PURGE_LIMIT
    if raw is not None and raw.strip():
        try:
            limit = int(raw.strip())
        except ValueError:
            raise PurgeConfigError(f"{PURGE_LIMIT_VAR} must be an integer") from None
        if not 1 <= limit <= MAX_PURGE_LIMIT:
            raise PurgeConfigError(f"{PURGE_LIMIT_VAR} must be between 1 and {MAX_PURGE_LIMIT}")
    return PurgeSettings(audience, caller, limit)


def google_oidc_verifier() -> OidcVerifier:
    """Verify with ``google.oauth2.id_token`` (signature against Google's certificates, expiry,
    issuer and the audience). Needs the ``auth`` or ``gcp`` extra; the import is lazy."""

    def verify(token: str, audience: str) -> Mapping[str, Any]:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(token, google_requests.Request(), audience=audience)

    return verify


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def mount_purge(
    app: FastAPI, settings: PurgeSettings, verify: OidcVerifier,
    purge: Callable[[int], dict[str, int]], clock: Callable[[], Any] | None = None,
) -> None:
    """Mount ``POST /internal/purge``. ``purge(limit)`` returns per-collection counts."""
    router = APIRouter()
    before = OperationLimiter(clock, max_operations=UNAUTHENTICATED_PER_MINUTE,
                              window=timedelta(minutes=1))
    after = OperationLimiter(clock, max_operations=AUTHENTICATED_PER_MINUTE,
                             window=timedelta(minutes=1))

    def refuse(status: int, message: str, wait: int | None = None) -> JSONResponse:
        headers = {"Retry-After": str(wait)} if wait is not None else None
        code = ErrorCode.QUOTA if status == 429 else ErrorCode.POLICY
        reason = ErrorReason.RATE_LIMITED if status == 429 else None
        return error_response(status, code, message, headers, reason)

    @router.post("/internal/purge", include_in_schema=False)
    def run_purge(request: Request) -> JSONResponse:
        wait = before.check("purge")
        if wait is not None:
            return refuse(429, "too many requests", wait)
        token = _bearer(request)
        if token is None:
            return refuse(401, "unauthorized")
        try:
            claims = verify(token, settings.audience)
        except Exception:  # noqa: BLE001 - static text; a verifier message can echo the token
            return refuse(401, "unauthorized")
        if claims.get("email_verified") is not True or claims.get("email") != settings.caller:
            return refuse(403, "forbidden")
        wait = after.check("purge")
        if wait is not None:
            return refuse(429, "too many requests", wait)
        try:
            counts = purge(settings.limit)
        except Exception:  # noqa: BLE001 - static text; store errors can name documents
            return refuse(503, "purge unavailable")
        return JSONResponse({"purged": counts, "limit": settings.limit})

    app.include_router(router)  # deliberately NOT behind app_check_gate or the Firebase resolver


__all__ = [
    "AUTHENTICATED_PER_MINUTE", "DEFAULT_PURGE_LIMIT", "MAX_PURGE_LIMIT", "PURGE_AUDIENCE_VAR",
    "PURGE_CALLER_VAR", "PURGE_LIMIT_VAR", "UNAUTHENTICATED_PER_MINUTE", "OidcVerifier",
    "PurgeConfigError", "PurgeSettings", "google_oidc_verifier", "mount_purge",
    "purge_settings_from_env",
]
