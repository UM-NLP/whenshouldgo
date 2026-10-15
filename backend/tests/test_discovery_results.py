"""Typed results for weather, routes and the two resolve tools (documented shapes)."""
from __future__ import annotations

import pytest
from maps_simulator import (
    NOW,
    SyntheticMapsClient,
    new_run,
    synthetic_payload,
    synthetic_resolve_names,
    synthetic_resolve_urls,
    synthetic_route,
    synthetic_weather,
)

from app.discovery import (
    ItemFailureCode,
    MalformedToolResult,
    RouteResult,
    ToolResult,
    parse_resolve_names,
    parse_resolve_urls,
    parse_route,
    parse_weather,
)
from app.discovery._mint import IssuedToken

LOC = {"lat_lng": {"latitude": 1.0, "longitude": 2.0}}
WAYPOINTS = {"origin": {"address": "A"}, "destination": {"address": "B"}}


def get(tool, payload, args):
    return SyntheticMapsClient(payload=payload).call_tool(new_run(), tool, args)


def weather(payload=None):
    return get("lookup_weather", synthetic_weather() if payload is None else payload, {"location": LOC})


def route(payload=None, mode=None):
    args = {**WAYPOINTS, **({"travel_mode": mode} if mode else {})}
    return get("compute_routes", synthetic_route() if payload is None else payload, args)


# --- weather ----------------------------------------------------------------------------------------
def test_weather_keeps_attribution_exactly_and_a_few_typed_readings() -> None:
    parsed = parse_weather(weather())
    assert parsed.attribution_title == "Google Maps Weather"
    assert parsed.attribution_url == "https://www.google.com/maps"
    assert (parsed.condition_type, parsed.temperature_unit, parsed.temperature_degrees) == ("CLEAR", "CELSIUS", 17.5)
    assert parsed.precipitation_percent == 10
    assert "Clear" not in repr(parsed)


@pytest.mark.parametrize("change", [
    {"attribution": None}, {"attribution": {"title": "x"}}, {"attribution": {"title": "x", "url": "https://evil.example/"}},
    {"attribution": {"title": "Caf‮", "url": "https://www.google.com/maps"}},
    {"unknownKey": 1}, {"temperature": {"unit": "KELVIN", "degrees": 1}},
    {"temperature": {"unit": "CELSIUS", "degrees": float("nan")}},
    {"precipitation": {"probability": {"type": "RAIN", "percent": 200}}},
])
def test_weather_payloads_off_the_documented_shape_are_rejected(change) -> None:
    payload = synthetic_weather() | change
    payload = {k: v for k, v in payload.items() if v is not None}
    with pytest.raises(MalformedToolResult):
        parse_weather(weather(payload))


# --- routes ---------------------------------------------------------------------------------------------
def test_a_driving_route_has_no_walking_warning_and_a_walking_one_requires_it() -> None:
    drive = parse_route(route())
    assert (drive.distance_meters, drive.duration_seconds, drive.walking_warning) == (1200, 900.5, False)
    assert drive.travel_mode == "DRIVE" and drive.attribution_title == "Google Maps"
    walk = parse_route(route(mode="WALK"))
    assert walk.travel_mode == "WALK" and walk.walking_warning is True  # beta: the UI must warn
    with pytest.raises(ValueError):
        RouteResult(**{**walk.model_dump(), "walking_warning": False})


@pytest.mark.parametrize("payload", [
    {}, {"routes": []}, {"routes": [{}]}, {"routes": "x"},
    {"routes": [{"distanceMeters": -1, "duration": "1s", "attribution": {"title": "t", "url": "https://www.google.com/maps"}}]},
    {"routes": [{"distanceMeters": 1, "duration": "abc", "attribution": {"title": "t", "url": "https://www.google.com/maps"}}]},
    {"routes": [{"distanceMeters": 1, "duration": "1s"}]},
    {"routes": [{"distanceMeters": 1, "duration": "1s", "attribution": {"title": "t", "url": "https://www.google.com/maps"}, "polyline": "x"}]},
    {"routes": [{"distanceMeters": True, "duration": "1s", "attribution": {"title": "t", "url": "https://www.google.com/maps"}}] },
    {"routes": synthetic_route()["routes"] * 2},
])
def test_route_payloads_off_the_documented_shape_are_rejected(payload) -> None:
    with pytest.raises(MalformedToolResult):
        parse_route(route(payload))


# --- resolve tools -------------------------------------------------------------------------------------
def test_resolve_names_returns_items_failures_and_the_save_link_verbatim() -> None:
    result = get("resolve_names", synthetic_resolve_names(), {"queries": [{"text": "a"}, {"text": "b"}]})
    parsed = parse_resolve_names(result)
    assert [i.place_id for i in parsed.items] == ["SYNTH_PLACE_0001", None]
    assert parsed.items[0].confidence == "HIGH"
    assert [(f.index, f.code, f.retryable) for f in parsed.failures] == [(1, ItemFailureCode.NOT_FOUND, False)]
    assert parsed.save_to_maps_url == "https://maps.app.goo.gl/SYNTHSAVE1"  # opaque, never constructed
    assert parsed.all_failed is False and "Place not found" not in repr(parsed)


def test_resolve_maps_urls_accepts_the_camel_case_spelling_and_internal_failures_are_retryable() -> None:
    result = get("resolve_maps_urls", synthetic_resolve_urls(), {"urls": ["https://maps.app.goo.gl/SYNTH0002"]})
    parsed = parse_resolve_urls(result)
    assert [i.place_id for i in parsed.items] == ["SYNTH_PLACE_0002"]
    assert parsed.save_to_maps_url == "https://maps.app.goo.gl/SYNTHSAVE2"
    failing = {"entities": [{}, {}], "failedRequests": {"0": {"code": 13, "message": "x"}, "1": {"code": 3, "message": "y"}}}
    both = parse_resolve_urls(get("resolve_maps_urls", failing, {"urls": ["https://maps.app.goo.gl/SYNTH0002"] * 2}))
    assert both.all_failed and both.save_to_maps_url is None
    assert {f.index: (f.code, f.retryable) for f in both.failures} == {
        0: (ItemFailureCode.INTERNAL, True), 1: (ItemFailureCode.INVALID_ARGUMENT, False)}


@pytest.mark.parametrize("payload", [
    {}, {"results": "x"}, {"results": [{"entity": {"place": "places/ok"}, "confidence": "SURE"}]},
    {"results": [{"entity": {"place": "not-a-resource"}}]}, {"results": [{"entity": {"place": "places/has space"}}]},
    {"results": [{"entity": {"place": "places/a"}}] * 21},
    {"results": [], "failed_requests": {"x": {"code": 5}}}, {"results": [], "failed_requests": {"0": "bad"}},
    {"results": [], "failed_requests": {"0": {"code": 5, "message": "m", "details": [{"@type": "t"}]}}, "extra": 1},
    {"results": [], "save_to_maps_url": "https://evil.example/save"},
    {"results": [], "save_to_maps_url": "https://www.google.com/url?q=x"},
    {"results": [], "save_to_maps_url": 5},
    {"results": [{"entity": {"place": "places/a", "extra": 1}}]},
])
def test_resolve_payloads_off_the_documented_shape_are_rejected(payload) -> None:
    result = get("resolve_names", payload, {"queries": [{"text": "a"}]})
    with pytest.raises(MalformedToolResult):
        parse_resolve_names(result)


# --- provenance ---------------------------------------------------------------------------------------------
def test_each_parser_accepts_only_its_own_tool_and_only_client_issued_results() -> None:
    search = get("search_places", synthetic_payload(), {"text_query": "q"})
    for parser in (parse_weather, parse_route, parse_resolve_names, parse_resolve_urls):
        with pytest.raises(MalformedToolResult):
            parser(search)
    with pytest.raises(MalformedToolResult):
        parse_weather(route())
    unissued = ToolResult.model_construct(**{**dict(weather()), "issued": None})
    with pytest.raises(MalformedToolResult):
        parse_weather(unissued)
    forged_token = ToolResult.model_construct(**{**dict(weather()), "issued": IssuedToken.__new__(IssuedToken)})
    with pytest.raises(MalformedToolResult):
        parse_weather(forged_token)
    assert NOW  # fixtures use a fixed clock
