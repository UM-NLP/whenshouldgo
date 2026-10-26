"""Firebase ID-token verification as code (decision 26). No Google call happens at import.

`FirebaseIdentityVerifier` implements `auth.IdentityVerifier`. It verifies an RS256
signature against Google's securetoken x509 certificates (fetched through an injectable
`KeyFetcher` and cached for the Cache-Control max-age), validates the standard claims and
returns the verified uid and `firebase.sign_in_provider`. The tier is derived later by
`auth.PrincipalResolver` from those two claims only; nothing from a request body or header
other than the bearer token is ever used. Every failure raises `FirebaseTokenError`, whose
message is a fixed reason code and never contains token content.

The shared key cache and RS256 decode helper here are also used by `appcheck.py`.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import jwt
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .auth import InvalidToken, VerifiedIdentity

SECURETOKEN_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)
ISSUER_PREFIX = "https://securetoken.google.com/"
MAX_TOKEN_CHARS = 8192
DEFAULT_SKEW_SECONDS = 60
DEFAULT_MIN_REFRESH_SECONDS = 30
_MAX_CACHE_SECONDS = 86400

# firebase.sign_in_provider -> registered. "anonymous" is handled by auth.PrincipalResolver.
REGISTERED_PROVIDERS = frozenset({"google.com", "password", "emailLink"})
_ALLOWED_PROVIDERS = REGISTERED_PROVIDERS | {"anonymous"}


class FirebaseConfigError(Exception):
    """Missing or invalid verifier configuration. Raised at construction (fail closed)."""


class FirebaseTokenError(InvalidToken):
    """Token rejected. `reason` is a fixed code; the token text is never included."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KeyFetchError(Exception):
    pass


@dataclass(frozen=True)
class FetchedKeys:
    """kid -> raw key material (x509 PEM string for securetoken, JWK dict for App Check)."""

    keys: Mapping[str, Any]
    max_age_seconds: int


class KeyFetcher(Protocol):
    def fetch(self) -> FetchedKeys: ...


class RevocationChecker(Protocol):
    def is_revoked(self, uid: str, issued_at: float) -> bool:
        """True when the account is disabled or its tokens were revoked after issued_at."""
        ...


Clock = Callable[[], float]


def parse_x509_public_key(raw: Any) -> RSAPublicKey:
    if not isinstance(raw, str):
        raise ValueError("cert")
    key = x509.load_pem_x509_certificate(raw.encode()).public_key()
    if not isinstance(key, RSAPublicKey):
        raise ValueError("cert")
    return key


def parse_jwk_public_key(raw: Any) -> RSAPublicKey:
    if not isinstance(raw, dict) or raw.get("kty") != "RSA":
        raise ValueError("jwk")
    key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(raw))
    if not isinstance(key, RSAPublicKey):
        raise ValueError("jwk")
    return key


class TtlKeyCache:
    """kid -> public key. Honors max-age, with these abuse rules (decision 28):

    * ONE refresh floor for every refresh: at most one fetch per ``min_refresh_interval`` after a
      success, whatever caused it (an unknown kid, an expired TTL, ``max-age=0``);
    * the fetch runs OUTSIDE the lock and is single-flight: concurrent callers neither queue behind
      the network nor start a second fetch (a cold cache waits for the one in flight; a warm cache
      serves its current keys);
    * a failed fetch keeps serving the last keys (stale, for at most ``max_stale_seconds`` past
      their expiry) and backs off exponentially up to ``max_backoff_seconds``, so an upstream
      outage plus a flood of forged-kid tokens cannot turn into a fetch flood or an auth outage.
    """

    def __init__(
        self,
        fetcher: KeyFetcher,
        parse: Callable[[Any], RSAPublicKey],
        clock: Clock = time.time,
        min_refresh_interval: float = DEFAULT_MIN_REFRESH_SECONDS,
        *,
        max_backoff_seconds: float = 300.0,
        max_stale_seconds: float = 86400.0,
        wait_seconds: float = 10.0,
    ) -> None:
        self._fetcher = fetcher
        self._parse = parse
        self._clock = clock
        self._floor = max(0.0, float(min_refresh_interval))
        self._max_backoff = max_backoff_seconds
        self._max_stale = max_stale_seconds
        self._wait = wait_seconds
        self._lock = threading.Lock()
        self._keys: dict[str, RSAPublicKey] | None = None
        self._expires = 0.0
        self._not_before = 0.0  # no fetch before this time (the floor, or a failure backoff)
        self._failures = 0
        self._inflight: threading.Event | None = None

    def _fetch(self) -> tuple[dict[str, RSAPublicKey], int]:
        fetched = self._fetcher.fetch()  # may raise; the caller maps it to a typed rejection
        keys: dict[str, RSAPublicKey] = {}
        for kid, raw in fetched.keys.items():
            try:
                keys[str(kid)] = self._parse(raw)
            except Exception:  # unusable entry is simply absent
                continue
        return keys, min(max(int(fetched.max_age_seconds), 0), _MAX_CACHE_SECONDS)

    def get(self, kid: str) -> RSAPublicKey | None:
        while True:
            with self._lock:
                now = self._clock()
                keys = self._keys
                if keys is not None and now >= self._expires + self._max_stale:
                    keys = self._keys = None  # too stale to trust at all
                needs = keys is None or now >= self._expires or kid not in keys
                if not needs:
                    assert keys is not None
                    return keys.get(kid)
                if now < self._not_before:  # the floor or a backoff: no fetch
                    if keys is None:
                        raise KeyFetchError()
                    return keys.get(kid)
                if self._inflight is not None:  # single flight: someone is already fetching
                    if keys is not None:
                        return keys.get(kid)
                    waiter = self._inflight
                else:
                    waiter = None
                    self._inflight = flight = threading.Event()
            if waiter is not None:  # cold cache: wait for the one fetch, then look again
                waiter.wait(self._wait)
                with self._lock:
                    if self._keys is None:
                        raise KeyFetchError()
                continue
            try:
                fetched_keys, ttl = self._fetch()
            except Exception:
                with self._lock:
                    done = self._clock()
                    self._failures += 1
                    backoff = min(self._max_backoff, max(self._floor, 1.0) * 2 ** (self._failures - 1))
                    self._not_before = done + backoff
                    self._inflight = None
                    stale = self._keys
                flight.set()
                if stale is None:
                    raise
                return stale.get(kid)
            with self._lock:
                done = self._clock()
                self._keys, self._expires = fetched_keys, done + ttl
                self._not_before = done + self._floor
                self._failures = 0
                self._inflight = None
            flight.set()
            return fetched_keys.get(kid)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def decode_rs256(
    token: str,
    cache: TtlKeyCache,
    error: Callable[[str], Exception],
) -> dict[str, Any]:
    """Pinned RS256 signature verification. Claim checks are done by the caller."""
    if not isinstance(token, str) or not token.strip() or len(token) > MAX_TOKEN_CHARS:
        raise error("malformed")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise error("malformed") from None
    if header.get("alg") != "RS256":
        raise error("algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise error("kid")
    try:
        key = cache.get(kid)
    except Exception:
        raise error("keys_unavailable") from None
    if key is None:
        raise error("kid_unknown")
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={
                "verify_signature": True,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "verify_aud": False,
                "verify_iss": False,
                "verify_sub": False,
            },
        )
    except jwt.PyJWTError:
        raise error("signature") from None
    if not isinstance(claims, dict):
        raise error("claims")
    return claims


def check_times(
    claims: Mapping[str, Any],
    now: float,
    skew: float,
    error: Callable[[str], Exception],
    *,
    require_auth_time: bool = False,
) -> None:
    exp, iat = claims.get("exp"), claims.get("iat")
    if not _is_number(exp) or not _is_number(iat):
        raise error("time_claims")
    if exp <= now - skew:
        raise error("expired")
    if iat > now + skew:
        raise error("not_yet_valid")
    if require_auth_time:
        auth_time = claims.get("auth_time")
        if not _is_number(auth_time):
            raise error("time_claims")
        if auth_time > now + skew:
            raise error("not_yet_valid")


def require_project_value(value: str | None, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FirebaseConfigError(f"{what} is required")
    return value.strip()


class FirebaseIdentityVerifier:
    def __init__(
        self,
        project_id: str | None,
        fetcher: KeyFetcher,
        *,
        revocation: RevocationChecker | None = None,
        disabled_uids: set[str] | None = None,
        clock: Clock = time.time,
        skew_seconds: float = DEFAULT_SKEW_SECONDS,
        min_refresh_interval: float = DEFAULT_MIN_REFRESH_SECONDS,
    ) -> None:
        self._project = require_project_value(project_id, "FIREBASE_PROJECT_ID")
        self._issuer = ISSUER_PREFIX + self._project
        self._cache = TtlKeyCache(fetcher, parse_x509_public_key, clock, min_refresh_interval)
        self._revocation = revocation
        self._disabled = disabled_uids
        self._clock = clock
        self._skew = skew_seconds

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str], fetcher: KeyFetcher | None = None, **kwargs: Any
    ) -> "FirebaseIdentityVerifier":
        project = require_project_value(env.get("FIREBASE_PROJECT_ID"), "FIREBASE_PROJECT_ID")
        return cls(project, fetcher or HttpKeyFetcher(SECURETOKEN_CERTS_URL), **kwargs)

    def verify(self, token: str) -> VerifiedIdentity:
        claims = decode_rs256(token, self._cache, FirebaseTokenError)
        if claims.get("aud") != self._project or claims.get("iss") != self._issuer:
            raise FirebaseTokenError("audience_or_issuer")
        check_times(
            claims, self._clock(), self._skew, FirebaseTokenError, require_auth_time=True
        )
        uid = claims.get("sub")
        if not isinstance(uid, str) or not uid.strip() or len(uid) > 128:
            raise FirebaseTokenError("subject")
        firebase = claims.get("firebase")
        provider = firebase.get("sign_in_provider") if isinstance(firebase, dict) else None
        if not isinstance(provider, str) or provider not in _ALLOWED_PROVIDERS:
            raise FirebaseTokenError("provider")
        revoked = self._is_revoked(uid, float(claims["iat"]))
        return VerifiedIdentity(uid=uid, sign_in_provider=provider, revoked=revoked)

    def _is_revoked(self, uid: str, issued_at: float) -> bool:
        """True when the revocation checker says this token's account is disabled or revoked. The
        answer travels on the returned ``VerifiedIdentity`` (``PrincipalResolver`` maps it to
        DISABLED): it is never written to shared state. ``disabled_uids`` is kept only so a caller
        can still hand the resolver the same set; this class neither adds to it nor discards."""
        if self._revocation is None:
            return False
        try:
            return bool(self._revocation.is_revoked(uid, issued_at))
        except Exception:
            raise FirebaseTokenError("revocation_unavailable") from None


_MAX_AGE = re.compile(r"max-age=(\d+)", re.IGNORECASE)


class HttpKeyFetcher:
    """Production fetcher: HTTPS GET, no redirects, bounded size, Cache-Control max-age.

    `extract` turns the decoded JSON body into kid -> raw key. Default: the body is already
    that mapping (securetoken certs). App Check supplies a JWKS extractor.
    Never exercised in tests against the network.
    """

    def __init__(
        self,
        url: str,
        extract: Callable[[Any], Mapping[str, Any]] | None = None,
        client: Any = None,
        timeout: float = 5.0,
        default_max_age: int = 300,
    ) -> None:
        if not url.startswith("https://"):
            raise FirebaseConfigError("key url must be https")
        self._url, self._extract = url, extract or (lambda body: body)
        self._timeout, self._default_max_age = timeout, default_max_age
        self._client = client

    def fetch(self) -> FetchedKeys:
        try:
            if self._client is None:
                import httpx

                client = httpx.Client(follow_redirects=False)
            else:
                client = self._client
            resp = client.get(self._url, timeout=self._timeout)
            if resp.status_code != 200 or len(resp.content) > 262144:
                raise KeyFetchError()
            keys = self._extract(resp.json())
            match = _MAX_AGE.search(resp.headers.get("cache-control", ""))
            max_age = int(match.group(1)) if match else self._default_max_age
            return FetchedKeys(keys=keys, max_age_seconds=max_age)
        except KeyFetchError:
            raise
        except Exception:
            raise KeyFetchError() from None
