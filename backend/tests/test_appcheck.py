"""App Check verifier and enforce/observe policy."""
from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.appcheck import (
    AppCheckError,
    AppCheckPolicy,
    AppCheckSignal,
    AppCheckVerifier,
)
from app.auth_firebase import FirebaseConfigError
from tests.firebase_support import (
    PROJECT_NUMBER,
    Clock,
    FakeFetcher,
    KeyPair,
    appcheck_claims,
    mint,
    mint_alg_none,
    mint_hs256_with_public_key,
)

PAIR = KeyPair("ac-1")
PAIR2 = KeyPair("ac-2")


def verifier(fetcher=None, **kw):
    fetcher = fetcher or FakeFetcher({PAIR.kid: PAIR.jwk()})
    return AppCheckVerifier(PROJECT_NUMBER, fetcher, clock=Clock(), **kw), fetcher


def test_valid_token_returns_app_id():
    v, _ = verifier()
    assert v.verify(mint(PAIR, appcheck_claims())).app_id == "1:123456789012:web:abc"


@pytest.mark.parametrize(
    "over",
    [
        {"iss": "https://firebaseappcheck.googleapis.com/999"},
        {"aud": ["projects/999"]},
        {"aud": "projects/123456789012x"},
        {"exp": 1.0},
        {"iat": 9_999_999_999},
        {"sub": ""},
        {"sub": None},
        {"exp": None},
    ],
)
def test_bad_claims_rejected(over):
    v, _ = verifier()
    with pytest.raises(AppCheckError):
        v.verify(mint(PAIR, appcheck_claims(**over)))


def test_alg_confusion_rejected():
    v, _ = verifier()
    with pytest.raises(AppCheckError):
        v.verify(mint_alg_none(appcheck_claims(), PAIR.kid))
    with pytest.raises(AppCheckError):
        v.verify(mint_hs256_with_public_key(PAIR, appcheck_claims()))


def test_rotation_and_flood_floor():
    fetcher = FakeFetcher({PAIR.kid: PAIR.jwk()})
    v, _ = verifier(fetcher, min_refresh_interval=0)
    v.verify(mint(PAIR, appcheck_claims()))
    fetcher.keys = {PAIR2.kid: PAIR2.jwk()}
    v.verify(mint(PAIR2, appcheck_claims()))
    assert fetcher.calls == 2
    v2, f2 = verifier()
    v2.verify(mint(PAIR, appcheck_claims()))
    for _ in range(4):
        with pytest.raises(AppCheckError):
            v2.verify(mint(PAIR2, appcheck_claims()))
    assert f2.calls == 1


def test_fetch_failure_fails_closed():
    v, _ = verifier(FakeFetcher({}, fail=True))
    with pytest.raises(AppCheckError):
        v.verify(mint(PAIR, appcheck_claims()))


def test_construction_requires_project_number():
    for bad in ("", " ", None):
        with pytest.raises(FirebaseConfigError):
            AppCheckVerifier(bad, FakeFetcher({}))


def make_client(policy):
    app = FastAPI()

    @app.get("/probe")
    def probe(request: Request, signal: AppCheckSignal = Depends(policy)):
        return {"signal": signal.value, "state": request.state.app_check_signal.value}

    return TestClient(app)


def test_enforced_rejects_missing_and_invalid_accepts_valid():
    v, _ = verifier()
    c = make_client(AppCheckPolicy(enforce=True, verifier=v))
    assert c.get("/probe").status_code == 401
    assert c.get("/probe", headers={"X-Firebase-AppCheck": "junk"}).status_code == 401
    good = mint(PAIR, appcheck_claims())
    r = c.get("/probe", headers={"X-Firebase-AppCheck": good})
    assert r.status_code == 200 and r.json()["signal"] == "VERIFIED"


def test_enforced_without_verifier_fails_closed():
    c = make_client(AppCheckPolicy(enforce=True, verifier=None))
    assert c.get("/probe", headers={"X-Firebase-AppCheck": "x"}).status_code == 401


def test_not_enforced_records_risk_signal_only():
    v, _ = verifier()
    c = make_client(AppCheckPolicy(enforce=False, verifier=v))
    assert c.get("/probe").json() == {"signal": "ABSENT", "state": "ABSENT"}
    r = c.get("/probe", headers={"X-Firebase-AppCheck": "junk"})
    assert r.status_code == 200 and r.json()["signal"] == "INVALID"
    good = mint(PAIR, appcheck_claims())
    assert c.get("/probe", headers={"X-Firebase-AppCheck": good}).json()["signal"] == "VERIFIED"


def test_not_enforced_without_verifier_is_not_checked():
    c = make_client(AppCheckPolicy(enforce=False, verifier=None))
    r = c.get("/probe", headers={"X-Firebase-AppCheck": "x"})
    assert r.status_code == 200 and r.json()["signal"] == "NOT_CHECKED"


def test_tokens_not_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    v, _ = verifier()
    bad = mint(PAIR, appcheck_claims(iss="https://evil"))
    with pytest.raises(AppCheckError) as ei:
        v.verify(bad)
    for part in bad.split("."):
        assert part not in repr(ei.value) + str(ei.value)
    c = make_client(AppCheckPolicy(enforce=True, verifier=v))
    r = c.get("/probe", headers={"X-Firebase-AppCheck": bad})
    assert bad not in r.text and bad not in caplog.text
