"""Item 12.2: the saved-places store and the ask-state store (memory and Firestore on the fake).

A saved place is user data: place ID, the user's label, a kind and a position rounded to 3 decimals.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from history_support import LABEL_CANARY, PLACE, PLACE2, T0

from app import domain as d
from app.stores.fake_firestore import FakeFirestoreClient, fake_transactional
from app.stores.firestore import StoredDataError
from app.stores.saved_places_store import (
    MAX_SAVED_PLACES,
    SAVED_PLACE_ASKS,
    SAVED_PLACES,
    AskRecord,
    FirestoreAskStateStore,
    FirestoreSavedPlaceStore,
    MemoryAskStateStore,
    MemorySavedPlaceStore,
)

K = d.SavedPlaceKind


def make(kind: str):
    if kind == "memory":
        return MemorySavedPlaceStore(), MemoryAskStateStore(), None
    client = FakeFirestoreClient(lambda: T0)
    return (FirestoreSavedPlaceStore(client, transactional=fake_transactional),
            FirestoreAskStateStore(client, transactional=fake_transactional), client)


@pytest.fixture(params=["memory", "fake-firestore"])
def kind(request: pytest.FixtureRequest) -> str:
    return request.param


def place(pid: str = PLACE, label_kind: K = K.HOME, label: str = LABEL_CANARY) -> d.SavedPlace:
    return d.SavedPlace(place_id=pid, user_label=label, label_kind=label_kind,
                        position=d.RoundedPosition(latitude=43.65322, longitude=-79.38318),
                        created_at=T0)


def test_replace_list_get_and_replay_by_action_id(kind: str) -> None:
    places, _, _ = make(kind)
    assert places.replace("u1", place(), "act_1") == "saved"
    assert places.replace("u1", place(), "act_1") == "replayed"
    assert places.list("u1") == (place(),)
    assert places.get("u1", PLACE) == place()
    assert places.get("u1", PLACE2) is None


def test_one_home_and_one_work_per_owner(kind: str) -> None:
    places, _, _ = make(kind)
    places.replace("u1", place(PLACE, K.HOME, "Home"), "a1")
    places.replace("u1", place(PLACE2, K.HOME, "New home"), "a2")  # replaces the old Home
    assert [p.place_id for p in places.list("u1")] == [PLACE2]
    places.replace("u1", place("ChIJw", K.WORK, "Work"), "a3")
    places.replace("u1", place("ChIJc1", K.CUSTOM, "Gym"), "a4")
    places.replace("u1", place("ChIJc2", K.CUSTOM, "Park"), "a5")
    assert len(places.list("u1")) == 4
    # the same place saved again under another kind replaces its own entry
    places.replace("u1", place("ChIJc1", K.WORK, "Gym as work"), "a6")
    kinds = sorted((p.place_id, p.label_kind.value) for p in places.list("u1"))
    assert ("ChIJc1", "WORK") in kinds and ("ChIJw", "WORK") not in kinds
    assert len(places.list("u1")) == 3


def test_relabel_and_delete(kind: str) -> None:
    places, _, _ = make(kind)
    places.replace("u1", place(), "a1")
    renamed = places.relabel("u1", PLACE, "Renamed")
    assert renamed is not None and renamed.user_label == "Renamed"
    assert renamed.position == place().position and renamed.label_kind == K.HOME
    assert places.relabel("u2", PLACE, "x") is None and places.relabel("u1", PLACE2, "x") is None
    assert places.delete("u2", PLACE) == 0
    assert places.delete("u1", PLACE) == 1 and places.delete("u1", PLACE) == 0


def test_tenant_isolation_and_delete_all_includes_ask_states(kind: str) -> None:
    places, asks, _ = make(kind)
    places.replace("u1", place(), "a1")
    places.replace("u2", place(), "a1")
    asks.put("u1", K.HOME, AskRecord(declined_at=T0, dont_ask_again=False))
    asks.put("u2", K.WORK, AskRecord(declined_at=None, dont_ask_again=True))
    assert places.list("u2") != () and asks.get("u2")[K.WORK].dont_ask_again is True
    assert places.delete_all("u1") == 1 and asks.delete_owner("u1") == 1
    assert places.list("u1") == () and asks.get("u1") == {}
    assert places.list("u2") != () and asks.get("u2") != {}


def test_cap(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.stores import saved_places_store

    assert MAX_SAVED_PLACES == 100
    monkeypatch.setattr(saved_places_store, "MAX_SAVED_PLACES", 4)
    places, _, _ = make(kind)
    for i in range(4):
        assert places.replace("u1", place(f"ChIJ{i}", K.CUSTOM, f"p{i}"), f"a{i}") == "saved"
    assert places.replace("u1", place("ChIJover", K.CUSTOM, "over"), "ao") == "full"
    assert places.replace("u1", place("ChIJ0", K.CUSTOM, "renamed"), "a0b") == "saved"  # an edit


def test_ask_state_put_clear_and_get(kind: str) -> None:
    _, asks, _ = make(kind)
    assert asks.get("u1") == {}
    asks.put("u1", K.HOME, AskRecord(declined_at=T0, dont_ask_again=False))
    asks.put("u1", K.WORK, AskRecord(declined_at=None, dont_ask_again=True))
    state = asks.get("u1")
    assert state[K.HOME].declined_at == T0 and state[K.WORK].dont_ask_again is True
    asks.put("u1", K.HOME, None)
    assert K.HOME not in asks.get("u1") and K.WORK in asks.get("u1")
    asks.put("u1", K.WORK, None)
    assert asks.get("u1") == {}


def test_ask_state_only_for_home_and_work(kind: str) -> None:
    _, asks, _ = make(kind)
    with pytest.raises(ValueError):
        asks.put("u1", K.CUSTOM, AskRecord(declined_at=T0))


def test_firestore_documents_are_hashed_typed_and_hold_only_user_data() -> None:
    places, asks, client = make("fake-firestore")
    places.replace("user-raw-1", place(), "act_1")
    asks.put("user-raw-1", K.HOME, AskRecord(declined_at=T0))
    docs = client.documents()
    assert {p.split("/")[0] for p, _ in docs} == {SAVED_PLACES, SAVED_PLACE_ASKS}
    for path, doc in docs:
        assert "user-raw-1" not in path
        assert doc["schema_version"] == 1 and doc["owner_id"] == "user-raw-1" and doc["kind"]
    saved = client.documents(SAVED_PLACES)[0][1]
    assert saved["place"]["position"] == {"latitude": 43.653, "longitude": -79.383}
    assert "43.65322" not in repr(docs) and "79.38318" not in repr(docs)
    for forbidden in ("display_name", "address", "rating", "review", "hours"):
        assert forbidden not in repr(docs)


def test_firestore_refuses_another_tenants_document() -> None:
    places, asks, client = make("fake-firestore")
    places.replace("u1", place(), "a1")
    asks.put("u1", K.HOME, AskRecord(declined_at=T0))
    client.documents(SAVED_PLACES)[0][1]["owner_id"] = "intruder"
    with pytest.raises(StoredDataError):
        places.get("u1", PLACE)
    with pytest.raises(StoredDataError):
        places.relabel("u1", PLACE, "x")
    assert places.list("u1") == ()  # the sweep query never even returns a foreign document
    client.documents(SAVED_PLACE_ASKS)[0][1]["owner_id"] = "intruder"
    with pytest.raises(StoredDataError):
        asks.get("u1")


def test_cooldown_helper_is_a_plain_record() -> None:
    record = AskRecord(declined_at=T0)
    assert record.declined_at + timedelta(days=30) > T0 and record.dont_ask_again is False
