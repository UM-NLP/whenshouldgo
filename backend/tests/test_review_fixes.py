"""Security-review fixes: consent bound to notice version, safe post-commit usage, docs off,
bounded limiter and ledger growth, capabilities honesty."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from location_support import HEADERS_A, env_with, grant, no_area_turn, turn

import app.consent as consent_module
from app import main as main_module
from app.access import AccountUnavailable, capabilities_for
from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.consent import InMemoryConsentStore, OperationLimiter
from app.domain import ConsentPurpose, Principal, PrincipalKind
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService

LOC = ConsentPurpose.LOCATION_FOREGROUND


def test_a_stale_notice_version_is_not_an_active_consent(monkeypatch) -> None:
    store = InMemoryConsentStore()
    store.grant("u1", LOC, "location-notice-1")
    assert store.has_active("u1", LOC) and len(store.active("u1")) == 1
    monkeypatch.setitem(consent_module.NOTICE_VERSIONS, LOC, "location-notice-2")
    assert not store.has_active("u1", LOC)
    assert store.active("u1") == ()


def test_a_turn_with_location_under_a_stale_consent_is_403(monkeypatch) -> None:
    env, gateway, maps = env_with(no_area_turn)
    assert grant(env).status_code == 200
    monkeypatch.setitem(consent_module.NOTICE_VERSIONS, LOC, "location-notice-2")
    assert env.client.get("/v1/me/consents", headers=HEADERS_A).json() == {"consents": []}
    response = turn(env)
    assert response.status_code == 403
    assert response.json()["reason"] == "LOCATION_CONSENT_REQUIRED"
    assert gateway.requests == [] and maps.calls == []
    # a fresh grant under the new notice works
    monkeypatch.setattr(consent_module, "LOCATION_NOTICE_VERSION", "location-notice-2")
    again = env.client.post("/v1/me/consents", headers=HEADERS_A, json={
        "purpose": "LOCATION_FOREGROUND", "notice_version": "location-notice-2"})
    assert again.status_code == 200 and again.json()["notice_version"] == "location-notice-2"


def test_export_and_delete_are_not_advertised_until_they_exist() -> None:
    quotas = QuotaService()
    principal = Principal(uid="g", kind=PrincipalKind.GUEST, policy_version=quotas.policies.current().version)
    from app.access import usage_view
    caps = capabilities_for(principal, usage_view(quotas, principal))
    assert caps.can_export is False and caps.can_delete is False and caps.can_read is True


def _client(**kw):
    quotas = QuotaService()
    app = create_app(verifier=StaticTokenVerifier({"t": VerifiedIdentity("u1", "anonymous")}),
                     orchestrator=TurnOrchestrator(quotas, dev_scaffold=True), **kw)
    return TestClient(app), app


def test_interactive_docs_and_schema_routes_are_off_by_default() -> None:
    client, app = _client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert "paths" in app.openapi()  # the export script still works


def test_docs_can_be_enabled_explicitly() -> None:
    client, _ = _client(enable_docs=True)
    assert client.get("/openapi.json").status_code == 200


def test_the_turn_response_carries_the_effective_policy_version() -> None:
    client, app = _client()
    quotas = app.state.orchestrator.quotas
    # a principal carrying an unknown version resolves to the current one
    response = client.post("/v1/agent/turns", json={"message": "Plan a day trip"},
                           headers={"Authorization": "Bearer t", "Idempotency-Key": "k1"})
    assert response.status_code == 200
    assert response.json()["policy_version"] == quotas.policies.current().version


def test_a_counted_turn_never_returns_500_when_the_usage_view_fails(monkeypatch) -> None:
    client, app = _client()

    def boom(*_a, **_k):
        raise AccountUnavailable()

    monkeypatch.setattr("app.routes.turns.usage_view", boom)  # the route moved out of main.py
    response = client.post("/v1/agent/turns", json={"message": "Plan a day trip"},
                           headers={"Authorization": "Bearer t", "Idempotency-Key": "k1"})
    assert response.status_code == 200, response.text
    usage = response.json()["usage"]
    assert usage["meter_state"] == "GATED"  # fail closed: the client re-reads usage
    assert app.state.orchestrator.quotas.usage_for("u1").successful_turns == 1


def test_limiter_prunes_empty_deques_and_caps_tracked_owners(monkeypatch) -> None:
    monkeypatch.setattr(consent_module, "MAX_TRACKED_OWNERS", 50)
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    limiter = OperationLimiter(lambda: now[0], max_operations=2, window=timedelta(minutes=1))
    for i in range(500):
        assert limiter.check(f"uid-{i}") is None
    assert len(limiter._calls) <= 50
    now[0] += timedelta(minutes=5)
    assert limiter.check("uid-new") is None
    assert len(limiter._calls) <= 50
    # an owner who is still limited stays limited (the newest owners are kept)
    assert limiter.check("uid-new") is None and limiter.check("uid-new") is not None


def test_limiter_drops_an_owner_whose_window_emptied() -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    limiter = OperationLimiter(lambda: now[0], max_operations=1, window=timedelta(minutes=1))
    limiter.check("a")
    now[0] += timedelta(minutes=2)
    limiter.check("b")
    assert "a" not in limiter._calls


def test_quota_ledger_is_bounded(monkeypatch) -> None:
    import app.quota as quota_module
    monkeypatch.setattr(quota_module, "MAX_LEDGER_RECORDS", 20)
    quotas = QuotaService()
    for i in range(200):
        quotas.ensure_record(f"u{i}")
    assert len(quotas._usage) <= 20
    assert quotas.usage_for("u199").successful_turns == 0
