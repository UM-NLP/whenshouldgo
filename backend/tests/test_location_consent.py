"""Consent ledger and endpoints (POST/GET /v1/me/consents, DELETE /v1/me/consents/{purpose})."""
from __future__ import annotations

import json

import pytest
from location_support import HEADERS_A, HEADERS_B, env_with, grant, no_area_turn, turn
from wiring_support import deep_text

from app.consent import LOCATION_NOTICE_VERSION, ConsentStore
from app.domain import ConsentProcessor, ConsentPurpose, ConsentRecord


def test_the_notice_version_is_the_pinned_constant() -> None:
    assert LOCATION_NOTICE_VERSION == "location-notice-1"


def test_grant_list_and_withdraw() -> None:
    env, _, _ = env_with()
    assert env.client.get("/v1/me/consents", headers=HEADERS_A).json() == {"consents": []}
    response = grant(env)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["purpose"] == "LOCATION_FOREGROUND" and body["notice_version"] == "location-notice-1"
    assert body["processors"] == ["GOOGLE"] and "withdrawn_at" not in body
    listed = env.client.get("/v1/me/consents", headers=HEADERS_A).json()["consents"]
    assert [c["id"] for c in listed] == [body["id"]]
    gone = env.client.delete("/v1/me/consents/LOCATION_FOREGROUND", headers=HEADERS_A)
    assert gone.status_code == 204
    assert env.client.get("/v1/me/consents", headers=HEADERS_A).json() == {"consents": []}


def test_a_grant_is_idempotent_and_the_ledger_is_append_only() -> None:
    env, _, _ = env_with()
    first, second = grant(env).json(), grant(env).json()
    assert first["id"] == second["id"]
    store = env.app.state.consents
    assert len(store.ledger("guest-a")) == 1
    env.client.delete("/v1/me/consents/LOCATION_FOREGROUND", headers=HEADERS_A)
    ledger = store.ledger("guest-a")
    assert len(ledger) == 2 and ledger[0].withdrawn_at is None  # the grant was never mutated
    assert ledger[1].id == ledger[0].id and ledger[1].withdrawn_at is not None
    regrant = grant(env).json()
    assert regrant["id"] != first["id"] and len(store.ledger("guest-a")) == 3


def test_a_record_holds_google_only_and_no_coordinates() -> None:
    env, _, _ = env_with()
    grant(env)
    (record,) = env.app.state.consents.active("guest-a")
    assert isinstance(record, ConsentRecord)
    assert record.purpose == ConsentPurpose.LOCATION_FOREGROUND
    assert record.processors == (ConsentProcessor.GOOGLE,) and record.owner_id == "guest-a"
    assert isinstance(env.app.state.consents, ConsentStore)


def test_tenants_are_isolated() -> None:
    env, _, _ = env_with()
    grant(env, HEADERS_A)
    assert env.client.get("/v1/me/consents", headers=HEADERS_B).json() == {"consents": []}
    missing = env.client.delete("/v1/me/consents/LOCATION_FOREGROUND", headers=HEADERS_B)
    assert missing.status_code == 404 and missing.json()["code"] == "VALIDATION"
    assert len(env.client.get("/v1/me/consents", headers=HEADERS_A).json()["consents"]) == 1


def test_a_notice_version_mismatch_is_422_and_grants_nothing() -> None:
    env, _, _ = env_with()
    response = grant(env, notice_version="location-notice-0")
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION"
    assert env.app.state.consents.ledger("guest-a") == ()


@pytest.mark.parametrize("body", [
    {"purpose": "HISTORY_STORE", "notice_version": "location-notice-1"},
    {"purpose": "LOCATION_FOREGROUND"},
    {"purpose": "LOCATION_FOREGROUND", "notice_version": "location-notice-1", "uid": "x"},
    {"purpose": "LOCATION_FOREGROUND", "notice_version": "location-notice-1",
     "processors": ["OPENROUTER"]},
])
def test_requests_are_closed_and_typed(body) -> None:
    env, _, _ = env_with()
    assert env.client.post("/v1/me/consents", json=body, headers=HEADERS_A).status_code == 422


def test_withdrawing_an_unknown_purpose_or_without_a_grant_is_404() -> None:
    env, _, _ = env_with()
    assert env.client.delete("/v1/me/consents/HISTORY_STORE", headers=HEADERS_A).status_code == 404
    assert env.client.delete("/v1/me/consents/NOPE", headers=HEADERS_A).status_code == 404
    assert env.client.delete("/v1/me/consents/LOCATION_FOREGROUND", headers=HEADERS_A).status_code == 404


def test_the_endpoints_need_a_verified_token() -> None:
    env, _, _ = env_with()
    body = {"purpose": "LOCATION_FOREGROUND", "notice_version": "location-notice-1"}
    for call in (lambda h: env.client.get("/v1/me/consents", headers=h),
                 lambda h: env.client.post("/v1/me/consents", headers=h, json=body),
                 lambda h: env.client.delete("/v1/me/consents/LOCATION_FOREGROUND", headers=h)):
        assert call({}).status_code == 401
        assert call({"Authorization": "Bearer nope"}).status_code == 401


def test_a_disabled_principal_is_403() -> None:
    env, _, _ = env_with()
    env.app.state.resolver.disabled_uids.add("guest-a")
    assert grant(env).status_code == 403


def test_consent_operations_are_rate_limited_per_principal() -> None:
    env, _, _ = env_with()
    codes = [env.client.get("/v1/me/consents", headers=HEADERS_A).status_code for _ in range(40)]
    assert 429 in codes and codes[0] == 200
    limited = env.client.get("/v1/me/consents", headers=HEADERS_A)
    assert limited.status_code == 429 and limited.json()["code"] == "QUOTA"
    assert "retry-after" in limited.headers
    assert env.client.get("/v1/me/consents", headers=HEADERS_B).status_code == 200


def test_the_openapi_contract_documents_the_endpoints_and_typed_errors() -> None:
    env, _, _ = env_with()
    spec = env.client.app.openapi()
    assert set(spec["paths"]["/v1/me/consents"]) == {"get", "post"}
    assert "delete" in spec["paths"]["/v1/me/consents/{purpose}"]
    post = spec["paths"]["/v1/me/consents"]["post"]
    assert {"200", "401", "403", "422", "429"} <= set(post["responses"])
    reasons = spec["components"]["schemas"]["ErrorReason"]["enum"]
    assert "LOCATION_CONSENT_REQUIRED" in reasons


def test_withdrawal_blocks_the_next_located_turn() -> None:
    env, gateway, maps = env_with(no_area_turn)
    grant(env)
    env.client.delete("/v1/me/consents/LOCATION_FOREGROUND", headers=HEADERS_A)
    response = turn(env)
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "POLICY" and body["reason"] == "LOCATION_CONSENT_REQUIRED"
    assert gateway.requests == [] and maps.calls == [] and env.turns() == 0
    assert "12.3" not in json.dumps(body)
    assert "12.3" not in deep_text(env.app.state.consents.ledger("guest-a"))
