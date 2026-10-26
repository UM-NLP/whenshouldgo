"""HISTORY_STORE and SAVED_PLACE consent: bound to the approved notice version, grantable only while
history is active (typed DISABLED otherwise), and always withdrawable."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import domain as d
from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.composition import pinned_runtime_config
from app.consent import (
    HISTORY_PURPOSES,
    SUPPORTED_PURPOSES,
    ConsentDisabled,
    InMemoryConsentStore,
    WrongNoticeVersion,
)
from app.main import create_app

NOTICE = "history-notice-1"
PURPOSES = [d.ConsentPurpose.HISTORY_STORE, d.ConsentPurpose.SAVED_PLACE]
HEADERS = {"Authorization": "Bearer tok-a"}


def config_with_history(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(d, "APPROVED_HISTORY_NOTICE_VERSIONS", frozenset({NOTICE}))
    return d.evolve(pinned_runtime_config(), history_enabled=True, history_notice_version=NOTICE)


def client(config) -> TestClient:
    app = create_app(
        verifier=StaticTokenVerifier({"tok-a": VerifiedIdentity("guest-a", "anonymous")}),
        runtime_config=config, dev_scaffold=True, clock=lambda: datetime(2026, 10, 26, tzinfo=UTC))
    return TestClient(app)


def test_the_purposes_are_supported() -> None:
    assert set(PURPOSES) <= SUPPORTED_PURPOSES and set(PURPOSES) == set(HISTORY_PURPOSES)


@pytest.mark.parametrize("purpose", PURPOSES)
def test_with_history_off_the_store_refuses_and_writes_nothing(purpose: d.ConsentPurpose) -> None:
    store = InMemoryConsentStore(config=pinned_runtime_config())  # history is off by default
    with pytest.raises(ConsentDisabled):
        store.grant("guest-a", purpose, NOTICE)
    assert store.ledger("guest-a") == () and not store.has_active("guest-a", purpose)


@pytest.mark.parametrize("purpose", PURPOSES)
def test_with_history_off_the_route_is_a_typed_404_disabled(purpose: d.ConsentPurpose) -> None:
    http = client(pinned_runtime_config())
    response = http.post("/v1/me/consents", headers=HEADERS,
                         json={"purpose": purpose.value, "notice_version": NOTICE})
    assert response.status_code == 404
    assert response.json()["reason"] == "DISABLED" and response.json()["code"] == "VALIDATION"
    assert http.app.state.consents.ledger("guest-a") == ()


def test_with_no_runtime_config_at_all_history_consent_is_disabled() -> None:
    with pytest.raises(ConsentDisabled):
        InMemoryConsentStore().grant("guest-a", d.ConsentPurpose.HISTORY_STORE, NOTICE)


@pytest.mark.parametrize("purpose", PURPOSES)
def test_with_history_active_the_grant_names_the_approved_notice(
    purpose: d.ConsentPurpose, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InMemoryConsentStore(config=config_with_history(monkeypatch))
    for stale in ("location-notice-1", "history-notice-0", ""):
        with pytest.raises(WrongNoticeVersion):
            store.grant("guest-a", purpose, stale)
    assert store.ledger("guest-a") == ()
    record = store.grant("guest-a", purpose, NOTICE)
    assert record.notice_version == NOTICE and record.purpose == purpose
    assert store.grant("guest-a", purpose, NOTICE) == record  # idempotent while active
    assert store.has_active("guest-a", purpose) and not store.has_active("guest-b", purpose)
    assert store.withdraw("guest-a", purpose) is True
    assert not store.has_active("guest-a", purpose)


def test_a_history_consent_goes_inactive_when_history_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = [config_with_history(monkeypatch)]
    store = InMemoryConsentStore(config=lambda: config[0])
    store.grant("guest-a", d.ConsentPurpose.HISTORY_STORE, NOTICE)
    assert store.has_active("guest-a", d.ConsentPurpose.HISTORY_STORE)
    config[0] = pinned_runtime_config()  # history went dark: fail closed
    assert not store.has_active("guest-a", d.ConsentPurpose.HISTORY_STORE)
    assert store.active("guest-a") == ()
    assert store.withdraw("guest-a", d.ConsentPurpose.HISTORY_STORE) is True  # still withdrawable


def test_the_route_grants_and_withdraws_when_history_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = client(config_with_history(monkeypatch))
    bad = http.post("/v1/me/consents", headers=HEADERS,
                    json={"purpose": "HISTORY_STORE", "notice_version": "location-notice-1"})
    assert bad.status_code == 422
    ok = http.post("/v1/me/consents", headers=HEADERS,
                   json={"purpose": "HISTORY_STORE", "notice_version": NOTICE})
    assert ok.status_code == 200 and ok.json()["notice_version"] == NOTICE
    assert http.delete("/v1/me/consents/HISTORY_STORE", headers=HEADERS).status_code == 204
