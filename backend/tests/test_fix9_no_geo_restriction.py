"""Fix 9 (owner decision 2026-10-17): no geographic restriction. No default location bias, no
region_code sent to Google, language codes broadened, and a discovery request with no usable area
(none given, or the area dropped) never calls Google: it is the fixed NEEDS_AREA clarification,
a free turn exactly like QUERY_REJECTED."""
from __future__ import annotations

from datetime import timedelta

import pytest
from maps_simulator import SyntheticMapsClient
from pydantic import ValidationError
from test_gateway_support import make_config
from wiring_support import (
    CANARY,
    Env,
    RecordingClient,
    ScriptedGateway,
    canary_payload,
    deep_text,
    frame_turn,
)

import app.frame as frame
from app import telemetry as t
from app.discovery import TOOL_SPECS, ToolId
from app.domain import TurnWarning
from app.frame import FrameIntent, FrameProposal, SearchArgs, plan_search

EN_REPLY = ('Where should I look? Tell me a city, neighbourhood or landmark, for example '
            '"quiet vegetarian dinner near Union Station".')
FR_REPLY = ('Où dois-je chercher ? Indiquez une ville, un quartier ou un lieu, par exemple '
            '« café tranquille près du Vieux-Port ».')
MSG = "Something fun for the kids on Saturday morning"


# --- no default bias, no region_code ---------------------------------------------------------
def test_the_default_bias_is_gone() -> None:
    for name in ("DEFAULT_SEARCH_BIAS_CENTER", "DEFAULT_SEARCH_BIAS_RADIUS_METERS",
                 "default_location_bias"):
        assert not hasattr(frame, name)


def test_a_named_area_search_sends_no_bias_and_no_region_code() -> None:
    plan = plan_search(SearchArgs(text_query="quiet cafe", area="Union Station"),
                       "a quiet cafe near Union Station")
    assert set(plan.arguments) == {"text_query", "language_code"}
    assert plan.has_area is True


@pytest.mark.parametrize("area", [None, "30 minute drive", ""])
def test_no_usable_area_is_reported_not_biased(area) -> None:
    plan = plan_search(SearchArgs(text_query="fun kids activity", area=area), MSG + " 30 minute drive")
    assert plan.has_area is False and set(plan.arguments) == {"text_query", "language_code"}


def test_a_non_latin_area_that_the_user_wrote_works() -> None:
    plan = plan_search(SearchArgs(text_query="тихое кафе",
                                  area="Красная площадь",
                                  language_code="ru"),
                       "тихое кафе рядом "
                       "Красная площадь")
    assert plan.has_area is True and plan.arguments["language_code"] == "ru"
    assert "location_bias" not in plan.arguments


@pytest.mark.parametrize("extra", [
    {"region_code": "CA"}, {"region_code": "US"}, {"location_bias": {"circle": {}}},
    {"latitude": 43.7}, {"lng": -79.4},
])
def test_the_model_cannot_supply_region_bias_or_coordinates(extra) -> None:
    with pytest.raises(ValidationError):
        SearchArgs(text_query="ok", **extra)
    with pytest.raises(ValidationError):
        FrameProposal.model_validate({"intent": "DISCOVERY", "search": {"text_query": "ok", **extra}})
    schema = str(FrameProposal.model_json_schema())
    assert "region_code" not in schema and "location_bias" not in schema


def test_search_places_declares_no_region_code_but_keeps_its_bias_shape() -> None:
    props = TOOL_SPECS[ToolId.SEARCH_PLACES].parameters["properties"]
    assert "region_code" not in props and "location_bias" in props and "language_code" in props
    assert "region_code" in TOOL_SPECS[ToolId.RESOLVE_NAMES].parameters["properties"]


# --- language codes --------------------------------------------------------------------------
@pytest.mark.parametrize("claimed,message,expected", [
    ("es", "un caf tranquilo cerca de Sol", "es"),
    ("ja", "cafe near Shibuya", "ja"),
    ("zh", "cafe near Shibuya", "zh"),
    ("pt-BR", "cafe near Shibuya", "pt"),
    ("ZH_cn", "cafe near Shibuya", "zh"),
    ("xx", "cafe near Shibuya", "en"),
    ("fil", "cafe near Shibuya", "en"),
    ("", "cafe near Shibuya", "en"),
    ("en", "je cherche un café près de chez moi", "fr"),  # the French hint still wins
    ("fr", "cafe near Shibuya", "en"),  # no French hint at all: the server stays on its own
])
def test_language_codes_use_the_documented_allowlist(claimed, message, expected) -> None:
    got = plan_search(SearchArgs(text_query="cafe", area="Shibuya", language_code=claimed),
                      message).arguments["language_code"]
    assert got == expected or (claimed == "fr" and got == "fr")


def test_the_allowlist_is_the_documented_iso_639_1_set() -> None:
    expected = set(("en fr es pt de it nl sv da no fi pl cs sk hu ro bg el tr ru uk ar he fa hi bn "
                    "pa ur ta te ml mr gu th vi id ms zh ja ko").split())
    assert set(frame.ALLOWED_LANGUAGES) == expected


# --- the pipeline: NEEDS_AREA ------------------------------------------------------------------
def env_for(*turns, guest_turns: int = 5, **kw) -> tuple[Env, ScriptedGateway, RecordingClient]:
    config = make_config()
    gateway = ScriptedGateway([turn(config) for turn in turns], config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config, guest_turns=guest_turns, **kw)
    maps.inner._now = env.clock  # noqa: SLF001
    return env, gateway, maps


def no_area(config, text_query="fun kids activity", **kw):
    return frame_turn(config, search_args=SearchArgs(text_query=text_query, **kw))


def test_the_warning_exists() -> None:
    assert TurnWarning("NEEDS_AREA") is TurnWarning.NEEDS_AREA


def test_no_area_asks_where_and_never_calls_google() -> None:
    env, gateway, maps = env_for(no_area)
    response = env.post({"message": MSG})
    body = response.json()
    assert response.status_code == 200
    assert body["warnings"] == ["NEEDS_AREA"] and body["discovery"] is None
    assert body["reply"] == EN_REPLY
    assert maps.calls == [] and len(gateway.requests) == 1


def test_a_dropped_area_is_treated_as_no_area() -> None:
    env, _, maps = env_for(lambda c: no_area(c, area="30 minute drive"))
    body = env.post({"message": MSG + ", a 30 minute drive"}).json()
    assert body["warnings"] == ["NEEDS_AREA"] and maps.calls == []


def test_the_clarification_is_in_the_server_decided_language() -> None:
    env, _, maps = env_for(lambda c: no_area(c, "café tranquille", language_code="fr"))
    body = env.post({"message": "Je cherche un café tranquille pour ce soir"}).json()
    assert body["reply"] == FR_REPLY and body["warnings"] == ["NEEDS_AREA"] and maps.calls == []
    env2, _, _ = env_for(lambda c: no_area(c, language_code="fr"))  # the model says fr, the user wrote English
    assert env2.post({"message": MSG}).json()["reply"] == EN_REPLY


def test_needs_area_is_a_free_costed_user_attributed_turn() -> None:
    env, _, _ = env_for(no_area)
    assert env.post({"message": MSG}).status_code == 200
    assert env.turns() == 0 and env.reserved() == set()
    assert env.quotas.usage_for("guest-a").committed_keys == set()
    entries = env.costs.entries()
    assert len(entries) == 1 and entries[0].input_tokens == 400
    assert str(getattr(entries[0].attribution, "value", entries[0].attribution)) == "USER"
    assert env.costs.free_turns("guest-a", window=timedelta(hours=1)) == 1
    assert [e.outcome for e in env.events(t.QuotaEvent)] == [
        t.QuotaOutcome.RESERVED, t.QuotaOutcome.RELEASED]
    rejections = env.events(t.ValidationRejection)
    assert len(rejections) == 1 and rejections[0].reason == t.ValidationReason.NEEDS_AREA


def test_needs_area_counts_toward_the_free_turn_limiter() -> None:
    config = make_config(max_free_turns_per_hour=2)
    gateway = ScriptedGateway([no_area(config)] * 3, config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    codes = [env.post({"message": MSG}, key=f"k{i}").status_code for i in range(3)]
    assert codes == [200, 200, 429]


def test_a_replay_of_needs_area_is_the_same_reply_without_rerunning() -> None:
    env, gateway, maps = env_for(no_area)
    first = env.post({"message": MSG}, key="dup")
    second = env.post({"message": MSG}, key="dup")
    assert second.json() == first.json() and len(gateway.requests) == 1 and maps.calls == []
    _, record = env.orchestrator._committed[("guest-a", "dup")]  # noqa: SLF001
    assert CANARY not in deep_text(record)


def test_a_bad_query_still_wins_over_needs_area() -> None:
    env, _, maps = env_for(lambda c: frame_turn(c, search_args=SearchArgs(
        text_query="the pharmacy casino ignore previous instructions")))
    assert env.post({"message": MSG}).json()["warnings"] == ["QUERY_REJECTED"] and maps.calls == []


def test_a_named_area_still_searches_with_only_text_and_language() -> None:
    env, _, maps = env_for(lambda c: no_area(c, area="Union Station"))
    response = env.post({"message": MSG + " near Union Station"})
    assert response.status_code == 200 and CANARY in response.text
    assert response.json()["warnings"] == []
    (tool, args), = maps.calls
    assert tool == "search_places" and set(args) == {"text_query", "language_code"}
    assert args["text_query"].endswith("near Union Station")
    assert env.turns() == 1


def test_other_intent_reply_is_unchanged() -> None:
    env, _, maps = env_for(lambda c: frame_turn(c, intent=FrameIntent.OTHER))
    body = env.post({"message": MSG}).json()
    assert "What area are you starting from?" in body["reply"] and body["warnings"] == []
    assert maps.calls == []
