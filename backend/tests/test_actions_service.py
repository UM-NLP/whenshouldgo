"""ActionService: propose, confirm, cancel, activation. Over the memory and Firestore-fake stores."""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from actions_support import (
    GUEST, KEY, LABEL, OTHER, PLACE, REGISTERED, T0, CountingSink, Env, workspace,
)

from app import telemetry as t
from app.actions import ActionError, ActionFailure, ActionService
from app.domain import MAX_PENDING_ACTION_TTL, SavedPlaceKind
from app.domain import PendingActionEffect as E
from app.handoff import build_maps_url

KINDS = ["memory", "firestore"]


@pytest.fixture(params=KINDS)
def env(request: pytest.FixtureRequest) -> Env:
    return Env(request.param)


def fails(failure: ActionFailure):
    return pytest.raises(ActionError, check=lambda e: e.failure == failure)


# -- propose -------------------------------------------------------------------------------------
def test_propose_is_server_authored_and_bounded(env: Env) -> None:
    action = env.propose(E.SET_REMINDER)
    assert action.effect == E.SET_REMINDER and action.target == PLACE
    assert action.expires_at - action.created_at == MAX_PENDING_ACTION_TTL == timedelta(minutes=15)
    assert action.confirmed_at is None and action.idempotency_key == KEY
    assert LABEL in action.preview  # the user's own label, typed
    assert len(action.nonce) >= 16


def test_nonce_is_random_per_action(env: Env) -> None:
    nonces = {env.propose(E.LAUNCH_HANDOFF, key=f"idem-key-{i:04d}").nonce for i in range(20)}
    assert len(nonces) == 20


def test_same_key_returns_same_action_without_second_record_or_event(env: Env) -> None:
    first = env.propose()
    second = env.propose()
    assert first == second
    assert [e.event for e in env.trace.events].count(t.EventType.ACTION_PROPOSED) == 1


def test_same_key_with_different_request_is_a_conflict(env: Env) -> None:
    env.propose(E.LAUNCH_HANDOFF)
    with fails(ActionFailure.IDEMPOTENCY_CONFLICT):
        env.propose(E.SET_REMINDER)


def test_other_tenant_same_key_gets_its_own_action(env: Env) -> None:
    a = env.propose(principal=GUEST)
    b = env.propose(principal=OTHER)
    assert a.id != b.id and a.nonce != b.nonce


@pytest.mark.parametrize("key", ["", "short", "x" * 201])
def test_idempotency_key_is_required_and_bounded(env: Env, key: str) -> None:
    with fails(ActionFailure.INVALID):
        env.service.propose(GUEST, env.body(), key)


def test_reminder_needs_a_future_same_day_due_time(env: Env) -> None:
    with fails(ActionFailure.INVALID):
        env.propose(E.SET_REMINDER, due_at=None)
    with fails(ActionFailure.INVALID):
        env.propose(E.SET_REMINDER, due_at=T0 - timedelta(minutes=1))
    with fails(ActionFailure.INVALID):
        env.propose(E.SET_REMINDER, due_at=T0 + timedelta(days=3))


def test_handoff_takes_no_due_time_and_save_needs_label(env: Env) -> None:
    with fails(ActionFailure.INVALID):
        env.propose(E.LAUNCH_HANDOFF, due_at=T0 + timedelta(hours=1))
    with fails(ActionFailure.INVALID):
        env.propose(E.SAVE_PLACE, user_label=None)


@pytest.mark.parametrize("target", ["a:b", "a.b", "..", "places.x"])
def test_target_must_look_like_a_place_id(env: Env, target: str) -> None:
    with fails(ActionFailure.INVALID):
        env.propose(E.LAUNCH_HANDOFF, target=target)


def test_workspace_must_belong_to_the_tenant_and_plan_match(env: Env) -> None:
    with fails(ActionFailure.NOT_FOUND):
        env.propose(workspace_id="someone-elses-ws")
    env.workspaces.put(GUEST.uid, "default", workspace(GUEST.uid, plan=2), env.clock(), None)
    with fails(ActionFailure.STALE_PLAN):
        env.propose(plan_version=1)
    assert env.propose(plan_version=2)


def test_save_place_proposal_is_created_but_disabled_without_history(env: Env) -> None:
    action = env.propose(E.SAVE_PLACE)
    with fails(ActionFailure.DISABLED):
        env.service.confirm(GUEST, action.id, action.nonce)
    assert env.sink.calls == []
    # nothing consumed: the nonce still works once history is on
    assert env.store.get(GUEST.uid, action.id).status.value == "PENDING"


# -- confirm: boundaries -------------------------------------------------------------------------
def test_wrong_nonce_is_rejected_with_no_effect(env: Env) -> None:
    action = env.propose(E.LAUNCH_HANDOFF)
    with fails(ActionFailure.NONCE_MISMATCH):
        env.service.confirm(GUEST, action.id, "w" * 20)
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 0
    assert env.store.get(GUEST.uid, action.id).status.value == "PENDING"


def test_nonce_is_locked_after_repeated_failures(env: Env) -> None:
    action = env.propose()
    for _ in range(5):
        with fails(ActionFailure.NONCE_MISMATCH):
            env.service.confirm(GUEST, action.id, "w" * 20)
    with fails(ActionFailure.NONCE_MISMATCH):  # even the right nonce no longer works
        env.service.confirm(GUEST, action.id, action.nonce)


def test_expired_action_is_rejected_with_no_effect(env: Env) -> None:
    action = env.propose()
    env.clock.advance(minutes=15, seconds=1)
    with fails(ActionFailure.EXPIRED):
        env.service.confirm(GUEST, action.id, action.nonce)
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 0


def test_cross_tenant_confirm_is_not_found_even_with_the_nonce(env: Env) -> None:
    action = env.propose(principal=GUEST)
    with fails(ActionFailure.NOT_FOUND):
        env.service.confirm(OTHER, action.id, action.nonce)
    assert env.store.get(GUEST.uid, action.id).status.value == "PENDING"


def test_cancelled_action_cannot_be_confirmed(env: Env) -> None:
    action = env.propose()
    assert env.service.cancel(GUEST, action.id) is True
    assert env.service.cancel(GUEST, action.id) is True  # idempotent
    with fails(ActionFailure.CANCELLED):
        env.service.confirm(GUEST, action.id, action.nonce)
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 0


def test_cannot_cancel_a_confirmed_action_or_someone_elses(env: Env) -> None:
    action = env.propose()
    env.service.confirm(GUEST, action.id, action.nonce)
    with fails(ActionFailure.ALREADY_CONFIRMED):
        env.service.cancel(GUEST, action.id)
    with fails(ActionFailure.NOT_FOUND):
        env.service.cancel(OTHER, action.id)


def test_unknown_action_is_not_found(env: Env) -> None:
    with fails(ActionFailure.NOT_FOUND):
        env.service.confirm(GUEST, "act_missing", "n" * 20)


def test_stale_plan_at_commit_invalidates_the_approval(env: Env) -> None:
    env.workspaces.put(GUEST.uid, "default", workspace(GUEST.uid, plan=1), env.clock(), None)
    action = env.propose(plan_version=1)
    env.workspaces.put(GUEST.uid, "default", workspace(GUEST.uid, plan=2), env.clock(), None)
    with fails(ActionFailure.STALE_PLAN):
        env.service.confirm(GUEST, action.id, action.nonce)
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 0


def test_idempotency_key_on_confirm_must_match(env: Env) -> None:
    action = env.propose()
    with fails(ActionFailure.IDEMPOTENCY_CONFLICT):
        env.service.confirm(GUEST, action.id, action.nonce, "another-key-9999")
    assert env.service.confirm(GUEST, action.id, action.nonce, KEY)


# -- confirm: effects ----------------------------------------------------------------------------
def test_handoff_confirm_returns_a_pinned_url_from_the_place_id_only(env: Env) -> None:
    action = env.propose(E.LAUNCH_HANDOFF)
    result = env.service.confirm(GUEST, action.id, action.nonce)
    assert result.handoff_url == build_maps_url(PLACE) and result.reminder is None
    assert result.action.confirmed_at == T0
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 1


def test_reminder_confirm_returns_a_typed_local_item(env: Env) -> None:
    action = env.propose(E.SET_REMINDER)
    result = env.service.confirm(GUEST, action.id, action.nonce)
    item = result.reminder
    assert item is not None and result.handoff_url is None
    assert (item.place_id, item.user_label, item.due_at) == (PLACE, LABEL, T0 + timedelta(hours=2))
    assert item.action_id == action.id
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 1


def test_double_confirm_yields_exactly_one_effect_and_replays_the_result(env: Env) -> None:
    action = env.propose(E.SET_REMINDER)
    first = env.service.confirm(GUEST, action.id, action.nonce)
    second = env.service.confirm(GUEST, action.id, action.nonce)
    assert first == second
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 1
    confirmed = [e for e in env.trace.events if e.event == t.EventType.ACTION_CONFIRMED]
    assert [e.replay for e in confirmed] == [False, True]
    assert [e.event for e in env.trace.events].count(t.EventType.TRIP_ACTIVATED) == 1


def test_confirm_after_confirm_with_wrong_nonce_is_still_rejected(env: Env) -> None:
    action = env.propose()
    env.service.confirm(GUEST, action.id, action.nonce)
    with fails(ActionFailure.NONCE_MISMATCH):
        env.service.confirm(GUEST, action.id, "w" * 20)


def test_concurrent_confirms_produce_one_effect(env: Env) -> None:
    action = env.propose(E.SET_REMINDER)
    outcomes: list[bool] = []

    def go() -> None:
        outcomes.append(env.service.confirm(GUEST, action.id, action.nonce).reminder is not None)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert all(outcomes) and len(outcomes) == 8
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 1
    firsts = [e for e in env.trace.events
              if e.event == t.EventType.ACTION_CONFIRMED and not e.replay]
    assert len(firsts) == 1


def test_trip_is_counted_once_per_workspace_across_actions(env: Env) -> None:
    a = env.propose(E.LAUNCH_HANDOFF, key="idem-key-0001")
    b = env.propose(E.SET_REMINDER, key="idem-key-0002")
    env.service.confirm(GUEST, a.id, a.nonce)
    env.service.confirm(GUEST, b.id, b.nonce)
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 1


# -- gate ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("effect", [E.LAUNCH_HANDOFF, E.SET_REMINDER])
def test_a_gated_user_can_still_confirm_handoff_and_reminder(effect: E) -> None:
    env = Env("memory", trips=1)
    env.quotas.activate_trip(GUEST, "another-workspace")  # the trip allowance is used up
    action = env.propose(effect)
    result = env.service.confirm(GUEST, action.id, action.nonce)
    assert result.handoff_url or result.reminder
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 1  # no second trip counted


def test_activate_counts_once_and_returns_the_typed_gate_at_the_limit() -> None:
    env = Env("memory", trips=1)
    first = env.service.activate(GUEST, "ws1", 1)
    assert first.counted and not first.gated
    again = env.service.activate(GUEST, "ws1", 1)
    assert not again.counted and not again.gated
    env.workspaces.put(GUEST.uid, "default", workspace(GUEST.uid, "ws2"), env.clock(), None)
    blocked = env.service.activate(GUEST, "ws2", 1)
    assert blocked.gated and not blocked.counted
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 1


def test_activate_unknown_workspace_is_not_found(env: Env) -> None:
    with fails(ActionFailure.NOT_FOUND):
        env.service.activate(GUEST, "nope", 1)
    assert env.quotas.usage_for(GUEST.uid).activated_trips == 0


def test_registered_users_confirm_the_same_way(env: Env) -> None:
    action = env.propose(E.SET_REMINDER, principal=REGISTERED)
    assert env.service.confirm(REGISTERED, action.id, action.nonce).reminder is not None


# -- SAVE_PLACE with history on --------------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_save_place_runs_the_sink_once_when_history_is_active(kind: str, monkeypatch) -> None:
    env = Env(kind, history=True, monkeypatch=monkeypatch)
    action = env.propose(E.SAVE_PLACE)
    env.service.confirm(GUEST, action.id, action.nonce)
    env.service.confirm(GUEST, action.id, action.nonce)
    assert env.sink.calls == [dict(uid=GUEST.uid, place_id=PLACE, user_label="Home",
                                   label_kind=SavedPlaceKind.HOME, action_id=action.id)]


def test_a_failing_sink_reopens_the_action(monkeypatch) -> None:
    class Boom(CountingSink):
        def save_place(self, *a, **k) -> None:  # noqa: ANN002
            raise RuntimeError("down")

    env = Env("memory", history=True, sink=Boom(), monkeypatch=monkeypatch)
    action = env.propose(E.SAVE_PLACE)
    with pytest.raises(RuntimeError):
        env.service.confirm(GUEST, action.id, action.nonce)
    assert env.store.get(GUEST.uid, action.id).status.value == "PENDING"


def test_default_sink_is_disabled(monkeypatch) -> None:
    env = Env("memory", history=True, monkeypatch=monkeypatch)
    service = ActionService(env.store, env.quotas, clock=env.clock,
                            runtime_config=env.service.runtime_config)
    action = service.propose(GUEST, env.body(E.SAVE_PLACE), KEY)
    with fails(ActionFailure.DISABLED):
        service.confirm(GUEST, action.id, action.nonce)
    assert env.store.get(GUEST.uid, action.id).status.value == "PENDING"


# -- purge ---------------------------------------------------------------------------------------
def test_expired_records_purge(env: Env) -> None:
    action = env.propose()
    env.clock.advance(days=2)
    assert env.service.purge() == 1
    with fails(ActionFailure.NOT_FOUND):
        env.service.confirm(GUEST, action.id, action.nonce)


# -- telemetry -----------------------------------------------------------------------------------
def test_rejections_emit_enum_only_events(env: Env) -> None:
    action = env.propose()
    with fails(ActionFailure.NONCE_MISMATCH):
        env.service.confirm(GUEST, action.id, "w" * 20)
    with fails(ActionFailure.NOT_FOUND):
        env.service.confirm(GUEST, "act_missing", "n" * 20)
    rejected = [e for e in env.trace.events if e.event == t.EventType.ACTION_REJECTED]
    assert [e.reason.value for e in rejected] == ["NONCE_MISMATCH", "NOT_FOUND"]
    assert rejected[0].effect == E.LAUNCH_HANDOFF and rejected[1].effect is None
