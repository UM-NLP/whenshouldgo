"""Review finding 3: the stored ActionRecord fingerprint is no brute-forceable function of the
user's position or label, yet a same-key different-request conflict is still detected."""
from __future__ import annotations

import dataclasses

import pytest
from actions_support import GUEST, KEY, REGISTERED, Env, T0  # noqa: F401
from actions_support import PLACE  # noqa: F401

from app.actions import ActionError, ActionFailure
from app.domain import PendingActionEffect as E
from app.domain import RoundedPosition

KINDS = ["memory", "firestore"]
POS1 = RoundedPosition(latitude=43.653226, longitude=-79.383184)
POS2 = RoundedPosition(latitude=43.700110, longitude=-79.400400)


def record(env: Env, key: str):
    from app.actions import _action_id
    return env.store.get(REGISTERED.uid, _action_id(REGISTERED.uid, key))


def save(env: Env, key: str, *, position=POS1, label="Home"):
    return env.propose(E.SAVE_PLACE, key=key, principal=REGISTERED, position=position,
                       user_label=label)


@pytest.mark.parametrize("kind", KINDS)
def test_the_fingerprint_does_not_depend_on_position_or_label(kind: str, monkeypatch) -> None:
    env = Env(kind, history=True, monkeypatch=monkeypatch)
    save(env, "key-aaaaaaa1", position=POS1, label="Home")
    save(env, "key-aaaaaaa2", position=POS2, label="Mum's flat")
    assert record(env, "key-aaaaaaa1").fingerprint == record(env, "key-aaaaaaa2").fingerprint


@pytest.mark.parametrize("kind", KINDS)
def test_a_same_key_different_position_or_label_is_still_a_conflict_while_pending(
        kind: str, monkeypatch) -> None:
    env = Env(kind, history=True, monkeypatch=monkeypatch)
    first = save(env, "key-aaaaaaa1")
    assert save(env, "key-aaaaaaa1").id == first.id  # the same request replays
    for changed in (dict(position=POS2), dict(label="Work")):
        with pytest.raises(ActionError) as caught:
            save(env, "key-aaaaaaa1", **changed)
        assert caught.value.failure == ActionFailure.IDEMPOTENCY_CONFLICT


def _flat(rec) -> str:
    return " ".join(str(getattr(rec, f.name)) for f in dataclasses.fields(rec)
                    if f.name != "action") + rec.action.model_dump_json()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("how", ["confirm", "cancel", "expire"])
def test_nothing_derivable_from_position_or_label_remains_after_the_end(
        kind: str, how: str, monkeypatch) -> None:
    env = Env(kind, history=True, monkeypatch=monkeypatch)
    action = save(env, "key-aaaaaaa1", label="CANARYLABEL-77")
    if how == "confirm":
        # The sink here is a counting double: confirming scrubs the record like the real sink.
        env.service.confirm(REGISTERED, action.id, action.nonce)
    elif how == "cancel":
        env.service.cancel(REGISTERED, action.id)
    else:
        env.clock.advance(minutes=16)
        with pytest.raises(ActionError):
            env.service.confirm(REGISTERED, action.id, action.nonce)
    rec = record(env, "key-aaaaaaa1")
    text = _flat(rec)
    assert "CANARYLABEL-77" not in text and "43.653" not in text and "-79.383" not in text
    assert rec.position is None
    # The stored digest is the same for any position and label: nothing to brute-force.
    other = save(env, "key-aaaaaaa2", position=POS2, label="Other label")
    assert other and record(env, "key-aaaaaaa2").fingerprint == rec.fingerprint
    # A replay of the original request is still the same action, not a conflict.
    assert save(env, "key-aaaaaaa1", label="CANARYLABEL-77").id == action.id
