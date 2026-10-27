"""Item 12.1: the history store (memory and the Firestore adapter on the in-process fake).

Opt-in OutingRecords hold a place ID, the user's label, an activity tag, timestamps, outcome and
feedback. Never Maps content. Document ids are hashed; every document has schema_version, kind,
owner_id and the TTL field expires_at.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fs_backend import MEMORY_FAKE_EMULATOR, client_for, txn_for

from history_support import LABEL_CANARY, PLACE, T0

from app import domain as d
from app.stores.firestore import StoredDataError
from app.stores.history_store import (
    HISTORY_RECORDS,
    HISTORY_SETTINGS,
    MAX_RECORDS_PER_OWNER,
    FirestoreHistoryStore,
    MemoryHistoryStore,
)


def make(kind: str):
    if kind == "memory":
        return MemoryHistoryStore(), None
    client = client_for(kind, lambda: T0)
    return FirestoreHistoryStore(client, transactional=txn_for(client)), client


@pytest.fixture(params=MEMORY_FAKE_EMULATOR)
def kind(request: pytest.FixtureRequest) -> str:
    return request.param


def record(owner: str = "u1", rid: str = "out_a", *, at: datetime = T0, days: int = 90,
           label: str | None = LABEL_CANARY, place: str = PLACE) -> d.OutingRecord:
    return d.OutingRecord(
        id=rid, owner_id=owner, workspace_id="ws1", plan_version=1, occurred_at=at, tz="UTC",
        local_dow=at.weekday(), local_daypart=d.Daypart.AFTERNOON,
        place_ref=d.PlaceRef(place_id=place, id_checked_at=at), user_label=label,
        intent_tag=d.IntentTag.FOOD_DRINK,
        events=(d.OutingEvent(type=d.OutingEventType.SELECTED, at=at),),
        strength=d.OutingStrength.HANDOFF, consent_id="consent-1",
        retention_expires_at=at + timedelta(days=days))


def test_put_get_list_and_idempotent_replay(kind: str) -> None:
    store, _ = make(kind)
    rec = record()
    stored, outcome = store.put("u1", rec, "fp1")
    assert outcome == "created" and stored == rec
    again, outcome = store.put("u1", rec, "fp1")
    assert outcome == "replayed" and again == rec
    _, outcome = store.put("u1", rec, "OTHER")
    assert outcome == "conflict"
    assert store.get("u1", "out_a", T0) == rec
    assert store.list("u1", T0) == (rec,)


def test_list_is_newest_first(kind: str) -> None:
    store, _ = make(kind)
    old = record(rid="out_old", at=T0 - timedelta(days=3))
    new = record(rid="out_new", at=T0)
    store.put("u1", old, "a")
    store.put("u1", new, "b")
    assert [r.id for r in store.list("u1", T0)] == ["out_new", "out_old"]


def test_tenant_isolation(kind: str) -> None:
    store, _ = make(kind)
    store.put("u1", record("u1"), "fp")
    assert store.get("u2", "out_a", T0) is None
    assert store.list("u2", T0) == ()
    assert store.delete("u2", "out_a") is False
    assert store.delete_all("u2") == 0
    assert store.get("u1", "out_a", T0) is not None
    with pytest.raises(ValueError):  # a record for another owner is never written
        store.put("u2", record("u1"), "fp")


def test_expired_records_are_invisible_before_the_sweep(kind: str) -> None:
    store, _ = make(kind)
    store.put("u1", record(days=30), "fp")
    later = T0 + timedelta(days=30)
    assert store.get("u1", "out_a", later) is None and store.list("u1", later) == ()
    assert store.purge(later) == 1
    assert store.purge(later) == 0


def test_update_is_atomic_and_tenant_bound(kind: str) -> None:
    store, _ = make(kind)
    store.put("u1", record(), "fp")
    updated = store.update("u1", "out_a", lambda r: d.evolve(r, user_label="Cafe"), T0)
    assert updated is not None and updated.user_label == "Cafe"
    assert store.get("u1", "out_a", T0).user_label == "Cafe"  # type: ignore[union-attr]
    assert store.update("u2", "out_a", lambda r: d.evolve(r, user_label="x"), T0) is None
    assert store.update("u1", "missing", lambda r: r, T0) is None


def test_delete_one_and_clear_all(kind: str) -> None:
    store, _ = make(kind)
    for i in range(3):
        store.put("u1", record(rid=f"out_{i}"), f"fp{i}")
    store.put("u2", record("u2", "out_x"), "fpx")
    assert store.delete("u1", "out_0") is True and store.delete("u1", "out_0") is False
    assert store.delete_all("u1") == 2
    assert store.list("u1", T0) == () and len(store.list("u2", T0)) == 1


def test_retention_defaults_and_shortening_purges_immediately(kind: str) -> None:
    store, _ = make(kind)
    assert store.retention_days("u1") is None
    store.put("u1", record(rid="out_old", at=T0 - timedelta(days=40), days=90), "a")
    store.put("u1", record(rid="out_new", at=T0 - timedelta(days=5), days=90), "b")
    purged = store.apply_retention("u1", 30, T0)
    assert purged == 1 and store.retention_days("u1") == 30
    kept = store.list("u1", T0)
    assert [r.id for r in kept] == ["out_new"]
    # the survivor's expiry is brought forward to occurred_at + 30 days
    assert kept[0].retention_expires_at == kept[0].occurred_at + timedelta(days=30)


def test_extending_retention_never_extends_existing_records(kind: str) -> None:
    store, _ = make(kind)
    store.put("u1", record(days=30), "a")
    assert store.apply_retention("u1", 180, T0) == 0
    assert store.list("u1", T0)[0].retention_expires_at == T0 + timedelta(days=30)
    assert store.retention_days("u1") == 180


def test_per_owner_cap(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.stores import history_store

    assert MAX_RECORDS_PER_OWNER == 1_000
    monkeypatch.setattr(history_store, "MAX_RECORDS_PER_OWNER", 5)
    store, _ = make(kind)
    for i in range(5):
        _, outcome = store.put("u1", record(rid=f"out_{i}"), f"fp{i}")
        assert outcome == "created"
    _, outcome = store.put("u1", record(rid="out_over"), "fpo")
    assert outcome == "full"
    _, outcome = store.put("u1", record(rid="out_0"), "fp0")
    assert outcome == "replayed"  # a replay is never refused by the cap
    _, outcome = store.put("u2", record("u2", "out_o"), "fpo")
    assert outcome == "created"


def test_firestore_documents_are_hashed_typed_and_carry_the_ttl_field() -> None:
    store, client = make("fake-firestore")
    store.put("user-raw-1", record("user-raw-1"), "fp")
    store.apply_retention("user-raw-1", 60, T0)
    docs = client.documents()
    assert {p.split("/")[0] for p, _ in docs} == {HISTORY_RECORDS, HISTORY_SETTINGS}
    for path, doc in docs:
        assert "user-raw-1" not in path
        assert doc["schema_version"] == 1 and doc["owner_id"] == "user-raw-1" and doc["kind"]
    rec_doc = client.documents(HISTORY_RECORDS)[0][1]
    assert isinstance(rec_doc["expires_at"], datetime)
    assert rec_doc["expires_at"] == T0 + timedelta(days=60)
    text = repr(docs)
    for forbidden in ("display_name", "address", "rating", "latitude", "longitude", "review"):
        assert forbidden not in text


def test_firestore_ttl_sweep_and_purge_agree() -> None:
    store, client = make("fake-firestore")
    store.put("u1", record(days=30), "fp")
    client._clock = lambda: T0 + timedelta(days=31)  # noqa: SLF001
    assert client.run_ttl_sweep() == 1
    assert client.documents(HISTORY_RECORDS) == []


def test_firestore_refuses_a_document_of_another_tenant() -> None:
    store, client = make("fake-firestore")
    store.put("u1", record(), "fp")
    path, doc = client.documents(HISTORY_RECORDS)[0]
    doc["owner_id"] = "intruder"
    with pytest.raises(StoredDataError):
        store.get("u1", "out_a", T0)
    doc["owner_id"] = "u1"
    doc["kind"] = "something_else"
    with pytest.raises(StoredDataError):
        store.list("u1", T0)


def test_utc_aware_times_only() -> None:
    assert record().occurred_at.tzinfo is UTC
