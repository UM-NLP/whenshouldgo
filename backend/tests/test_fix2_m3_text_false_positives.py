"""M3: ordinary same-day outing facts must pass. A single bad fact is DROPPED (typed warning
FACT_DROPPED) and the rest of the patch is kept; only structural violations reject the patch.
ZWJ/ZWNJ no longer ban a fact value, so emoji sequences and French/Persian text are fine."""
from __future__ import annotations

import pytest
from wiring_support import Env, RecordingClient, ScriptedGateway, fact, frame_turn

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config

from app.domain import NextAction, TripWorkspace, WorkspacePatch
from app.frame import FrameIntent, SearchArgs
from app.patching import apply_patch
from app.textsafety import plain_text_problem

# Realistic same-day outing facts, English and French. Every one must be accepted.
LEGITIMATE = [
    "30 minute drive",
    "4 people on the way",
    "6 pm go to the place",
    "2 hours drive",
    "15 min walk",
    "party of 5 near the park",
    "two adults and one child",
    "vegetarian options needed",
    "wheelchair accessible entrance",
    "back home by 9 pm",
    "budget about 40 dollars per person",
    "prefers outdoor patio seating",
    "avoid long lineups",
    "leaving from Union Station at 10 am",
    "dog friendly",
    "stroller friendly route",
    "no more than 20 minutes by transit",
    "likes live jazz on Saturday night",
    "gluten free brunch",
    "5 stars preferred",
    "rating above 4.5",
    "kids aged 3 and 7",
    "parking is a must",
    "near the lake in the afternoon",
    "table for 6 at 7:30",
    "10 km bike ride",
    "$25 each",
    "a 20 minute walk from the subway",
    "arrive before 5 pm and return by 11 pm",
    "trajet de 30 minutes en voiture",
    "souper pour 4 personnes",
    "près du parc ce soir",
    "brunch sans gluten",
    "à 18 h on va au restaurant",
    "deux adultes et un enfant",
    "marche de 15 minutes",
    "place de stationnement requise",
    "route panoramique le long du lac",
    "rue calme préférée",
    "café près de la gare",
    "Queen Street",
    "Rue Saint-Denis",
    "👨‍👩‍👧 family outing",
    "🧑🏽‍🍳 cooking class",
]


@pytest.mark.parametrize("text", LEGITIMATE)
def test_legitimate_outing_facts_are_plain(text) -> None:
    assert not plain_text_problem(text, allow_joiners=True), text  # fact values allow ZWJ/ZWNJ


@pytest.mark.parametrize("text", [t for t in LEGITIMATE if len(t) <= 80 and "‍" not in t])
def test_legitimate_text_is_accepted_as_a_search_query(text) -> None:
    assert SearchArgs(text_query=text).text_query == text


def workspace() -> TripWorkspace:
    return TripWorkspace(id="ws-1", owner_id="u1", version=3)


def patch(*facts) -> WorkspacePatch:
    return WorkspacePatch(workspace_id="ws-1", base_version=3, next_action=NextAction.SEARCH,
                          upsert_facts=list(facts))


def test_legitimate_facts_survive_a_patch() -> None:
    facts = [fact(f"f{i}", text) for i, text in enumerate(LEGITIMATE[:10])]
    kept = {f.id for f in apply_patch(workspace(), patch(*facts)).facts}
    assert {f.id for f in facts} <= kept


def test_one_bad_fact_is_dropped_and_the_rest_is_kept() -> None:
    from app.patching import apply_patch_checked

    good, also_good = fact("party", "two adults"), fact("pace", "slow and easy")
    bad = fact("note", "see https://evil.example/x")
    result, dropped = apply_patch_checked(workspace(), patch(good, bad, also_good))
    assert {f.id for f in result.facts} == {"party", "pace"}
    assert dropped == 1 and result.version == 4


def test_structural_violations_still_reject_the_whole_patch() -> None:
    from app.patching import PatchRejected

    with pytest.raises(PatchRejected):
        apply_patch(workspace(), patch(fact("party", "two"), fact("budget", "x", hard=True)))


def test_a_dropped_fact_adds_a_typed_warning_and_the_turn_succeeds() -> None:
    config = make_config()
    gateway = ScriptedGateway(
        [frame_turn(config, intent=FrameIntent.OTHER,
                    facts=[fact("party", "two adults"), fact("note", "mailto:a@b.ca")])], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    response = env.post()
    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == ["FACT_DROPPED"]
    facts = {f.id for f in env.orchestrator.workspaces[("guest-a", "default")].facts}
    assert facts == {"party"}


@pytest.mark.parametrize("text", [
    "zero​width", "word⁠joiner", "bom﻿here", "bidi ‮override",
    "isolate ⁦text", "fillerㅤhere", "soft­hyphen", "line one\nline two",
])
def test_hidden_characters_are_still_rejected_in_fact_values(text) -> None:
    from app.patching import apply_patch_checked

    _, dropped = apply_patch_checked(workspace(), patch(fact("note", text)))
    assert dropped == 1, text


def test_joiners_alone_do_not_ban_a_value() -> None:
    from app.patching import apply_patch_checked

    for text in ("👨‍👩‍👧 trip", "می‌خواهم کافه", "ok‍ok"):
        _, dropped = apply_patch_checked(workspace(), patch(fact("note", text)))
        assert dropped == 0, text


def test_a_search_query_that_fails_the_plain_text_check_is_still_rejected() -> None:
    from app.frame import ArgumentRejected, search_arguments

    # the query is checked by the server's plan (the pipeline then degrades to a clarification)
    with pytest.raises(ArgumentRejected):
        search_arguments(SearchArgs(text_query="see https://evil.example/x"),
                         "see https://evil.example/x")
