"""Review finding 12: every user label is held to the plain-text policy (bidi, markup, URLs, emails,
phones) by the contract itself and again by ActionService.propose, for every effect."""
from __future__ import annotations

import pytest
from actions_support import GUEST, KEY, PLACE, Env
from pydantic import ValidationError

from app.actions import ActionError, ActionFailure
from app.domain import (
    HistoryRecordEdit,
    OutcomeCheckin,
    PendingActionEffect as E,
    PlanDestination,
    ProposeActionRequest,
    SavedPlaceKind,
    SavedPlaceUpsert,
)

BAD = [
    "evil‮txt.exe",      # bidi override
    "zero​width",        # zero-width space
    "<script>x</script>",     # markup
    "see https://evil.test",  # URL
    "mail me a@evil.test",    # email
    "call 416-555-0199",      # phone
    "+1 (416) 555 0199",      # phone
    "4165550199",             # phone, bare
    "line\nbreak",            # control character
]
GOOD = ["Quiet corner cafe", "Mum's flat", "Cafe 21", "party of 5"]


@pytest.mark.parametrize("label", BAD)
def test_the_contract_rejects_an_unsafe_label_everywhere(label: str) -> None:
    for build in (
        lambda: ProposeActionRequest(workspace_id="w", plan_version=1, effect=E.LAUNCH_HANDOFF,
                                     target=PLACE, user_label=label),
        lambda: HistoryRecordEdit(user_label=label),
        lambda: OutcomeCheckin(place_id=PLACE, outcome="WENT", user_label=label),
        lambda: PlanDestination(place_id=PLACE, user_label=label),
        lambda: SavedPlaceUpsert(place_id=PLACE, user_label=label, label_kind=SavedPlaceKind.HOME,
                                 position={"latitude": 43.653, "longitude": -79.383}),
    ):
        with pytest.raises(ValidationError):
            build()


@pytest.mark.parametrize("label", GOOD)
def test_ordinary_labels_still_pass(label: str) -> None:
    assert ProposeActionRequest(workspace_id="w", plan_version=1, effect=E.LAUNCH_HANDOFF,
                                target=PLACE, user_label=label).user_label == label


@pytest.mark.parametrize("effect", [E.LAUNCH_HANDOFF, E.SET_REMINDER])
@pytest.mark.parametrize("label", BAD)
def test_propose_refuses_an_unsafe_label_even_if_the_contract_was_bypassed(
        effect: E, label: str) -> None:
    env = Env()
    body = env.body(effect)
    forged = ProposeActionRequest.model_construct(**{**body.__dict__, "user_label": label})
    with pytest.raises(ActionError) as caught:
        env.service.propose(GUEST, forged, KEY)
    assert caught.value.failure == ActionFailure.INVALID
    assert env.store.items == {}


def test_propose_refuses_an_unsafe_save_place_label(monkeypatch) -> None:
    env = Env(history=True, monkeypatch=monkeypatch)
    from actions_support import REGISTERED
    forged = ProposeActionRequest.model_construct(
        **{**env.body(E.SAVE_PLACE).__dict__, "user_label": "call 416-555-0199"})
    with pytest.raises(ActionError) as caught:
        env.service.propose(REGISTERED, forged, KEY)
    assert caught.value.failure == ActionFailure.INVALID


def test_the_route_answers_422_for_an_unsafe_label(monkeypatch) -> None:
    from history_support import PLACE as HPLACE
    from history_support import HEnv

    env = HEnv("memory", monkeypatch)
    body = {"workspace_id": env.workspace("a")["id"], "plan_version": 1,
            "effect": "LAUNCH_HANDOFF", "target": HPLACE, "user_label": "call 416-555-0199"}
    response = env.call("POST", "/v1/actions/propose", "a", body, "act-key-0001")
    assert response.status_code == 422
