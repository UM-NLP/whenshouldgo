"""Review finding 8 (decision 28): while no FirebaseAdmin is configured, me:delete deletes the data and
writes the tombstone but reports completed=false with login_not_removed, and can_delete is false."""
from __future__ import annotations

import pytest
from composition_support import NoopAdmin, build
from fastapi.testclient import TestClient

from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.stores.fake_firestore import FakeFirestoreClient

TOKENS = {"tok-a": VerifiedIdentity("reg-a", "google.com")}
H = {"Authorization": "Bearer tok-a"}


def make(kind: str, **overrides):
    kwargs = dict(identity_verifier=StaticTokenVerifier(TOKENS))
    if kind == "firestore":
        from app import composition
        kwargs.update(firestore_client=FakeFirestoreClient(), allow_in_memory_state=False,
                      spend_budget=composition.SpendBudget(max_tokens=10_000_000))
    kwargs.update(overrides)
    runtime = build(**kwargs)
    return runtime, TestClient(runtime.app)


def seed(http: TestClient) -> None:
    edit = {"base_version": 0, "upsert_facts": [{"key": "selected_place_id", "value": "ChIJplaceA_-1"}]}
    assert http.patch("/v1/workspace", json=edit,
                      headers={**H, "Idempotency-Key": "edit-key-0001"}).status_code == 200


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_delete_with_no_admin_deletes_the_data_but_is_not_completed(kind: str) -> None:
    runtime, http = make(kind, firebase_admin=None)
    seed(http)
    receipt = http.post("/v1/me:delete", headers=H)
    assert receipt.status_code == 200
    body = receipt.json()
    assert body["completed"] is False and body["login_not_removed"] is True
    assert runtime.stores.workspaces.get("reg-a", "default", runtime.account_service._clock()) is None  # noqa: SLF001
    assert runtime.account_service.is_deleted("reg-a") is True  # the tombstone is written
    again = http.post("/v1/me:delete", headers=H).json()
    assert again["completed"] is False and again["login_not_removed"] is True  # retryable


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_a_later_admin_completes_the_same_deletion(kind: str) -> None:
    runtime, http = make(kind, firebase_admin=None)
    http.post("/v1/me:delete", headers=H)
    runtime.account_service.admin = NoopAdmin()
    done = http.post("/v1/me:delete", headers=H).json()
    assert done["completed"] is True and done["login_not_removed"] is False


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_can_delete_is_false_unless_an_admin_is_configured(kind: str) -> None:
    _, without = make(kind, firebase_admin=None)
    _, with_admin = make(kind)
    assert without.get("/v1/me/capabilities", headers=H).json()["can_delete"] is False
    assert with_admin.get("/v1/me/capabilities", headers=H).json()["can_delete"] is True
    assert without.get("/v1/me/capabilities", headers=H).json()["can_export"] is True


def test_the_runtime_exposes_the_state() -> None:
    assert make("memory", firebase_admin=None)[0].login_deletion_available is False
    assert make("memory")[0].login_deletion_available is True
