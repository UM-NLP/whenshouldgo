"""PendingActionStore contract over the in-memory store and the Firestore adapter (fake client)."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from actions_support import KEY, LABEL, PLACE, T0, make_store

from app.domain import PendingAction, PendingActionEffect
from app.stores.actions_store import (
    RECORD_GRACE,
    ActionRecord,
    ActionStatus,
    FirestorePendingActionStore,
    PendingActionStore,
)
from app.stores.fake_firestore import FakeFirestoreClient, fake_transactional
from app.stores.firestore import StoredDataError

KINDS = ["memory", "firestore"]


def record(owner: str = "guest-a", action_id: str = "act_1", nonce: str = "n" * 20) -> ActionRecord:
    action = PendingAction(
        id=action_id, workspace_id="ws1", plan_version=1, effect=PendingActionEffect.SET_REMINDER,
        target=PLACE, preview="Set a reminder", nonce=nonce, idempotency_key=KEY, created_at=T0,
        expires_at=T0 + timedelta(minutes=15))
    return ActionRecord(owner_id=owner, action=action, fingerprint="f" * 64, user_label=LABEL,
                        due_at=T0 + timedelta(hours=1))


def put(store: PendingActionStore, rec: ActionRecord) -> ActionRecord:
    return store.update(rec.owner_id, rec.action.id, lambda cur: (cur or rec, cur or rec))


@pytest.mark.parametrize("kind", KINDS)
def test_create_get_and_tenant_isolation(kind: str) -> None:
    store = make_store(kind)
    rec = put(store, record())
    assert store.get("guest-a", "act_1") == rec
    assert store.get("guest-b", "act_1") is None  # another tenant never sees it
    assert store.get("guest-a", "act_2") is None


@pytest.mark.parametrize("kind", KINDS)
def test_update_is_atomic_and_create_if_absent(kind: str) -> None:
    store = make_store(kind)
    first = put(store, record(nonce="a" * 20))
    second = put(store, record(nonce="b" * 20))
    assert second == first and store.get("guest-a", "act_1").action.nonce == "a" * 20


@pytest.mark.parametrize("kind", KINDS)
def test_update_can_change_status_and_none_means_no_write(kind: str) -> None:
    store = make_store(kind)
    rec = put(store, record())
    store.update("guest-a", "act_1", lambda cur: (None, "noop"))
    assert store.get("guest-a", "act_1") == rec
    store.update("guest-a", "act_1", lambda cur: (replace(cur, status=ActionStatus.CANCELLED), 1))
    assert store.get("guest-a", "act_1").status == ActionStatus.CANCELLED


@pytest.mark.parametrize("kind", KINDS)
def test_purge_removes_only_expired_records(kind: str) -> None:
    store = make_store(kind)
    put(store, record(action_id="act_1"))
    assert store.purge(T0 + timedelta(minutes=15)) == 0  # expired but inside the grace period
    assert store.purge(T0 + timedelta(minutes=15) + RECORD_GRACE) == 1
    assert store.get("guest-a", "act_1") is None


@pytest.mark.parametrize("kind", KINDS)
def test_delete_owner(kind: str) -> None:
    store = make_store(kind)
    put(store, record("guest-a", "act_1"))
    put(store, record("guest-a", "act_2"))
    put(store, record("guest-b", "act_1"))
    assert store.delete_owner("guest-a") == 2
    assert store.get("guest-a", "act_1") is None and store.get("guest-b", "act_1") is not None


def test_firestore_documents_have_schema_version_kind_ttl_and_hashed_id() -> None:
    client = FakeFirestoreClient()
    store = FirestorePendingActionStore(client, transactional=fake_transactional)
    put(store, record())
    ((path, doc),) = client.documents("pending_actions")
    assert "guest-a" not in path and "act_1" not in path
    assert doc["schema_version"] == 1 and doc["kind"] == "pending_action_record"
    assert doc["owner_id"] == "guest-a"
    assert doc["expires_at"] == T0 + timedelta(minutes=15) + RECORD_GRACE
    text = repr(doc).lower()
    assert "latitude" not in text and "http" not in text and "coordinates" not in text


def test_firestore_ttl_sweep_and_tampered_owner_refused() -> None:
    client = FakeFirestoreClient(clock=lambda: T0 + timedelta(days=3))
    store = FirestorePendingActionStore(client, transactional=fake_transactional)
    put(store, record())
    client.documents("pending_actions")[0][1]["owner_id"] = "guest-b"
    with pytest.raises(StoredDataError):
        store.get("guest-a", "act_1")
    assert client.run_ttl_sweep() == 1
