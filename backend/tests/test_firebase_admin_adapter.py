"""The Firebase Admin REST adapter and revocation checker over a fake session. No network."""
from __future__ import annotations

import json
import logging
import sys
import types
from typing import Any

import pytest
from account_support import H, Env
from composition_support import ENV, build

from app import server
from app.account import AdminAccount
from app.auth_firebase import FirebaseIdentityVerifier, FirebaseTokenError
from app.composition import ConfigurationError, firebase_admin_from_env
from app.firebase_admin_adapter import (
    FirebaseAdminClient,
    FirebaseAdminError,
    FirebaseRevocationChecker,
)

CANARY_EMAIL = "secret.person@example.com"
CANARY_TOKEN = "ya29.CANARYTOKEN"
PROJECT = "wsg-test-1"


class Resp:
    def __init__(self, status: int, body: Any = None, raw: bytes | None = None) -> None:
        self.status_code = status
        self.content = raw if raw is not None else json.dumps(body or {}).encode()
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Session:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def post(self, url: str, **kw: Any) -> Resp:
        body = kw.pop("json")
        self.calls.append((url, body, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses: Any, sleeps: list[float] | None = None) -> tuple[FirebaseAdminClient, Session]:
    s = Session(*responses)
    sl = sleeps if sleeps is not None else []
    return FirebaseAdminClient(PROJECT, session_factory=lambda: s, sleep=sl.append), s


def user(uid: str = "u1", **extra: Any) -> dict[str, Any]:
    return {"users": [{"localId": uid, "email": CANARY_EMAIL, **extra}]}


def test_lookup_request_shape_and_providers() -> None:
    c, s = client(Resp(200, user(providerUserInfo=[{"providerId": "google.com", "email": CANARY_EMAIL}])))
    assert c.get_account("u1") == AdminAccount("u1", frozenset({"google.com"}), False)
    url, body, kw = s.calls[0]
    assert url == f"https://identitytoolkit.googleapis.com/v1/projects/{PROJECT}/accounts:lookup"
    assert body == {"localId": ["u1"]}
    assert kw == {"timeout": 5.0, "allow_redirects": False}


def test_anonymous_account_has_no_providers_and_missing_is_none() -> None:
    c, _ = client(Resp(200, user()), Resp(200, {}), Resp(400, {"error": {"message": "USER_NOT_FOUND"}}))
    assert c.get_account("u1") == AdminAccount("u1", frozenset(), False)
    assert c.get_account("u1") is None
    assert c.get_account("u1") is None


def test_disabled_flag_and_wrong_uid_and_malformed() -> None:
    c, _ = client(Resp(200, user(disabled=True)), Resp(200, user("other")),
                  Resp(200, {"users": "x"}), Resp(200, user(providerUserInfo=[{}])),
                  Resp(200, raw=b"not json"))
    assert c.get_account("u1").disabled is True  # type: ignore[union-attr]
    for _ in range(4):
        with pytest.raises(FirebaseAdminError):
            c.get_account("u1")


def test_delete_request_shape_and_idempotence() -> None:
    c, s = client(Resp(200, {}), Resp(400, {"error": {"message": "USER_NOT_FOUND"}}), Resp(404, {}))
    for _ in range(3):
        c.delete_user("u1")
    url, body, _ = s.calls[0]
    assert url == f"https://identitytoolkit.googleapis.com/v1/projects/{PROJECT}/accounts:delete"
    assert body == {"localId": "u1"}


def test_delete_other_failures_raise() -> None:
    c, _ = client(Resp(403, {"error": {"message": "PERMISSION_DENIED"}}),
                  Resp(400, {"error": {"message": "OTHER"}}))
    for _ in range(2):
        with pytest.raises(FirebaseAdminError) as e:
            c.delete_user("u1")
        assert e.value.reason == "rejected"


def test_retry_only_on_5xx_and_429_with_bounded_backoff() -> None:
    sleeps: list[float] = []
    c, s = client(Resp(429), Resp(503), Resp(200, {}), sleeps=sleeps)
    c.delete_user("u1")
    assert len(s.calls) == 3 and sleeps == [0.2, 0.4]
    sleeps2: list[float] = []
    c, s = client(Resp(500), Resp(500), Resp(500), Resp(200, {}), sleeps=sleeps2)
    with pytest.raises(FirebaseAdminError) as e:
        c.delete_user("u1")
    assert e.value.reason == "unavailable" and len(s.calls) == 3 and len(sleeps2) == 2
    for status in (400, 401, 403, 404):
        c, s = client(Resp(status, {}), sleeps=sleeps2)
        try:
            c.delete_user("u1")
        except FirebaseAdminError:
            pass
        assert len(s.calls) == 1


def test_transport_errors_are_not_retried_and_text_is_static(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    c, s = client(ConnectionError(f"boom {CANARY_TOKEN} {CANARY_EMAIL}"))
    with pytest.raises(FirebaseAdminError) as e:
        c.get_account("u1")
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert CANARY_TOKEN not in str(e.value) and CANARY_EMAIL not in str(e.value)
    assert len(s.calls) == 1 and caplog.text == ""


def test_credentials_failure_is_static() -> None:
    def factory() -> Any:
        raise RuntimeError(f"key {CANARY_TOKEN}")

    c = FirebaseAdminClient(PROJECT, session_factory=factory)
    with pytest.raises(FirebaseAdminError) as e:
        c.delete_user("u1")
    assert str(e.value) == "credentials_unavailable" and CANARY_TOKEN not in repr(e.value)


def test_oversize_response_uid_and_project_are_refused() -> None:
    c, _ = client(Resp(200, raw=b"x" * 262_145))
    with pytest.raises(FirebaseAdminError):
        c.get_account("u1")
    c, s = client()
    for bad in ("", " ", "x" * 129):
        with pytest.raises(FirebaseAdminError):
            c.delete_user(bad)
    assert s.calls == []
    for bad in ("", "UPPER", "a/b", "evil.com#", "x"):
        with pytest.raises(FirebaseAdminError):
            FirebaseAdminClient(bad)


def test_errors_never_carry_payloads(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {"error": {"message": f"{CANARY_EMAIL} {CANARY_TOKEN}"}}
    c, _ = client(Resp(400, payload), Resp(200, user(validSince="zzz")))
    with pytest.raises(FirebaseAdminError) as e1:
        c.delete_user("u1")
    with pytest.raises(FirebaseAdminError) as e2:
        c.token_state("u1")
    for e in (e1, e2):
        assert CANARY_EMAIL not in repr(e.value) and CANARY_TOKEN not in repr(e.value)
    assert CANARY_EMAIL not in caplog.text


# --- revocation checker --------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_token_state_and_revocation_rules() -> None:
    c, _ = client(Resp(200, user(validSince="2000")), Resp(200, user("u2", disabled=True, validSince="0")),
                  Resp(200, {}))
    checker = FirebaseRevocationChecker(c)
    assert checker.is_revoked("u1", 1999.0) is True  # issued before validSince
    assert checker.is_revoked("u1", 2000.0) is False
    assert checker.is_revoked("u2", 5000.0) is True  # disabled
    assert checker.is_revoked("u3", 5000.0) is True  # deleted account


def test_cache_ttl_and_bound() -> None:
    clock = Clock()
    c, s = client(Resp(200, user("u0")))
    checker = FirebaseRevocationChecker(c, ttl_seconds=60, max_entries=2, clock=clock)
    checker.is_revoked("u0", 1.0)
    checker.is_revoked("u0", 2.0)
    assert len(s.calls) == 1
    clock.t += 59.9
    checker.is_revoked("u0", 3.0)
    assert len(s.calls) == 1
    clock.t += 0.2
    s.responses.append(Resp(200, user("u0")))
    checker.is_revoked("u0", 3.0)
    assert len(s.calls) == 2
    for uid in ("u1", "u2"):
        s.responses.append(Resp(200, user(uid)))
        checker.is_revoked(uid, 1.0)
    assert len(checker._cache) == 2  # noqa: SLF001
    assert "u0" not in checker._cache  # noqa: SLF001


def test_failures_are_not_cached_and_verifier_fails_closed() -> None:
    c, s = client(Resp(503), Resp(503), Resp(503), Resp(200, user("u1", validSince="0")))
    checker = FirebaseRevocationChecker(c)
    with pytest.raises(FirebaseAdminError):
        checker.is_revoked("u1", 1.0)
    assert checker.is_revoked("u1", 1.0) is False
    v = FirebaseIdentityVerifier.__new__(FirebaseIdentityVerifier)
    v._revocation = type("Boom", (), {"is_revoked": lambda self, u, i: 1 / 0})()  # noqa: SLF001
    with pytest.raises(FirebaseTokenError):
        v._is_revoked("u1", 1.0)  # noqa: SLF001


# --- wiring --------------------------------------------------------------------------------------
def fake_google(monkeypatch: pytest.MonkeyPatch) -> None:
    g = types.ModuleType("google")
    a = types.ModuleType("google.auth")
    t = types.ModuleType("google.auth.transport")
    r = types.ModuleType("google.auth.transport.requests")
    g.auth, a.transport, t.requests = a, t, r  # type: ignore[attr-defined]
    for name, mod in (("google", g), ("google.auth", a), ("google.auth.transport", t),
                      ("google.auth.transport.requests", r)):
        monkeypatch.setitem(sys.modules, name, mod)


def test_composition_builds_both_by_default_and_respects_the_switch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    fake_google(monkeypatch)
    admin, rev = firebase_admin_from_env({"FIREBASE_PROJECT_ID": PROJECT})
    assert isinstance(admin, FirebaseAdminClient) and isinstance(rev, FirebaseRevocationChecker)
    assert firebase_admin_from_env({"FIREBASE_PROJECT_ID": PROJECT, "FIREBASE_ADMIN": "enabled"})[0]
    assert firebase_admin_from_env({"FIREBASE_PROJECT_ID": PROJECT, "FIREBASE_ADMIN": "disabled"}) == (
        None, None)
    assert firebase_admin_from_env({}) == (None, None)
    for bad in ("true", "DISABLED", "off"):
        with pytest.raises(ConfigurationError):
            firebase_admin_from_env({"FIREBASE_PROJECT_ID": PROJECT, "FIREBASE_ADMIN": bad})
    with pytest.raises(ConfigurationError):
        firebase_admin_from_env({"FIREBASE_PROJECT_ID": "BAD ID"})


def test_without_google_auth_nothing_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "google", None)
    assert firebase_admin_from_env({"FIREBASE_PROJECT_ID": PROJECT}) == (None, None)


def test_production_boot_is_unchanged_when_env_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_google(monkeypatch)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    with pytest.raises(ConfigurationError, match="FIREBASE_PROJECT_ID"):
        server.create_production_app()
    monkeypatch.setenv("FIREBASE_PROJECT_ID", PROJECT)
    monkeypatch.setenv("FIREBASE_ADMIN", "bogus")
    with pytest.raises(ConfigurationError):
        server.create_production_app()


def test_capabilities_and_delete_complete_only_when_delete_user_ran() -> None:
    c, s = client(Resp(200, {}))
    assert build(firebase_admin=c).login_deletion_available is True
    e = Env("memory")
    e.service.admin = c  # type: ignore[union-attr]
    assert e.http.get("/v1/me/capabilities", headers=H["g1"]).json()["can_delete"] is True
    receipt = e.http.post("/v1/me:delete", headers=H["g1"]).json()
    assert receipt["completed"] is True
    assert len(s.calls) == 1 and s.calls[0][1] == {"localId": "g1"}
    c2, _ = client(Resp(403, {}))
    e2 = Env("memory")
    e2.service.admin = c2  # type: ignore[union-attr]
    assert e2.http.post("/v1/me:delete", headers=H["g1"]).json()["completed"] is False
    assert build(firebase_admin=None).login_deletion_available is False
