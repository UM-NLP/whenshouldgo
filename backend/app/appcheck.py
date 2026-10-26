"""Firebase App Check token verification and the enforce/observe policy (decision 26).

App Check is a risk signal, not an identity. `AppCheckPolicy(enforce=...)` is enforced only
when the owner configures it: enforced means a missing or invalid `X-Firebase-AppCheck`
header is a 401; not enforced means the result is recorded on `request.state` and never
blocks anyone (an inconclusive signal cannot alone ban a person, docs/access-tiers.md).

Wiring (composition owner), one line per protected route or router:
    policy = AppCheckPolicy(enforce=cfg_enforce, verifier=app_check_verifier)
    router = APIRouter(dependencies=[Depends(policy)])
Downstream code may read `request.state.app_check_signal` (an `AppCheckSignal`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from fastapi import Header, HTTPException, Request

from .auth_firebase import (
    DEFAULT_MIN_REFRESH_SECONDS,
    DEFAULT_SKEW_SECONDS,
    Clock,
    HttpKeyFetcher,
    KeyFetcher,
    TtlKeyCache,
    check_times,
    decode_rs256,
    parse_jwk_public_key,
    require_project_value,
)

APPCHECK_JWKS_URL = "https://firebaseappcheck.googleapis.com/v1/jwks"
ISSUER_PREFIX = "https://firebaseappcheck.googleapis.com/"
HEADER_NAME = "X-Firebase-AppCheck"


class AppCheckError(Exception):
    """App Check token rejected. `reason` is a fixed code; token text is never included."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AppCheckSignal(StrEnum):
    VERIFIED = "VERIFIED"
    ABSENT = "ABSENT"
    INVALID = "INVALID"
    NOT_CHECKED = "NOT_CHECKED"  # no verifier configured and not enforced


@dataclass(frozen=True)
class AppCheckResult:
    app_id: str


def jwks_extractor(body: Any) -> Mapping[str, Any]:
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list):
        raise ValueError("jwks")
    return {k["kid"]: k for k in keys if isinstance(k, dict) and isinstance(k.get("kid"), str)}


def default_fetcher() -> HttpKeyFetcher:
    return HttpKeyFetcher(APPCHECK_JWKS_URL, extract=jwks_extractor)


class AppCheckVerifier:
    def __init__(
        self,
        project_number: str | None,
        fetcher: KeyFetcher,
        *,
        clock: Clock = time.time,
        skew_seconds: float = DEFAULT_SKEW_SECONDS,
        min_refresh_interval: float = DEFAULT_MIN_REFRESH_SECONDS,
    ) -> None:
        self._number = require_project_value(project_number, "Firebase project number")
        self._issuer = ISSUER_PREFIX + self._number
        self._audience = f"projects/{self._number}"
        self._cache = TtlKeyCache(fetcher, parse_jwk_public_key, clock, min_refresh_interval)
        self._clock = clock
        self._skew = skew_seconds

    def verify(self, token: str) -> AppCheckResult:
        claims = decode_rs256(token, self._cache, AppCheckError)
        if claims.get("iss") != self._issuer:
            raise AppCheckError("issuer")
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self._audience not in auds:
            raise AppCheckError("audience")
        check_times(claims, self._clock(), self._skew, AppCheckError)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise AppCheckError("subject")
        return AppCheckResult(app_id=sub)


class AppCheckPolicy:
    """FastAPI dependency (`Depends(policy)`) returning the observed `AppCheckSignal`."""

    def __init__(self, enforce: bool, verifier: AppCheckVerifier | None = None) -> None:
        self._enforce = bool(enforce)
        self._verifier = verifier

    def __call__(
        self,
        request: Request,
        x_firebase_appcheck: str | None = Header(default=None),
    ) -> AppCheckSignal:
        if not x_firebase_appcheck:
            signal = AppCheckSignal.ABSENT
        elif self._verifier is None:
            signal = AppCheckSignal.INVALID if self._enforce else AppCheckSignal.NOT_CHECKED
        else:
            try:
                self._verifier.verify(x_firebase_appcheck)
                signal = AppCheckSignal.VERIFIED
            except AppCheckError:
                signal = AppCheckSignal.INVALID
        request.state.app_check_signal = signal
        if self._enforce and signal is not AppCheckSignal.VERIFIED:
            raise HTTPException(status_code=401, detail="app check required")
        return signal
