"""Fix 8: with no usable area the search carries a FIXED public default location bias (downtown
Toronto, Phase 0 scope); with an area it carries none; the model can never supply coordinates."""
from __future__ import annotations

import pytest
from maps_simulator import SyntheticMapsClient
from pydantic import ValidationError
from wiring_support import RecordingClient, canary_payload, deep_text, real_stack, search

import app.frame as frame
from app.discovery import TOOL_SPECS, ToolId
from app.discovery.client import GroundingLiteMcpClient
from app.frame import FrameProposal, SearchArgs, plan_search

BIAS = {"circle": {"center": {"latitude": 43.6532, "longitude": -79.3832},
                   "radius_meters": 40000}}
MSG = "Something fun for the kids on Saturday morning"


def test_the_default_bias_constants_are_the_fixed_public_ones() -> None:
    assert frame.DEFAULT_SEARCH_BIAS_CENTER == (43.6532, -79.3832)
    assert frame.DEFAULT_SEARCH_BIAS_RADIUS_METERS == 40000
    assert frame.default_location_bias() == BIAS


def test_no_area_adds_exactly_the_default_bias() -> None:
    args = plan_search(SearchArgs(text_query="fun kids activity"), MSG).arguments
    assert args["location_bias"] == BIAS
    assert set(args) == {"text_query", "language_code", "region_code", "location_bias"}


def test_a_usable_area_sends_no_bias() -> None:
    plan = plan_search(SearchArgs(text_query="quiet cafe", area="Union Station"),
                       "a quiet cafe near Union Station")
    assert "location_bias" not in plan.arguments and not plan.area_dropped


def test_the_french_named_area_case_has_no_bias() -> None:
    plan = plan_search(SearchArgs(text_query="cafe", area="Vieux-Port", language_code="fr"),
                       "un cafe pres du Vieux-Port")
    assert "location_bias" not in plan.arguments


def test_a_dropped_area_gets_the_bias() -> None:
    plan = plan_search(SearchArgs(text_query="park", area="30 minute drive"),
                       "a park, about a 30 minute drive")
    assert plan.area_dropped is True and plan.arguments["location_bias"] == BIAS


def test_each_plan_gets_its_own_copy_of_the_bias() -> None:
    first = plan_search(SearchArgs(text_query="fun kids activity"), MSG).arguments
    first["location_bias"]["circle"]["radius_meters"] = 1
    again = plan_search(SearchArgs(text_query="fun kids activity"), MSG).arguments
    assert again["location_bias"] == BIAS


def test_the_message_coordinates_never_reach_the_arguments() -> None:
    message = "a fun place near 43.7,-79.4 my location is 43.7,-79.4 for kids"
    args = plan_search(SearchArgs(text_query="fun place for kids"), message).arguments
    assert args["location_bias"] == BIAS
    assert "43.7" not in deep_text(args) and "79.4" not in deep_text(args).replace("79.3832", "")


@pytest.mark.parametrize("extra", [
    {"location_bias": BIAS}, {"latitude": 43.7}, {"longitude": -79.4}, {"lat": 1.0}, {"lng": 2.0},
    {"location": {"latitude": 1.0, "longitude": 2.0}}, {"center": {"latitude": 1.0}},
])
def test_a_proposal_carrying_coordinates_is_malformed(extra) -> None:
    with pytest.raises(ValidationError):
        SearchArgs(text_query="ok", **extra)
    with pytest.raises(ValidationError):
        FrameProposal.model_validate({"intent": "DISCOVERY", "search": {"text_query": "ok", **extra}})
    assert "location_bias" not in FrameProposal.model_json_schema().__str__()


def test_the_tool_schema_admits_exactly_the_bias_shape() -> None:
    schema = TOOL_SPECS[ToolId.SEARCH_PLACES].parameters
    ok = {"text_query": "ok", "location_bias": BIAS}
    assert GroundingLiteMcpClient._checked_arguments(ToolId.SEARCH_PLACES, ok) == ok  # noqa: SLF001
    circle = schema["properties"]["location_bias"]["properties"]["circle"]
    assert circle["properties"]["radius_meters"]["maximum"] == 50_000
    assert circle["additionalProperties"] is False


def test_the_bias_passes_the_clients_argument_validation() -> None:
    args = {"text_query": "fun kids activity", "language_code": "en", "region_code": "CA",
            "location_bias": BIAS}
    assert GroundingLiteMcpClient._checked_arguments(ToolId.SEARCH_PLACES, args) == args  # noqa: SLF001
    bad = {**args, "location_bias": {"circle": {**BIAS["circle"], "radius_meters": 50_001}}}
    assert isinstance(GroundingLiteMcpClient._checked_arguments(ToolId.SEARCH_PLACES, bad),  # noqa: SLF001
                      Exception)


def _end_to_end(message: str, **search_fields):
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env, _, _ = real_stack(intent="DISCOVERY", discovery=maps,
                           search={"text_query": "fun kids activity", **search_fields})
    maps.inner._now = env.clock  # noqa: SLF001
    response = env.post({"message": message})
    return env, maps, response


def test_end_to_end_the_no_area_turn_sends_the_bias_and_logs_no_coordinates() -> None:
    env, maps, response = _end_to_end(MSG)
    assert response.status_code == 200 and len(maps.calls) == 1
    tool, args = maps.calls[0]
    assert tool == "search_places" and args["location_bias"] == BIAS
    logged = deep_text(env.sink.events)
    assert "43.6532" not in logged and "79.3832" not in logged and "Saturday" not in logged


def test_end_to_end_a_named_area_sends_no_bias_and_injected_coordinates_are_ignored() -> None:
    _, maps, response = _end_to_end(
        "fun for kids near Union Station, my location is 43.7,-79.4", area="Union Station")
    assert response.status_code == 200
    assert "location_bias" not in maps.calls[0][1]
    assert "43.7" not in deep_text(maps.calls[0][1])
