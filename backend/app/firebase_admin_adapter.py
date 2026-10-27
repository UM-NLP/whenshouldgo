"""Production Firebase Admin adapter over the Identity Toolkit Admin REST API.

``FirebaseAdminClient`` implements ``account.FirebaseAdmin`` (``get_account`` and ``delete_user``)
and ``FirebaseRevocationChecker`` implements ``auth_firebase.RevocationChecker``. Both talk to ONE
fixed host (``identitytoolkit.googleapis.com``) with Application Default Credentials (workload
identity on Cloud Run, never a key file) through ``google.auth`` and ``AuthorizedSession``, imported
lazily (the optional ``gcp`` extra). Nothing here runs at import and nothing is called at
construction: the credentials are resolved on the first request.

Strictness: project from ``FIREBASE_PROJECT_ID``, request timeout, no redirects, response size cap,
bounded retries with backoff on 5xx/429 only, and typed errors with STATIC messages (never a
payload, token, uid, email or phone number). Nothing is logged.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from .account import AdminAccount

HOST = "identitytoolkit.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MAX_RESPONSE_BYTES = 262_144
DEFAULT_TIMEOUT = 5.0
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.2
MAX_UID_CHARS = 128
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class FirebaseAdminError(Exception):
    """Static message only: the reason code. Never a payload, token, uid or personal data."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _not_found(resp: Any) -> bool:
    if resp.status_code == 404:
        return True
    if resp.status_code != 400:
        return False
    try:
        message = resp.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        return False
    return isinstance(message, str) and message.split(" ")[0] == "USER_NOT_FOUND"


def default_session_factory() -> Any:
    """ADC credentials (``google.auth.default``) wrapped in an ``AuthorizedSession``."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=[SCOPE])
    return AuthorizedSession(credentials)


class FirebaseAdminClient:
    def __init__(
        self,
        project_id: str,
        *,
        session_factory: Callable[[], Any] = default_session_factory,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(project_id, str) or not _PROJECT.match(project_id.strip()):
            raise FirebaseAdminError("project_invalid")
        self._base = f"https://{HOST}/v1/projects/{project_id.strip()}/accounts"
        self._factory, self._timeout, self._sleep = session_factory, timeout, sleep
        self._session: Any = None
        self._lock = threading.Lock()

    def _get_session(self) -> Any:
        with self._lock:
            if self._session is None:
                try:
                    self._session = self._factory()
                except Exception:  # noqa: BLE001 - credential errors can name secrets
                    raise FirebaseAdminError("credentials_unavailable") from None
            return self._session

    def _post(self, action: str, body: dict[str, Any]) -> Any:
        url = f"{self._base}:{action}"
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._get_session().post(
                    url, json=body, timeout=self._timeout, allow_redirects=False)
            except FirebaseAdminError:
                raise
            except Exception:  # noqa: BLE001 - never surface transport text
                raise FirebaseAdminError("transport") from None
            if resp.status_code in _RETRY_STATUS:
                if attempt + 1 < MAX_ATTEMPTS:
                    self._sleep(BACKOFF_BASE * 2**attempt)
                    continue
                raise FirebaseAdminError("unavailable")
            content = getattr(resp, "content", b"")
            if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_RESPONSE_BYTES:
                raise FirebaseAdminError("response_too_large")
            return resp
        raise FirebaseAdminError("unavailable")  # pragma: no cover

    @staticmethod
    def _check_uid(uid: str) -> None:
        if not isinstance(uid, str) or not uid.strip() or len(uid) > MAX_UID_CHARS:
            raise FirebaseAdminError("uid_invalid")

    def _lookup(self, uid: str) -> dict[str, Any] | None:
        self._check_uid(uid)
        resp = self._post("lookup", {"localId": [uid]})
        if _not_found(resp):
            return None
        if resp.status_code != 200:
            raise FirebaseAdminError("rejected")
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            raise FirebaseAdminError("malformed") from None
        if not isinstance(data, dict):
            raise FirebaseAdminError("malformed")
        users = data.get("users")
        if users is None:
            return None
        if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict):
            raise FirebaseAdminError("malformed")
        if users[0].get("localId") != uid:
            raise FirebaseAdminError("malformed")
        return users[0]

    def get_account(self, uid: str) -> AdminAccount | None:
        user = self._lookup(uid)
        if user is None:
            return None
        infos = user.get("providerUserInfo", [])
        if not isinstance(infos, list):
            raise FirebaseAdminError("malformed")
        providers: set[str] = set()
        for info in infos:
            pid = info.get("providerId") if isinstance(info, dict) else None
            if not isinstance(pid, str):
                raise FirebaseAdminError("malformed")
            providers.add(pid)
        return AdminAccount(uid=uid, provider_ids=frozenset(providers),
                            disabled=user.get("disabled") is True)

    def delete_user(self, uid: str) -> None:
        self._check_uid(uid)
        resp = self._post("delete", {"localId": uid})
        if resp.status_code == 200 or _not_found(resp):
            return
        raise FirebaseAdminError("rejected")

    def token_state(self, uid: str) -> tuple[bool, float]:
        """(disabled_or_missing, valid_since_epoch_seconds). A missing account counts as disabled."""
        user = self._lookup(uid)
        if user is None:
            return True, 0.0
        try:
            valid_since = float(user.get("validSince", "0"))
        except (TypeError, ValueError):
            raise FirebaseAdminError("malformed") from None
        return user.get("disabled") is True, valid_since


class FirebaseRevocationChecker:
    """``RevocationChecker`` with a short bounded per-uid cache of (disabled, validSince). A raise
    propagates: ``FirebaseIdentityVerifier`` rejects the token (fail closed). Failures are not
    cached."""

    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client, self._ttl, self._max = client, ttl_seconds, max(1, max_entries)
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, bool, float]] = OrderedDict()
        self._lock = threading.Lock()

    def is_revoked(self, uid: str, issued_at: float) -> bool:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(uid)
            if hit is not None and now < hit[0]:
                self._cache.move_to_end(uid)
            else:
                hit = None
        if hit is None:
            disabled, valid_since = self._client.token_state(uid)  # may raise: fail closed
            with self._lock:
                self._cache[uid] = (now + self._ttl, disabled, valid_since)
                self._cache.move_to_end(uid)
                while len(self._cache) > self._max:
                    self._cache.popitem(last=False)
        else:
            disabled, valid_since = hit[1], hit[2]
        return bool(disabled) or float(issued_at) < valid_since


def admin_enabled(env: Mapping[str, str]) -> bool:
    """False only for ``FIREBASE_ADMIN=disabled``; any value but empty/enabled/disabled raises."""
    raw = env.get("FIREBASE_ADMIN", "")
    if raw in ("", "enabled"):
        return True
    if raw == "disabled":
        return False
    raise ValueError("FIREBASE_ADMIN")
