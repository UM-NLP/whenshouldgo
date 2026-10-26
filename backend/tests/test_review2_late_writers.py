"""Review findings 5 and 13: a writer that is in flight while the account is deleted or a consent is
withdrawn takes its own write back (post-write recheck); delete always re-sweeps; lists ignore
records resting on an inactive consent; a guest cannot grant history consents."""
from __future__ import annotations

from datetime import timedelta

import pytest
from history_support import PLACE, HEnv

from app.domain import (
    ConsentPurpose,
    OutingEvent,
    OutingEventType,
    OutingFeedback,
    OutingRecord,
    OutingStrength,
    PlaceRef,
)

HIST, SAVED = ConsentPurpose.HISTORY_STORE, ConsentPurpose.SAVED_PLACE


@pytest.fixture(params=["memory", "fake-firestore"])
def env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> HEnv:
    e = HEnv(request.param, monkeypatch)
    e.grant_all("a")
    return e


def delete_account_now(env: HEnv, uid: str = "reg-a") -> None:
    """What a concurrent me:delete does BEFORE the late writer's write lands."""
    accounts = env.runtime.account_service
    accounts.store.begin_tombstone(uid, env.clock())
    accounts.sweep(uid)


def withdraw_now(env: HEnv, purpose: ConsentPurpose) -> None:
    assert env.call("DELETE", f"/v1/me/consents/{purpose.value}").status_code == 204


class Hooked:
    """Runs ``before`` once, then delegates (the race: the sweep ran before our write)."""

    def __init__(self, inner, method: str, before) -> None:
        self._inner, self._method, self._before, self._done = inner, method, before, False

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if name != self._method:
            return target

        def call(*args, **kwargs):
            if not self._done:
                self._done = True
                self._before()
            return target(*args, **kwargs)

        return call


def no_history(env: HEnv) -> bool:
    return env.runtime.history_service.store.list("reg-a", env.clock()) == ()


def pending_count(env: HEnv) -> int:
    if env.fs is not None:
        return sum(1 for path, _ in env.fs.documents() if path.split("/")[0] == "pending_actions")
    return len(env.runtime.action_service._store.items)  # noqa: SLF001


def first_check_passes(service, attr: str = "_assert_active"):
    """The pre-write guard passed (the account was not yet deleted when it ran); later ones are real."""
    original = getattr(service, attr)
    state = {"n": 0}

    def guard(*args):
        state["n"] += 1
        if state["n"] == 1:
            return None
        return original(*args)

    setattr(service, attr, guard)


# --- history ------------------------------------------------------------------------------------
def test_a_history_record_written_after_the_account_sweep_is_taken_back(env: HEnv) -> None:
    env.workspace("a")
    service = env.runtime.history_service
    service.store = Hooked(service.store, "put", lambda: delete_account_now(env))
    first_check_passes(service)
    response = env.save_history("a")
    assert response.status_code in (403, 404), response.text
    assert no_history(env)


def test_a_history_record_written_after_the_consent_withdrawal_is_taken_back(env: HEnv) -> None:
    env.workspace("a")
    service = env.runtime.history_service
    service.store = Hooked(service.store, "put", lambda: withdraw_now(env, HIST))
    response = env.save_history("a")
    assert response.status_code == 403 and response.json()["reason"] == "CONSENT_REQUIRED"
    assert no_history(env)


def test_an_outcome_written_after_the_withdrawal_leaves_no_history_record(env: HEnv) -> None:
    service = env.runtime.history_service
    service.store = Hooked(service.store, "put", lambda: withdraw_now(env, HIST))
    env.outcome("a")
    assert env.runtime.history_service.store.list("reg-a", env.clock()) == ()


def test_list_ignores_a_record_resting_on_an_inactive_consent(env: HEnv) -> None:
    assert env.save_history("a").status_code == 200
    rows = env.runtime.history_service.store.list("reg-a", env.clock())
    assert len(rows) == 1
    stale = OutingRecord.model_validate({**rows[0].model_dump(mode="json"),
                                         "id": "out_stale", "consent_id": "consent-gone"})
    env.runtime.history_service.store.put("reg-a", stale, "fp-stale")
    shown = env.call("GET", "/v1/history/records").json()["records"]
    assert [r["id"] for r in shown] == [rows[0].id]


# --- saved places -------------------------------------------------------------------------------
def test_a_saved_place_confirmed_after_the_withdrawal_is_taken_back(env: HEnv) -> None:
    service = env.runtime.saved_place_service
    proposed = env.propose_save("a")
    assert proposed.status_code == 200
    action = proposed.json()
    service.places = Hooked(service.places, "replace", lambda: withdraw_now(env, SAVED))
    confirmed = env.call("POST", f"/v1/actions/{action['id']}/confirm", "a",
                         {"nonce": action["nonce"]}, "save-key-0001")
    assert confirmed.status_code == 403
    assert service.places.list("reg-a") == ()


def test_a_saved_place_written_after_the_account_deletion_is_taken_back(env: HEnv) -> None:
    service = env.runtime.saved_place_service
    action = env.propose_save("a").json()
    service.places = Hooked(service.places, "replace", lambda: delete_account_now(env))
    first_check_passes(service)
    response = env.call("POST", f"/v1/actions/{action['id']}/confirm", "a",
                        {"nonce": action["nonce"]}, "save-key-0001")
    assert response.status_code in (403, 404)
    assert service.places.list("reg-a") == ()


# --- actions ------------------------------------------------------------------------------------
def test_a_proposal_recorded_after_the_withdrawal_is_taken_back(env: HEnv) -> None:
    actions = env.runtime.action_service
    actions._store = Hooked(actions._store, "update", lambda: withdraw_now(env, SAVED))  # noqa: SLF001
    response = env.propose_save("a")
    assert response.status_code == 403
    assert pending_count(env) == 0


def test_a_proposal_recorded_after_the_account_deletion_is_taken_back(env: HEnv) -> None:
    body = {"workspace_id": env.workspace("a")["id"], "plan_version": 1,
            "effect": "LAUNCH_HANDOFF", "target": PLACE}
    actions = env.runtime.action_service
    actions._store = Hooked(actions._store, "update", lambda: delete_account_now(env))  # noqa: SLF001
    first_check_passes(actions)
    response = env.call("POST", "/v1/actions/propose", "a", body, "act-key-0001")
    assert response.status_code in (403, 404)
    assert pending_count(env) == 0


# --- consent grant ------------------------------------------------------------------------------
def test_a_consent_granted_after_the_account_sweep_is_swept_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    for kind in ("memory", "fake-firestore"):
        env = HEnv(kind, monkeypatch)
        ledger = env.runtime.stores.consents
        accounts = env.runtime.account_service
        env.runtime.app.state.consents._ledger = Hooked(  # noqa: SLF001
            ledger, "update", lambda: (accounts.store.begin_tombstone("reg-a", env.clock()),
                                       accounts.sweep("reg-a")))
        response = env.consent("a", HIST)
        assert response.status_code == 403
        assert ledger.read("reg-a") == ()


# --- delete always re-sweeps --------------------------------------------------------------------
def late_record(env: HEnv) -> OutingRecord:
    now = env.clock()
    return OutingRecord(
        id="out_late", owner_id="reg-a", workspace_id="w", plan_version=1, occurred_at=now,
        tz="UTC", local_dow=0, local_daypart="MORNING", place_ref=PlaceRef(place_id=PLACE),
        intent_tag="OTHER", events=(OutingEvent(type=OutingEventType.SELECTED, at=now),),
        feedback=OutingFeedback(), strength=OutingStrength.HANDOFF, consent_id="consent-x",
        retention_expires_at=now + timedelta(days=30))


def test_delete_reruns_the_sweep_even_when_the_tombstone_is_completed(env: HEnv) -> None:
    assert env.save_history("a").status_code == 200
    assert env.call("POST", "/v1/me:delete").status_code == 200
    rows = env.runtime.history_service.store
    rows.put("reg-a", late_record(env), "fp-late")  # a late writer landed after the first sweep
    assert rows.list("reg-a", env.clock())
    again = env.call("POST", "/v1/me:delete")
    assert again.status_code == 200 and again.json()["completed"] is True
    assert rows.list("reg-a", env.clock()) == ()


# --- finding 13: a guest cannot grant history consents -------------------------------------------
@pytest.mark.parametrize("purpose", [HIST, SAVED])
def test_a_guest_cannot_grant_a_history_consent_and_nothing_is_written(
        env: HEnv, purpose: ConsentPurpose) -> None:
    response = env.consent("g", purpose)
    assert response.status_code == 403
    assert response.json()["reason"] == "GUEST_NOT_ALLOWED"
    assert env.runtime.stores.consents.read("guest-a") == ()
