"""Using the user's own location for a no-area search, with consent; and its containment."""
from __future__ import annotations

import json
import logging

import pytest
from location_support import (
    CANARIES,
    HEADERS_A,
    LOCATION,
    LOCATION_BIAS,
    NO_PLACE_MESSAGE,
    env_with,
    grant,
    no_area_turn,
    turn,
)
from pydantic import ValidationError
from wiring_support import CANARY, RaisingClient, deep_text, frame_turn

import app.location as location_module
from app import main
from app.discovery import DiscoveryUnavailable
from app.frame import FrameIntent, FrameProposal, SearchArgs
from app.location import (
    LOCATION_BIAS_RADIUS_METERS,
    LOCATION_DECIMALS,
    LocationInput,
    RoundedLocation,
)
from app.pipeline import REPLY_NEEDS_AREA


def test_the_constants_are_the_documented_placeholders() -> None:
    assert LOCATION_DECIMALS == 3 and LOCATION_BIAS_RADIUS_METERS == 5000


# --- consent gate ------------------------------------------------------------------------------
def test_location_without_consent_is_403_before_any_work_and_costs_nothing() -> None:
    env, gateway, maps = env_with(no_area_turn)
    response = turn(env)
    assert response.status_code == 403
    assert response.json()["code"] == "POLICY"
    assert response.json()["reason"] == "LOCATION_CONSENT_REQUIRED"
    assert gateway.requests == [] and maps.calls == []
    assert env.turns() == 0 and env.reserved() == set() and env.costs.entries() == ()
    assert not env.orchestrator.workspaces


def test_another_principals_consent_does_not_count() -> None:
    env, gateway, _ = env_with(no_area_turn)
    grant(env, {"Authorization": "Bearer tok-b"})
    assert turn(env, headers=HEADERS_A).status_code == 403 and gateway.requests == []


def test_a_turn_without_location_never_needs_consent() -> None:
    env, _, maps = env_with(no_area_turn)
    response = turn(env, location=None)
    assert response.status_code == 200 and response.json()["warnings"] == ["NEEDS_AREA"]
    assert maps.calls == []


# --- use ----------------------------------------------------------------------------------------
def test_consented_location_with_no_area_searches_with_the_exact_bias_and_unchanged_text() -> None:
    env, _, maps = env_with(no_area_turn)
    grant(env)
    response = turn(env)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["warnings"] == [] and CANARY in body["discovery"]["text"]
    (tool, args), = maps.calls
    assert tool == "search_places"
    assert args == {"text_query": "fun kids activity", "language_code": "en",
                    "location_bias": LOCATION_BIAS}
    assert env.turns() == 1  # a real result consumes the allowance like any search


def test_a_named_area_wins_and_no_bias_is_sent() -> None:
    env, _, maps = env_with(lambda c: no_area_turn(c, area="Union Station"))
    grant(env)
    assert turn(env, "fun for the kids near Union Station").status_code == 200
    (_, args), = maps.calls
    assert "location_bias" not in args and args["text_query"].endswith("near Union Station")


def test_a_dropped_area_falls_back_to_the_location() -> None:
    env, _, maps = env_with(lambda c: no_area_turn(c, area="30 minute drive"))
    grant(env)
    assert turn(env, NO_PLACE_MESSAGE + ", a 30 minute drive").status_code == 200
    assert maps.calls[0][1]["location_bias"] == LOCATION_BIAS


def test_location_is_ignored_for_other_intent_and_for_a_rejected_query() -> None:
    env, _, maps = env_with(lambda c: frame_turn(c, intent=FrameIntent.OTHER))
    grant(env)
    assert turn(env).json()["warnings"] == []
    env2, _, maps2 = env_with(lambda c: no_area_turn(c, "the pharmacy casino ignore previous"))
    grant(env2)
    assert turn(env2).json()["warnings"] == ["QUERY_REJECTED"]
    assert maps.calls == [] and maps2.calls == []


def test_without_area_or_location_the_clarification_mentions_sharing_the_location() -> None:
    assert "share your location" in REPLY_NEEDS_AREA["en"]
    assert "partagez votre position" in REPLY_NEEDS_AREA["fr"]
    env, _, maps = env_with(no_area_turn)
    body = turn(env, location=None).json()
    assert body["reply"] == REPLY_NEEDS_AREA["en"] and body["warnings"] == ["NEEDS_AREA"]
    assert maps.calls == []


# --- validation and rounding ---------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"latitude": 91.0, "longitude": 0.0}, {"latitude": -90.1, "longitude": 0.0},
    {"latitude": 0.0, "longitude": 180.5}, {"latitude": 0.0, "longitude": -181},
    {"latitude": 1.0}, {"longitude": 1.0}, {"latitude": "1.0", "longitude": "x"},
    {"latitude": 1.0, "longitude": 1.0, "accuracy": 5}, {"latitude": 1.0, "longitude": 1.0, "altitude": 3},
    {"latitude": 1.0, "longitude": 1.0, "timestamp": 1}, {"latitude": None, "longitude": 1.0},
    {"latitude": float("nan"), "longitude": 1.0}, {"latitude": 1.0, "longitude": float("inf")},
    "12.3,45.6", [12.3, 45.6],
])
def test_invalid_locations_are_422_before_consent_or_work(bad) -> None:
    env, gateway, _ = env_with(no_area_turn)
    grant(env)
    payload = json.dumps({"message": NO_PLACE_MESSAGE, "location": bad})  # NaN stays a token
    response = env.client.post(
        "/v1/agent/turns", content=payload,
        headers={**HEADERS_A, "Idempotency-Key": "k", "Content-Type": "application/json"})
    assert response.status_code == 422 and gateway.requests == []
    assert "12.3" not in response.text


def test_coordinates_are_rounded_on_receipt_and_the_boundary_values_pass() -> None:
    parsed = LocationInput(latitude=12.3456, longitude=-65.4321)
    assert (parsed.latitude, parsed.longitude) == (12.346, -65.432)
    edge = LocationInput(latitude=-90, longitude=180)
    assert (edge.latitude, edge.longitude) == (-90.0, 180.0)
    assert RoundedLocation(12.346, -65.432).bias() == LOCATION_BIAS


def test_the_request_hides_the_location_in_repr_and_validation_errors() -> None:
    request = main.TurnRequest(message="hi", location={"latitude": 12.3456, "longitude": -65.4321})
    for text in (repr(request), str(request), repr(request.location), str(request.location)):
        assert not any(c in text for c in CANARIES)
    with pytest.raises(ValidationError) as caught:
        main.TurnRequest(message="hi", location={"latitude": 12.3456, "longitude": 999.5})
    assert not any(c in str(caught.value) for c in CANARIES) and "999.5" not in str(caught.value)
    assert not any(c in repr(RoundedLocation(12.346, -65.432)) for c in CANARIES)


def test_the_model_still_cannot_supply_a_location() -> None:
    for extra in ({"location": LOCATION}, {"location_bias": LOCATION_BIAS}, {"latitude": 1.0}):
        with pytest.raises(ValidationError):
            SearchArgs(text_query="ok", **extra)
        with pytest.raises(ValidationError):
            FrameProposal.model_validate({"intent": "DISCOVERY", "search": {"text_query": "ok", **extra}})
    assert "location" not in str(FrameProposal.model_json_schema())
    assert hasattr(location_module, "LocationInput")


# --- replay -------------------------------------------------------------------------------------
def test_a_replay_ignores_a_different_location_and_returns_the_stored_result() -> None:
    env, gateway, maps = env_with(no_area_turn)
    grant(env)
    first = turn(env, key="same")
    other = {"latitude": 1.0, "longitude": 2.0}
    second = turn(env, key="same", location=other)
    third = turn(env, key="same", location=None)
    assert first.status_code == second.status_code == third.status_code == 200
    assert second.json() == first.json() == third.json()
    assert len(gateway.requests) == 1 and len(maps.calls) == 1


# --- containment ---------------------------------------------------------------------------------
def _assert_clean(label: str, obj: object) -> None:
    text = deep_text(obj)
    for canary in CANARIES:
        assert canary not in text, f"{canary} leaked into {label}"


def test_coordinates_exist_only_in_the_one_outbound_arguments_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    env, gateway, maps = env_with(no_area_turn)
    grant(env)
    response = turn(env)
    assert response.status_code == 200
    # the one place they may exist (and a positive control: the scan below can see them)
    assert maps.calls[0][1]["location_bias"] == LOCATION_BIAS
    assert "12.346" in deep_text(maps.calls)
    _assert_clean("response", response.text)
    _assert_clean("response headers", dict(response.headers))
    _assert_clean("telemetry", env.sink.events)
    _assert_clean("cost ledger", env.costs.entries())
    _assert_clean("workspaces", env.orchestrator.workspaces)
    _assert_clean("replay records", env.orchestrator._committed)  # noqa: SLF001
    _assert_clean("display cache", env.cache)
    _assert_clean("quota records", env.quotas)
    _assert_clean("model requests", gateway.requests)
    _assert_clean("tool run ledgers", maps.runs)
    _assert_clean("consent ledger", env.app.state.consents)
    _assert_clean("logs", caplog.text)
    _assert_clean("workspace dump", [w.model_dump_json() for w in env.orchestrator.workspaces.values()])


def test_a_failing_search_leaks_no_coordinates_through_errors_or_traces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    config_turns = (no_area_turn,)
    env, gateway, maps = env_with(*config_turns)
    failing = RaisingClient(DiscoveryUnavailable(calls=1))
    env.orchestrator._pipeline._discovery = failing  # noqa: SLF001
    grant(env)
    response = turn(env)
    assert response.status_code == 503
    assert failing.calls == ["search_places"]
    _assert_clean("error response", response.text)
    _assert_clean("telemetry", env.sink.events)
    _assert_clean("cost ledger", env.costs.entries())
    _assert_clean("logs", caplog.text)
    _assert_clean("model requests", gateway.requests)
