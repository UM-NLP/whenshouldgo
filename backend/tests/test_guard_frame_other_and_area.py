"""FRAME intent OTHER from a TRAVEL-classified message, plus the two FRAME query items:
an empty area is no area (no AREA_DROPPED), and the area joins the query with a connector in
the user's language ("near" / "près de")."""
from __future__ import annotations

from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, frame_turn, search
from maps_simulator import SyntheticMapsClient

from app.frame import FrameIntent, plan_search, search_arguments


def test_intent_other_for_a_message_the_guard_passed_is_a_fixed_redirect_without_tools() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER)], config)
    maps = RecordingClient(SyntheticMapsClient())
    env = Env(gateway=gateway, discovery=maps, config=config)
    response = env.post({"message": "hmm, what about Saturday"})
    body = response.json()
    assert response.status_code == 200 and body["domain"] == "TRAVEL"
    assert body["discovery"] is None and maps.calls == []
    # One travel-focused clarification, never general conversation (it is a completed turn).
    assert "Toronto/GTA" in body["reply"] and "starting from" in body["reply"]
    assert "outing" in body["reply"].lower()
    assert env.turns() == 1


def test_a_guard_refusal_makes_no_model_call_and_a_french_request_reaches_frame() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    assert env.post({"message": "Écris du code Python"}).json()["domain"] == "OUT_OF_SCOPE"
    assert gateway.requests == []
    assert env.post({"message": "Je cherche un café tranquille près du Vieux-Port"}, key="k2"
                    ).json()["domain"] == "TRAVEL"
    assert len(gateway.requests) == 1


# --- empty area ----------------------------------------------------------------------------
def test_an_empty_area_is_no_area_and_not_a_drop() -> None:
    for empty in ("", "   "):
        plan = plan_search(search("brunch", area=empty), "brunch in Leslieville")
        assert plan.area_dropped is False
        assert plan.arguments["text_query"] == "brunch"


def test_an_unusable_non_empty_area_is_still_a_drop() -> None:
    plan = plan_search(search("brunch", area="Paris"), "brunch in Leslieville")
    assert plan.area_dropped is True


# --- connector -----------------------------------------------------------------------------
def test_english_area_uses_near() -> None:
    assert search_arguments(search("brunch", area="Leslieville"), "brunch in Leslieville"
                            )["text_query"] == "brunch near Leslieville"


def test_french_area_uses_pres_de() -> None:
    args = search_arguments(search("brunch", area="Leslieville", language_code="fr"),
                            "je veux un brunch dans Leslieville")
    assert args["language_code"] == "fr"
    assert args["text_query"] == "brunch près de Leslieville"


def test_french_is_decided_by_the_server_not_only_the_model() -> None:
    args = search_arguments(search("café tranquille", area="Vieux-Port", language_code="en"),
                            "Je cherche un café tranquille près du Vieux-Port")
    assert args["language_code"] == "fr"
    # the query already ends with the area, so no connector and no duplicate
    args = search_arguments(search("café tranquille Vieux-Port", area="Vieux-Port"),
                            "Je cherche un café tranquille près du Vieux-Port")
    assert args["text_query"] == "café tranquille Vieux-Port"


def test_the_no_duplicate_rule_is_kept_in_both_languages() -> None:
    assert search_arguments(search("brunch Leslieville", area="Leslieville"),
                            "brunch in Leslieville")["text_query"] == "brunch Leslieville"
    assert search_arguments(search("brunch Leslieville", area="Leslieville", language_code="fr"),
                            "je veux un brunch a Leslieville")["text_query"] == "brunch Leslieville"
