"""Review finding 4: the outcome ledger obeys the history controls (delete, clear, edit, retention)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from history_support import NOTE_CANARY, PLACE, PLACE2, HEnv

from app.domain import ConsentPurpose

HIST = ConsentPurpose.HISTORY_STORE


@pytest.fixture(params=["memory", "fake-firestore"])
def ready(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> HEnv:
    env = HEnv(request.param, monkeypatch)
    assert env.consent("a", HIST).status_code == 200
    return env


def ledger(env: HEnv):
    return env.runtime.plan_stores.outcomes.entries("reg-a", env.clock())


def records(env: HEnv) -> list[dict]:
    return env.call("GET", "/v1/history/records").json()["records"]


def add(env: HEnv, key: str, place: str = PLACE, note: str | None = NOTE_CANARY) -> None:
    body = {"place_id": place, "outcome": "WENT"}
    if note:
        body["note"] = note
    assert env.call("POST", "/v1/outcomes", "a", body, key).json()["recorded"] is True


def test_the_outcome_is_in_the_ledger_to_begin_with(ready: HEnv) -> None:
    add(ready, "outcome-key-1")
    assert len(ledger(ready)) == 1 and ledger(ready)[0].note == NOTE_CANARY


def test_deleting_a_record_removes_that_places_ledger_entries(ready: HEnv) -> None:
    add(ready, "outcome-key-1", PLACE)
    add(ready, "outcome-key-2", PLACE2)
    target = next(r for r in records(ready) if r["place_ref"]["place_id"] == PLACE)
    assert ready.call("DELETE", f"/v1/history/records/{target['id']}").status_code == 200
    assert [e.place_id for e in ledger(ready)] == [PLACE2]


def test_clearing_history_clears_the_ledger(ready: HEnv) -> None:
    add(ready, "outcome-key-1")
    add(ready, "outcome-key-2", PLACE2)
    assert ready.call("DELETE", "/v1/history/records").status_code == 200
    assert ledger(ready) == ()
    assert NOTE_CANARY not in ready.stores_text()


def test_editing_the_feedback_drops_the_ledger_note_for_that_place(ready: HEnv) -> None:
    add(ready, "outcome-key-1")
    rid = records(ready)[0]["id"]
    edited = ready.call("PATCH", f"/v1/history/records/{rid}", "a",
                        {"feedback": {"thumb": "UP", "note": "new note"}})
    assert edited.status_code == 200
    assert [e.note for e in ledger(ready)] == [None]
    assert NOTE_CANARY not in ready.stores_text()


def test_shortening_the_retention_shortens_the_ledger(ready: HEnv) -> None:
    add(ready, "outcome-key-1")
    ready.clock.advance(days=40)
    add(ready, "outcome-key-2", PLACE2)
    assert len(ledger(ready)) == 2
    assert ready.call("PUT", "/v1/history/settings", "a", {"retention_days": 30}).status_code == 200
    assert [e.place_id for e in ledger(ready)] == [PLACE2]


def test_an_entry_expires_with_the_users_retention_and_purge_deletes_it(ready: HEnv) -> None:
    assert ready.call("PUT", "/v1/history/settings", "a", {"retention_days": 30}).status_code == 200
    add(ready, "outcome-key-1")
    entry = ledger(ready)[0]
    assert entry.expires_at == entry.recorded_at + timedelta(days=30)
    ready.clock.advance(days=31)
    assert ledger(ready) == ()  # a read never serves it
    assert ready.runtime.purge_expired()["history"] >= 0
    ready.runtime.history_service.purge()
    text = ready.stores_text()
    assert NOTE_CANARY not in text
