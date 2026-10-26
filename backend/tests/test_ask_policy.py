"""Item 12.3: the fixed 'near home' question (decision 24). Deterministic, server-authored text.

Asked only when the user's message references home/work AND no such saved place exists AND the
30-day post-decline cooldown has passed AND the user has not chosen "don't ask again". Never
inferred from visit patterns (decision 20) and never model-generated.
"""
from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from history_support import T0

from app import ask_policy
from app import domain as d
from app.ask_policy import ASK_COOLDOWN, QUESTIONS, decide, referenced_kinds
from app.stores.saved_places_store import AskRecord

H, W = d.SavedPlaceKind.HOME, d.SavedPlaceKind.WORK


@pytest.mark.parametrize("message,expected", [
    ("a quiet cafe near home", (H,)),
    ("somewhere close to my home", (H,)),
    ("things to do around home tonight", (H,)),
    ("a park nearby home", (H,)),
    ("lunch near work", (W,)),
    ("a bar near my office after 5", (W,)),
    ("coffee next to the office", (W,)),
    ("NEAR HOME please", (H,)),
    ("near   my\thome", (H,)),
    ("a restaurant near home or near work", (H, W)),
    # French
    ("un café près de chez moi", (H,)),
    ("un parc proche de la maison", (H,)),
    ("manger autour de chez moi", (H,)),
    ("un resto à côté de mon travail", (W,)),
    ("un café près du bureau", (W,)),
    ("PRÈS DE CHEZ MOI", (H,)),
])
def test_references_are_detected(message: str, expected: tuple[d.SavedPlaceKind, ...]) -> None:
    assert referenced_kinds(message) == expected


@pytest.mark.parametrize("message", [
    "a quiet cafe in Toronto",
    "I work near the harbour",  # 'work' as a verb, no home/work place reference
    "does this work for kids",
    "a homework help centre",
    "a house party venue",
    "not near home, somewhere far",
    "pas près de chez moi",
    "far from home",
    "home improvement store",
    "workshop near the park",
    "an artwork exhibit",
    "",
])
def test_conservative_no_reference(message: str) -> None:
    assert referenced_kinds(message) == ()


def test_the_question_text_is_fixed_and_english() -> None:
    assert QUESTIONS[H] == "Is where you are now your home? Save it as Home?"
    assert QUESTIONS[W] == "Is where you are now your work? Save it as Work?"
    assert set(QUESTIONS) == {H, W}


def test_ask_when_referenced_and_nothing_saved() -> None:
    prompt = decide("a cafe near home", saved=set(), states={}, now=T0)
    assert prompt.ask is True and prompt.label_kind == H and prompt.question == QUESTIONS[H]
    assert prompt.kind == "saved_place_prompt"


def test_no_ask_without_a_reference() -> None:
    prompt = decide("a cafe in Montreal", saved=set(), states={}, now=T0)
    assert prompt.ask is False and prompt.label_kind is None and prompt.question is None


def test_no_ask_when_that_label_is_saved() -> None:
    assert decide("near home", saved={H}, states={}, now=T0).ask is False
    # the other label is still asked
    prompt = decide("near home or near work", saved={H}, states={}, now=T0)
    assert prompt.ask is True and prompt.label_kind == W


def test_cooldown_after_a_decline_is_thirty_days() -> None:
    assert ASK_COOLDOWN == timedelta(days=30)
    states = {H: AskRecord(declined_at=T0)}
    assert decide("near home", saved=set(), states=states, now=T0 + timedelta(days=29)).ask is False
    assert decide("near home", saved=set(), states=states,
                  now=T0 + timedelta(days=30, seconds=1)).ask is True
    # a decline of Home does not silence Work
    assert decide("near work", saved=set(), states=states, now=T0).ask is True


def test_dont_ask_again_is_permanent() -> None:
    states = {H: AskRecord(declined_at=None, dont_ask_again=True)}
    for days in (0, 31, 400):
        assert decide("near home", saved=set(), states=states,
                      now=T0 + timedelta(days=days)).ask is False


def test_never_asks_for_a_custom_label() -> None:
    assert decide("near the gym", saved=set(), states={}, now=T0).ask is False


def test_oversized_messages_are_handled_without_backtracking_blowup() -> None:
    message = ("near " * 40_000) + "home"
    assert isinstance(referenced_kinds(message), tuple)


def test_the_module_is_deterministic_and_model_free() -> None:
    source = inspect.getsource(ask_policy)
    for forbidden in ("gateway", "ModelGateway", "openrouter", "random", "datetime.now"):
        assert forbidden not in source
