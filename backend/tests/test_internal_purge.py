"""POST /internal/purge: the scheduled retention sweep trigger (OIDC-only, internal, counts only)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from composition_support import ENV, build
from fastapi.testclient import TestClient

from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.composition import ConfigurationError, SpendBudget
from app.internal import (
    PURGE_AUDIENCE_VAR,
    PURGE_CALLER_VAR,
    PURGE_LIMIT_VAR,
    google_oidc_verifier,
)
from app.retention import SWEPT_COLLECTIONS, sweep_firestore
from app.stores.fake_firestore import FakeFirestoreClient

AUD = "https://whenshouldgo-api-abc123-uc.a.run.app"
SA = "purge-caller@proj.iam.gserviceaccount.com"
GOOD = "oidc-good-token-CANARY-91ac"
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
CONFIG = {**ENV, PURGE_AUDIENCE_VAR: AUD, PURGE_CALLER_VAR: SA}


class FakeOidc:
    """Verifier double: accepts one token, checks the audience like the real one."""

    def __init__(self, claims: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self.claims = claims if claims is not None else {
            "email": SA, "email_verified": True, "aud": AUD, "iss": "https://accounts.google.com"}

    def __call__(self, token: str, audience: str) -> dict[str, Any]:
        self.calls += 1
        if token != GOOD or audience != AUD:
            raise ValueError("bad token CANARY-should-not-leak")
        return dict(self.claims)


def make(env: dict[str, str] | None = None, oidc: Any = None, *, firestore: bool = False,
         **kw: Any):
    clock = {"now": NOW}
    kwargs: dict[str, Any] = dict(
        env=CONFIG if env is None else env, clock=lambda: clock["now"],
        purge_oidc_verifier=oidc if oidc is not None else FakeOidc())
    if firestore:
        kwargs.update(firestore_client=FakeFirestoreClient(), allow_in_memory_state=False,
                      spend_budget=SpendBudget(max_tokens=10_000_000))
    kwargs.update(kw)
    runtime = build(**kwargs)
    return runtime, TestClient(runtime.app), clock


def post(client: TestClient, token: str | None = GOOD, **headers: str):
    sent = dict(headers)
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return client.post("/internal/purge", headers=sent)


def doc(client: FakeFirestoreClient, collection: str, doc_id: str, expires: datetime) -> None:
    client.collection(collection).document(doc_id).set(
        {"owner_id": "u-secret", "expires_at": expires, "kind": "x"})


# -- authorization: no work without a valid, Google-signed token for the configured caller ---------
def test_disabled_without_full_config_is_404_and_does_no_work() -> None:
    for env in (ENV, {**ENV, PURGE_AUDIENCE_VAR: AUD}, {**ENV, PURGE_CALLER_VAR: SA},
                {**ENV, PURGE_AUDIENCE_VAR: " ", PURGE_CALLER_VAR: SA}):
        oidc = FakeOidc()
        _, client, _ = make(env, oidc)
        assert post(client).status_code == 404
        assert oidc.calls == 0


def test_missing_or_malformed_authorization_is_401_without_verifying() -> None:
    oidc = FakeOidc()
    _, client, _ = make(oidc=oidc)
    assert post(client, None).status_code == 401
    assert client.post("/internal/purge", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.post("/internal/purge", headers={"Authorization": "Bearer "}).status_code == 401
    assert oidc.calls == 0


def test_invalid_token_is_401_and_the_reason_is_not_echoed() -> None:
    _, client, _ = make()
    response = post(client, "forged-token")
    assert response.status_code == 401
    assert "CANARY" not in response.text and "forged" not in response.text


def test_wrong_audience_is_refused_by_the_verifier_contract() -> None:
    seen: list[str] = []

    def strict(token: str, audience: str) -> dict[str, Any]:
        seen.append(audience)
        raise ValueError("audience mismatch")

    _, client, _ = make(oidc=strict)
    assert post(client).status_code == 401
    assert seen == [AUD]  # the CONFIGURED audience is what the verifier is asked to enforce


@pytest.mark.parametrize("claims, status", [
    ({"email": "other@proj.iam.gserviceaccount.com", "email_verified": True}, 403),
    ({"email": SA, "email_verified": False}, 403),
    ({"email": SA, "email_verified": "true"}, 403),
    ({"email": SA}, 403),
    ({"email_verified": True}, 403),
    ({"email": SA.upper(), "email_verified": True}, 403),
    ({"email": SA, "email_verified": True}, 200),
])
def test_only_the_configured_verified_service_account_passes(claims, status) -> None:
    _, client, _ = make(oidc=FakeOidc(claims))
    assert post(client).status_code == status


def test_a_firebase_or_app_check_credential_does_not_open_it() -> None:
    _, client, _ = make(identity_verifier=StaticTokenVerifier(
        {"tok-fb": VerifiedIdentity("guest-x", "anonymous")}))
    assert post(client, "tok-fb", **{"X-Firebase-AppCheck": "token"}).status_code == 401


def test_rate_limited() -> None:
    _, client, _ = make()
    codes = [post(client).status_code for _ in range(12)]
    assert codes[0] == 200 and 429 in codes


def test_unauthenticated_floods_are_limited_too() -> None:
    oidc = FakeOidc()
    _, client, _ = make(oidc=oidc)
    codes = [post(client, "forged").status_code for _ in range(80)]
    assert 429 in codes and oidc.calls < 80


# -- behaviour ---------------------------------------------------------------------------------------
def test_success_returns_counts_only_and_is_idempotent() -> None:
    runtime, client, _ = make(firestore=True)
    fs = runtime.stores.client
    doc(fs, "history_records", "h1", NOW - timedelta(days=1))
    doc(fs, "pending_actions", "p1", NOW - timedelta(minutes=1))
    doc(fs, "workspaces", "w1", NOW - timedelta(days=2))
    doc(fs, "edit_receipts", "e1", NOW - timedelta(days=2))
    doc(fs, "idempotency", "i1", NOW + timedelta(days=2))  # not expired
    first = post(client)
    assert first.status_code == 200
    body = first.json()
    assert body["purged"]["history"] == 1 and body["purged"]["pending_actions"] == 1
    assert body["purged"]["workspaces"] == 1 and body["purged"]["edit_receipts"] == 1
    assert body["purged"]["idempotency"] == 0
    assert set(body) == {"purged", "limit"}
    assert "u-secret" not in first.text and "h1" not in first.text
    assert [p for p, _ in fs.documents()] == ["idempotency/i1"]
    assert sum(post(client).json()["purged"].values()) == 0


def test_in_memory_runtime_purges_without_error() -> None:
    _, client, _ = make()
    body = post(client).json()
    assert body["purged"]["history"] == 0 and body["purged"]["pending_actions"] == 0


def test_bounded_sweep_function() -> None:
    fs = FakeFirestoreClient()
    for i in range(30):
        doc(fs, "history_records", f"h{i}", NOW - timedelta(days=1))
    for i in range(3):
        doc(fs, "workspaces", f"w{i}", NOW - timedelta(days=1))
    first = sweep_firestore(fs, NOW, 10)
    assert sum(first.values()) == 10
    assert first["workspaces"] >= 1  # an even share first: a busy collection cannot starve others
    assert len(fs.documents()) == 23
    total = 10
    while n := sum(sweep_firestore(fs, NOW, 10).values()):
        assert n <= 10
        total += n
    assert total == 33 and fs.documents() == []
    with pytest.raises(ValueError):
        sweep_firestore(fs, NOW, 0)
    assert set(sweep_firestore(fs, NOW)) >= {"history", "outcome_ledgers", "pending_actions",
                                              "display_cache", "workspaces"}
    assert len(SWEPT_COLLECTIONS) == len(set(SWEPT_COLLECTIONS))


def test_the_route_applies_the_configured_limit() -> None:
    runtime, client, _ = make({**CONFIG, PURGE_LIMIT_VAR: "5"}, firestore=True)
    fs = runtime.stores.client
    for i in range(20):
        doc(fs, "history_records", f"h{i}", NOW - timedelta(days=1))
    body = post(client).json()
    assert body["limit"] == 5 and sum(body["purged"].values()) == 5
    assert len(fs.documents()) == 15


def test_config_validation() -> None:
    for bad in ("0", "-1", "abc", "100000"):
        with pytest.raises(ConfigurationError):
            make({**CONFIG, PURGE_LIMIT_VAR: bad})


def test_not_in_the_public_schema() -> None:
    runtime, _, _ = make()
    schema = runtime.app.openapi()
    assert not any("internal" in path or "purge" in path for path in schema["paths"])
    assert not any(path.startswith("/internal") for path in schema["paths"])


def test_no_token_or_document_content_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    runtime, client, _ = make(firestore=True)
    doc(runtime.stores.client, "history_records", "h-LEAK-1", NOW - timedelta(days=1))
    with caplog.at_level(logging.DEBUG):
        post(client)
        post(client, "forged-token-LEAK")
    for secret in (GOOD, "forged-token-LEAK", "u-secret", "h-LEAK-1", AUD, SA):
        assert secret not in caplog.text


def test_a_failing_sweep_is_a_bare_503() -> None:
    runtime, client, _ = make(firestore=True)

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("secret-detail CANARY")

    runtime.stores.client.collection = boom  # type: ignore[method-assign]
    response = post(client)
    assert response.status_code == 503 and "CANARY" not in response.text


def test_production_verifier_never_accepts_a_garbage_token() -> None:
    with pytest.raises(Exception):  # noqa: B017 - any refusal
        google_oidc_verifier()("not.a.jwt", AUD)
