"""Typed results for weather, routes and the two resolve tools (documented shapes), bound to the
run ledger: a payload the client did not receive, a mutated payload, or a replay in another run or
for another tenant is rejected."""
from __future__ import annotations

import json

import pytest
from maps_simulator import (
    PRINCIPAL,
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
    payload_digest,
)
from app.discovery._mint import IssuedToken

LOC = {"lat_lng": {"latitude": 1.0, "longitude": 2.0}}
WAYPOINTS = {"origin": {"address": "A"}, "destination": {"address": "B"}}


def get(tool, payload, args, run=None):
    run = run or new_run()
    return run, SyntheticMapsClient(payload=payload).call_tool(run, tool, args)


def weather(payload=None):
    return get("lookup_weather", synthetic_weather() if payload is None else payload, {"location": LOC})


def route(payload=None, mode=None):
    args = {**WAYPOINTS, **({"travel_mode": mode} if mode else {})}
    return get("compute_routes", synthetic_route() if payload is None else payload, args)


def names(payload=None):
    return get("resolve_names", synthetic_resolve_names() if payload is None else payload,
               {"queries": [{"text": "a"}, {"text": "b"}]})


def urls(payload=None):
    return get("resolve_maps_urls", synthetic_resolve_urls() if payload is None else payload,
               {"urls": ["https://maps.app.goo.gl/SYNTH0002"]})


# --- weather ----------------------------------------------------------------------------------------
def test_weather_keeps_attribution_exactly_and_a_few_typed_readings() -> None:
    run, result = weather()
    parsed = parse_weather(result, run, PRINCIPAL)
    assert parsed.attribution_title == "Google Maps Weather"
    assert parsed.attribution_url == "https://www.google.com/maps"
    assert (parsed.condition_type, parsed.temperature_unit, parsed.temperature_degrees) == ("CLEAR", "CELSIUS", 17.5)
    assert parsed.precipitation_percent == 10 and "Clear" not in repr(parsed)


@pytest.mark.parametrize("change", [
    {"attribution": None}, {"attribution": {"title": "x"}}, {"attribution": {"title": "x", "url": "https://evil.example/"}},
    {"attribution": {"title": "Caf‮", "url": "https://www.google.com/maps"}},
    {"unknownKey": 1}, {"temperature": {"unit": "KELVIN", "degrees": 1}},
    {"precipitation": {"probability": {"type": "RAIN", "percent": 200}}},
])
def test_weather_payloads_off_the_documented_shape_are_rejected(change) -> None:
    payload = {k: v for k, v in (synthetic_weather() | change).items() if v is not None}
    run, result = weather(payload)
    with pytest.raises(MalformedToolResult):
        parse_weather(result, run, PRINCIPAL)


# --- routes ---------------------------------------------------------------------------------------------
def test_a_driving_route_has_no_walking_warning_and_a_walking_one_requires_it() -> None:
    run, result = route()
    drive = parse_route(result, run, PRINCIPAL)
    assert (drive.distance_meters, drive.duration_seconds, drive.walking_warning) == (1200, 900.5, False)
    assert drive.travel_mode == "DRIVE" and drive.attribution_title == "Google Maps"
    run, result = route(mode="WALK")
    walk = parse_route(result, run, PRINCIPAL)
    assert walk.travel_mode == "WALK" and walk.walking_warning is True  # beta: the UI must warn
    with pytest.raises(ValueError):
        RouteResult(**{**walk.model_dump(), "walking_warning": False})


@pytest.mark.parametrize("payload", [
    {}, {"routes": []}, {"routes": [{}]}, {"routes": "x"},
    {"routes": [{"distanceMeters": -1, "duration": "1s", "attribution": {"title": "t", "url": "https://www.google.com/maps"}}]},
    {"routes": [{"distanceMeters": 1, "duration": "abc", "attribution": {"title": "t", "url": "https://www.google.com/maps"}}]},
    {"routes": [{"distanceMeters": 1, "duration": "1s"}]},
    {"routes": [{"distanceMeters": 1, "duration": "1s", "attribution": {"title": "t", "url": "https://www.google.com/maps"}, "polyline": "x"}]},
    {"routes": [{"distanceMeters": True, "duration": "1s", "attribution": {"title": "t", "url": "https://www.google.com/maps"}}]},
    {"routes": synthetic_route()["routes"] * 2},
])
def test_route_payloads_off_the_documented_shape_are_rejected(payload) -> None:
    run, result = route(payload)
    with pytest.raises(MalformedToolResult):
        parse_route(result, run, PRINCIPAL)


# --- resolve tools -------------------------------------------------------------------------------------
def test_resolve_names_returns_items_failures_and_the_save_link_verbatim() -> None:
    run, result = names()
    parsed = parse_resolve_names(result, run, PRINCIPAL)
    assert [i.place_id for i in parsed.items] == ["SYNTH_PLACE_0001", None]
    assert parsed.items[0].confidence == "HIGH"
    assert [(f.index, f.code, f.retryable) for f in parsed.failures] == [(1, ItemFailureCode.NOT_FOUND, False)]
    assert parsed.save_to_maps_url == "https://maps.app.goo.gl/SYNTHSAVE1"
    assert parsed.all_failed is False and "Place not found" not in repr(parsed)


def test_resolve_maps_urls_accepts_camel_case_and_internal_failures_are_retryable() -> None:
    run, result = urls()
    parsed = parse_resolve_urls(result, run, PRINCIPAL)
    assert [i.place_id for i in parsed.items] == ["SYNTH_PLACE_0002"]
    assert parsed.save_to_maps_url == "https://maps.app.goo.gl/SYNTHSAVE2"
    failing = {"entities": [{}, {}], "failedRequests": {"0": {"code": 13, "message": "x"}, "1": {"code": 3, "message": "y"}}}
    run, result = get("resolve_maps_urls", failing, {"urls": ["https://maps.app.goo.gl/SYNTH0002"] * 2})
    both = parse_resolve_urls(result, run, PRINCIPAL)
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
    run, result = get("resolve_names", payload, {"queries": [{"text": "a"}]})
    with pytest.raises(MalformedToolResult):
        parse_resolve_names(result, run, PRINCIPAL)


# --- provenance ---------------------------------------------------------------------------------------------
def test_each_parser_accepts_only_its_own_tool_and_only_client_issued_results() -> None:
    run, search = get("search_places", synthetic_payload(), {"text_query": "q"})
    for parser in (parse_weather, parse_route, parse_resolve_names, parse_resolve_urls):
        with pytest.raises(MalformedToolResult):
            parser(search, run, PRINCIPAL)
    wrun, wres = weather()
    with pytest.raises(MalformedToolResult):
        parse_route(wres, wrun, PRINCIPAL)
    unissued = ToolResult.model_construct(**{**dict(wres), "issued": None})
    with pytest.raises(MalformedToolResult):
        parse_weather(unissued, wrun, PRINCIPAL)
    bare = ToolResult.model_construct(**{**dict(wres), "issued": IssuedToken.__new__(IssuedToken)})
    with pytest.raises(MalformedToolResult):
        parse_weather(bare, wrun, PRINCIPAL)


# --- item 1: the ledger holds a payload hash for EVERY tool, and the parsers check it ----------------------
@pytest.mark.parametrize(("maker", "tool"), [
    (weather, "lookup_weather"), (route, "compute_routes"), (names, "resolve_names"), (urls, "resolve_maps_urls")])
def test_the_ledger_records_a_canonical_payload_hash_for_every_tool(maker, tool) -> None:
    run, result = maker()
    record = run.record_for(result.tool_call_id)
    assert record is not None and record.payload_hash == payload_digest(result.payload)
    assert len(record.payload_hash) == 64


PARSERS = [(weather, parse_weather), (route, parse_route), (names, parse_resolve_names), (urls, parse_resolve_urls)]
MUTATIONS = {
    "lookup_weather": lambda p: p["attribution"].update(title="Totally official"),
    "compute_routes": lambda p: p["routes"][0].update(distanceMeters=5),
    "resolve_names": lambda p: p.update(save_to_maps_url="https://maps.app.goo.gl/EVILSAVE"),
    "resolve_maps_urls": lambda p: p.update(saveToMapsUrl="https://maps.app.goo.gl/EVILSAVE"),
}


@pytest.mark.parametrize(("maker", "parser"), PARSERS)
def test_a_forged_payload_with_a_genuine_token_is_rejected(maker, parser) -> None:
    run, result = maker()
    evil = result.payload
    MUTATIONS[result.tool.value](evil)
    forged = result.model_copy(update={"payload_json": json.dumps(evil)})
    assert forged.issued is result.issued  # the genuine token rides along
    with pytest.raises(MalformedToolResult):
        parser(forged, run, PRINCIPAL)
    parser(result, run, PRINCIPAL)  # the genuine result still parses


def test_model_copy_with_a_new_payload_cannot_change_what_the_result_holds() -> None:
    run, result = weather()
    evil = synthetic_weather()
    evil["temperature"]["degrees"] = 99.0
    copied = result.model_copy(update={"payload": evil})
    assert copied.payload["temperature"]["degrees"] == 17.5
    assert parse_weather(copied, run, PRINCIPAL).temperature_degrees == 17.5


def test_mutating_the_returned_payload_does_not_change_the_stored_result() -> None:
    run, result = weather()
    result.payload["temperature"]["degrees"] = -5.0  # a copy: the stored result is frozen
    result.payload["attribution"]["title"] = "Mutated"
    assert result.payload["temperature"]["degrees"] == 17.5
    assert parse_weather(result, run, PRINCIPAL).attribution_title == "Google Maps Weather"


@pytest.mark.parametrize(("maker", "parser"), PARSERS)
def test_cross_run_and_cross_tenant_replays_are_rejected(maker, parser) -> None:
    run, result = maker()
    parser(result, run, PRINCIPAL)
    with pytest.raises(MalformedToolResult):
        parser(result, new_run(run_id="run-2"), PRINCIPAL)  # another run's ledger
    with pytest.raises(MalformedToolResult):
        parser(result, run, "user-2")  # another tenant
    with pytest.raises(MalformedToolResult):
        parser(result, new_run(principal_id="user-2"), "user-2")  # same run id, other tenant


def test_a_result_whose_call_id_the_ledger_does_not_know_is_rejected() -> None:
    run, result = weather()
    stranger = result.model_copy(update={"tool_call_id": "tc-other"})
    with pytest.raises(MalformedToolResult):
        parse_weather(stranger, run, PRINCIPAL)


def test_a_non_finite_payload_cannot_even_be_stored_in_a_result() -> None:
    with pytest.raises(ValueError):
        weather(synthetic_weather() | {"temperature": {"unit": "CELSIUS", "degrees": float("nan")}})
