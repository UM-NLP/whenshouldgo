"""Item 12.6: composition wiring of the dark Phase 1 services, and the intent-tag rules."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from composition_support import build
from fastapi.testclient import TestClient
from history_support import NOTICE, TOKENS, HEnv

from app import domain as d
from app.auth import StaticTokenVerifier
from app.history import derive_intent_tag

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_default_build_is_dark_but_fully_wired() -> None:
    runtime = build(identity_verifier=StaticTokenVerifier(TOKENS))
    assert d.history_active(runtime.config) is False
    assert runtime.history_service is not None and runtime.saved_place_service is not None
    assert runtime.app.state.history_service is runtime.history_service
    assert runtime.app.state.saved_place_service is runtime.saved_place_service
    assert set(runtime.purge_expired()) >= {"pending_actions", "display_cache", "history"}
    http = TestClient(runtime.app)
    response = http.get("/v1/history/records", headers={"Authorization": "Bearer tok-ra"})
    assert response.status_code == 404 and response.json()["reason"] == "DISABLED"
    boot = http.post("/v1/session:bootstrap", headers={"Authorization": "Bearer tok-ra"}).json()
    assert boot["capabilities"]["history_enabled"] is False


def test_the_flag_alone_is_not_enough_without_an_approved_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    assert d.APPROVED_HISTORY_NOTICE_VERSIONS == frozenset()  # nothing is approved in the repo
    runtime = build(identity_verifier=StaticTokenVerifier(TOKENS), history_enabled=True,
                    history_notice_version=NOTICE)
    assert runtime.config.history_enabled is True and d.history_active(runtime.config) is False
    http = TestClient(runtime.app)
    assert http.get("/v1/history/records",
                    headers={"Authorization": "Bearer tok-ra"}).status_code == 404
    assert http.get("/v1/me/capabilities",
                    headers={"Authorization": "Bearer tok-ra"}).json()["history_enabled"] is False


def test_with_the_flag_and_an_approved_notice_it_is_live(monkeypatch: pytest.MonkeyPatch) -> None:
    env = HEnv("memory", monkeypatch)
    assert d.history_active(env.runtime.config) is True
    assert env.call("GET", "/v1/history/settings", "a").status_code == 403  # consent first
    caps = env.call("GET", "/v1/me/capabilities", "a").json()
    assert caps["history_enabled"] is True


@pytest.mark.parametrize("kind", ["memory", "fake-firestore"])
def test_stores_follow_the_state_backend(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.stores.history_store import FirestoreHistoryStore, MemoryHistoryStore
    from app.stores.saved_places_store import FirestoreSavedPlaceStore, MemorySavedPlaceStore

    env = HEnv(kind, monkeypatch)
    memory = kind == "memory"
    assert isinstance(env.runtime.history_service.store,
                      MemoryHistoryStore if memory else FirestoreHistoryStore)
    assert isinstance(env.runtime.saved_place_service.places,
                      MemorySavedPlaceStore if memory else FirestoreSavedPlaceStore)


def test_deleters_and_export_contributors_are_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    env = HEnv("memory", monkeypatch)
    names = set(env.runtime.account_service.deleters.names())
    assert {"history", "saved_places"} <= names
    contributors = {c.name: c.requires_history for c in env.runtime.account_service._contributors}  # noqa: SLF001
    assert contributors["history"] is True and contributors["saved_places"] is True


def test_withdrawal_hooks_cover_exactly_the_two_purposes(monkeypatch: pytest.MonkeyPatch) -> None:
    env = HEnv("memory", monkeypatch)
    hooks = env.app.state.consent_withdrawal_hooks
    assert set(hooks) == {d.ConsentPurpose.HISTORY_STORE, d.ConsentPurpose.SAVED_PLACE}


def test_dev_server_dry_run_keeps_history_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import dev_server

    args = dev_server.build_parser().parse_args(["--i-understand-this-is-dev", "--dry-run"])
    app, runtime = dev_server.build_dev_app(args, {})
    assert d.history_active(runtime.config) is False
    http = TestClient(app, base_url=f"http://127.0.0.1:{args.port}")
    headers = {"Authorization": f"Bearer {dev_server.DEV_TOKEN}"}
    for method, path in (("GET", "/v1/history/records"), ("GET", "/v1/saved-places"),
                         ("POST", "/v1/saved-places/ask-check"), ("GET", "/v1/history/settings")):
        response = http.request(method, path, headers=headers, json={"message": "near home"}
                                if method == "POST" else None)
        assert response.status_code == 404 and response.json()["reason"] == "DISABLED", path
    sys.modules.pop("dev_server", None)


@pytest.mark.parametrize("text,expected", [
    ("a cheap lunch spot", d.IntentTag.FOOD_DRINK),
    ("un café pour le brunch", d.IntentTag.FOOD_DRINK),
    ("a hike with a view", d.IntentTag.OUTDOOR),
    ("une promenade au parc", d.IntentTag.OUTDOOR),
    ("pick up groceries and a pharmacy", d.IntentTag.ERRAND),
    ("something to do", d.IntentTag.OTHER),
    ("", d.IntentTag.OTHER),
    (None, d.IntentTag.OTHER),
    # sensitive tags are never derived, only chosen by the user
    ("a playground for my kids", d.IntentTag.OTHER),
    ("une activité pour enfants", d.IntentTag.OTHER),
])
def test_intent_tag_is_derived_conservatively_from_the_users_own_words(text, expected) -> None:
    assert derive_intent_tag(text) == expected
