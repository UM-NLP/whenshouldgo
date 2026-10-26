"""Shapes verified against a live capture on 2026-10-15 (structure only: every value here is
invented). Covers the contract, weather, routes, resolve tools, transport details and the render
options that the capture settled."""
from __future__ import annotations

import gzip
import json
import re
import zlib

import httpx
import pytest
from maps_simulator import (
    NOW,
    PRINCIPAL,
    SYNTH_TEXT,
    FakeMcpTransport,
    SyntheticMapsClient,
    directions_link,
    mcp_ok,
    new_run,
    photos_link,
    place_link,
    reviews_link,
    route_link,
    save_link,
    synth_id,
    synth_title,
    synthetic_payload,
    synthetic_place,
    synthetic_resolve_names,
    synthetic_route,
    synthetic_weather,
    weather_link,
    write_review_link,
)
from test_discovery_contracts import TTLS, make_answer
from test_discovery_results import get, route, weather
from test_discovery_validator import run_and_answer, validator
from test_gateway_support import SECRET, Clock, make_config

from app.discovery import (
    WALKING_WARNING_TEXT,
    DiscoveryAccessDenied,
    DiscoveryProtocolError,
    DiscoveryToolError,
    GroundingLiteMcpClient,
    GroundedParseError,
    HttpxMcpTransport,
    InvalidToolArguments,
    MalformedToolResult,
    McpResponse,
    RenderOption,
    parse_resolve_names,
    parse_route,
    parse_weather,
    render_grounded_block,
)
from app.gateway.errors import TransportFailure
from app.gateway.openrouter import Credential

CFG = make_config()
ARGS = {"text_query": "quiet cafe"}


# --- the contract on real shapes -------------------------------------------------------------------------
def test_a_real_shaped_search_payload_parses_with_five_places_and_zero_based_citations() -> None:
    answer = make_answer()
    assert len(answer.places) == 5 and [p.cited for p in answer.places] == [True] * 5
    assert answer.text == SYNTH_TEXT and answer.text.count("\n") == 5
    assert "★" in answer.text and "合成" in answer.text and "é" in answer.text  # star, CJK, accent
    assert all(len(p.place_id) == 27 and re.fullmatch(r"[A-Za-z0-9_-]{27}", p.place_id) for p in answer.places)
    assert answer.places[0].attribution_title == synth_title(1) and answer.places[0].attribution_title.endswith(" - Google Maps")
    assert answer.places[0].place_url == place_link(1) == answer.places[0].attribution_url
    run, built = run_and_answer()
    validator.validate(built, run, NOW, principal_id=PRINCIPAL)  # and it validates


def test_markdown_is_kept_literally_and_the_payload_keys_are_exactly_the_documented_ones() -> None:
    answer = make_answer()
    assert "**" in answer.text and "\n- " in answer.text  # verbatim: asterisks and bullets stay as text
    base = synthetic_payload()
    for payload in (base | {"nextPageToken": "x"}, {k: v for k, v in base.items() if k != "places"},
                    base | {"places": [synthetic_place(1) | {"rating": 4.5}]}):
        with pytest.raises(GroundedParseError):
            make_answer(payload)


@pytest.mark.parametrize(("key", "builder"), [
    ("placeUrl", directions_link), ("directionsUrl", place_link), ("photosUrl", reviews_link),
    ("reviewsUrl", photos_link), ("writeAReviewUrl", place_link),
])
def test_each_link_must_match_the_shape_of_its_own_field(key: str, builder) -> None:
    place = synthetic_place(1)
    place["googleMapsLinks"] = {**place["googleMapsLinks"], key: builder(1)}
    with pytest.raises(GroundedParseError):
        make_answer({"summary": "t", "places": [place]})


def test_a_places_links_must_all_carry_the_same_data_token() -> None:
    for key in ("placeUrl", "directionsUrl", "photosUrl", "reviewsUrl", "writeAReviewUrl"):
        place = synthetic_place(1)
        other = {"placeUrl": place_link, "directionsUrl": directions_link, "photosUrl": photos_link,
                 "reviewsUrl": reviews_link, "writeAReviewUrl": write_review_link}[key](2)
        place["googleMapsLinks"] = {**place["googleMapsLinks"], key: other}
        with pytest.raises(GroundedParseError):
            make_answer({"summary": "t", "places": [place]})
    swapped = synthetic_place(1)
    swapped["attribution"] = {**swapped["attribution"], "url": place_link(2)}  # another place's link
    with pytest.raises(GroundedParseError):
        make_answer({"summary": "t", "places": [swapped]})


def test_an_attribution_url_must_have_the_place_link_shape() -> None:
    for url in (route_link(), weather_link(), directions_link(1), save_link(synth_id(1)), "https://maps.app.goo.gl/SYNTH0001"):
        place = synthetic_place(1)
        place["attribution"] = {**place["attribution"], "url": url}
        with pytest.raises(GroundedParseError):
            make_answer({"summary": "t", "places": [place]})


def test_the_data_token_is_not_required_to_equal_the_place_id() -> None:
    # The live capture shows the id inside data= differs from places[].id, so no such check exists.
    answer = make_answer(synthetic_payload(places=1))
    assert answer.places[0].place_id not in answer.places[0].place_url


def test_the_markdown_emphasis_render_option_is_a_named_unimplemented_placeholder() -> None:
    # TODO(legal): whether a client may render Markdown emphasis is a question for Google (12).
    from maps_simulator import wrap_unvalidated

    run, answer = run_and_answer()
    validated = wrap_unvalidated(answer)
    assert RenderOption.MARKDOWN_EMPHASIS.value == "markdown_emphasis"
    block = render_grounded_block(validated)  # off by default
    assert block.render_mode == "text" and "**" in block.parts[0].text
    assert render_grounded_block(validated, options=frozenset()) == block
    with pytest.raises(NotImplementedError):
        render_grounded_block(validated, options=frozenset({RenderOption.MARKDOWN_EMPHASIS}))


# --- weather -----------------------------------------------------------------------------------------------
def test_weather_keeps_the_fields_we_need_with_units_as_enums_and_drops_the_rest() -> None:
    run, result = weather()
    w = parse_weather(result, run, PRINCIPAL)
    assert w.attribution_title == "Weather for Invented Place, Toronto" and w.attribution_url == weather_link()
    assert (w.temperature_unit, w.temperature_degrees) == ("CELSIUS", 17.5)
    assert (w.feels_like_unit, w.feels_like_degrees) == ("CELSIUS", 16)
    assert (w.condition_type, w.condition_text) == ("CLOUDY", "Cloudy")
    assert (w.precipitation_percent, w.precipitation_type) == (10, "RAIN")
    assert (w.wind_speed_unit, w.wind_speed_value, w.wind_cardinal, w.wind_degrees) == ("KILOMETERS_PER_HOUR", 12, "EAST", 90)
    assert (w.relative_humidity, w.uv_index) == (55, 3)
    dumped = w.model_dump_json() + repr(w)
    for dropped in ("43.65", "-79.38", "latLng", "returnedLocation", "iconBaseUri", "gstatic", "1013", "heatIndex",
                    "gust", "thunderstorm", "airPressure", "cloudCover"):
        assert dropped not in dumped, dropped


@pytest.mark.parametrize("path", [
    ("temperature", "unit", "KELVIN"), ("temperature", "degrees", 1_000), ("temperature", "degrees", -1_000),
    ("feelsLikeTemperature", "unit", "RANKINE"), ("relativeHumidity", None, 101), ("relativeHumidity", None, -1),
    ("uvIndex", None, 99), ("uvIndex", None, -1), ("wind", "speed", {"unit": "KNOTS", "value": 5}),
    ("wind", "speed", {"unit": "KILOMETERS_PER_HOUR", "value": 9_999}), ("wind", "direction", {"cardinal": "EAST", "degrees": 361}),
    ("precipitation", "probability", {"percent": 101, "type": "RAIN"}),
    ("weatherCondition", "description", {"languageCode": "en", "text": "Bad‮text"}),
    ("weatherCondition", "iconBaseUri", "https://evil.example/weather/v1/x"),
    ("weatherCondition", "iconBaseUri", "https://maps.gstatic.com/weather/v1/x/../y"),
    ("attribution", "url", "https://www.google.com/search?q=other"),
    ("attribution", "url", route_link()),
])
def test_weather_values_are_bounded_units_are_enums_and_the_icon_url_is_still_checked(path) -> None:
    key, sub, value = path
    payload = synthetic_weather()
    if sub is None:
        payload[key] = value
    elif isinstance(payload[key], dict) and sub in payload[key]:
        payload[key] = {**payload[key], sub: value}
    run, result = weather(payload)
    with pytest.raises(MalformedToolResult):
        parse_weather(result, run, PRINCIPAL)


# --- routes --------------------------------------------------------------------------------------------------
def test_a_route_parses_the_real_shape_and_a_walk_route_carries_the_warning_text() -> None:
    assert WALKING_WARNING_TEXT == "Walking routes are in beta and might sometimes be missing clear sidewalks or pedestrian paths."
    run, result = route()
    drive = parse_route(result, run, PRINCIPAL)
    assert (drive.distance_meters, drive.duration_seconds, drive.walking_warning) == (1200, 900.0, False)
    assert drive.walking_warning_text is None and drive.attribution_url == route_link()
    run, result = route(mode="WALK")
    walk = parse_route(result, run, PRINCIPAL)
    assert walk.walking_warning is True and walk.walking_warning_text == WALKING_WARNING_TEXT


@pytest.mark.parametrize("duration", ["1234s", "0s", "3.5s", "123.456789s"])
def test_route_durations_accept_google_duration_strings(duration: str) -> None:
    run, result = route(synthetic_route(duration=duration))
    assert parse_route(result, run, PRINCIPAL).duration_seconds == float(duration[:-1])


@pytest.mark.parametrize("duration", ["1234", "s", "-5s", "1.s", ".5s", "1e3s", "12 s", "1234S", 1234, None, "1234s\n"])
def test_other_duration_shapes_are_rejected(duration) -> None:
    run, result = route(synthetic_route(duration=duration))
    with pytest.raises(MalformedToolResult):
        parse_route(result, run, PRINCIPAL)


def test_a_route_attribution_url_must_have_the_route_shape() -> None:
    payload = synthetic_route()
    payload["routes"][0]["attribution"]["url"] = place_link(1)
    run, result = route(payload)
    with pytest.raises(MalformedToolResult):
        parse_route(result, run, PRINCIPAL)


# --- resolve tools ----------------------------------------------------------------------------------------------
def test_resolve_names_pins_the_save_link_and_accepts_the_camel_case_keys() -> None:
    run, result = get("resolve_names", synthetic_resolve_names(), {"queries": [{"text": "a"}, {"text": "b"}]})
    parsed = parse_resolve_names(result, run, PRINCIPAL)
    assert parsed.save_to_maps_url == save_link(synth_id(1))
    assert [(f.index, f.code.name) for f in parsed.failures] == [(1, "NOT_FOUND")]


@pytest.mark.parametrize("url", [
    save_link(synth_id(1)).replace("shortlist", "search"), save_link(synth_id(1)) + "&x=1",
    "https://maps.app.goo.gl/SYNTHSAVE1", place_link(1), save_link("bad id"),
])
def test_a_save_link_off_the_pinned_shape_rejects_the_result(url: str) -> None:
    run, result = get("resolve_names", synthetic_resolve_names() | {"saveToMapsUrl": url}, {"queries": [{"text": "a"}]})
    with pytest.raises(MalformedToolResult):
        parse_resolve_names(result, run, PRINCIPAL)


# --- transport and client details ----------------------------------------------------------------------------------
def client_for(script):
    transport = FakeMcpTransport(script)
    return transport, GroundingLiteMcpClient(transport, lambda: SECRET, config=CFG, clock=Clock(), now=lambda: NOW)


def rpc(result=None, error=None, id=1):
    body = {"id": "__echo_request_id__" if id == 1 else id, "jsonrpc": "2.0"}
    body.update({"result": result} if result is not None else {"error": error})
    return McpResponse(status_code=200, body=body)


def test_structured_content_is_preferred_and_the_text_json_is_the_fallback() -> None:
    payload = synthetic_payload()
    both = {"content": [{"type": "text", "text": json.dumps(payload)}], "structuredContent": payload}
    text_only = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    for result in (both, text_only):
        _, client = client_for([rpc(result)])
        assert client.call_tool(new_run(), "search_places", ARGS).payload == payload


def test_structured_content_and_the_text_copy_must_agree() -> None:
    payload = synthetic_payload()
    other = synthetic_payload(places=2)
    result = {"content": [{"type": "text", "text": json.dumps(other)}], "structuredContent": payload}
    _, client = client_for([rpc(result)])
    with pytest.raises(MalformedToolResult):
        client.call_tool(new_run(), "search_places", ARGS)
    junk = {"content": [{"type": "text", "text": "not json"}], "structuredContent": payload}
    _, client = client_for([rpc(junk)])
    client.call_tool(new_run(), "search_places", ARGS)  # non-JSON text next to structured data is ignored


def test_an_http_403_with_a_json_rpc_shaped_body_is_access_denied_and_never_parsed_as_a_result() -> None:
    payload = synthetic_payload()
    body = {"id": 1, "jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": "key restricted"}],
                                                   "isError": True, "structuredContent": payload}}
    _, client = client_for([McpResponse(status_code=403, body=body)])
    with pytest.raises(DiscoveryAccessDenied):
        client.call_tool(new_run(), "search_places", ARGS)
    ok_looking = {"id": 1, "jsonrpc": "2.0", "result": {"content": [], "structuredContent": payload}}
    for status in (400, 401, 402, 403, 404, 429, 500, 503):
        _, client = client_for([McpResponse(status_code=status, body=ok_looking)])
        with pytest.raises(Exception) as caught:  # noqa: B017 - any typed discovery error
            client.call_tool(new_run(), "search_places", ARGS)
        assert caught.type.__name__.startswith("Discovery"), (status, caught.type)


def test_a_200_with_iserror_and_plain_text_is_a_tool_error_and_an_unknown_tool_is_a_protocol_error() -> None:
    plain = {"content": [{"type": "text", "text": "query must not be empty"}], "isError": True}
    _, client = client_for([rpc(plain)])
    with pytest.raises(DiscoveryToolError):
        client.call_tool(new_run(), "search_places", ARGS)
    _, client = client_for([rpc(error={"code": -32602, "message": "unknown tool"})])
    with pytest.raises(DiscoveryProtocolError):
        client.call_tool(new_run(), "search_places", ARGS)


def test_search_places_accepts_the_optional_page_size_and_extended_details() -> None:
    # TODO(verify-live): untested on the wire. tools/list declares pageSize and
    # includeExtendedDetails (camelCase); we send snake_case like the other arguments.
    transport, client = client_for([mcp_ok(synthetic_payload()), mcp_ok(synthetic_payload())])
    client.call_tool(new_run(), "search_places", {**ARGS, "page_size": 5, "include_extended_details": False})
    assert transport.requests[0].body["params"]["arguments"] == {**ARGS, "page_size": 5, "include_extended_details": False}
    client.call_tool(new_run(), "search_places", ARGS)  # unset by default
    assert "page_size" not in transport.requests[1].body["params"]["arguments"]
    for bad in ({"page_size": 0}, {"page_size": 21}, {"page_size": True}, {"page_size": 2.5}, {"include_extended_details": "yes"}):
        with pytest.raises(InvalidToolArguments):
            client.call_tool(new_run(), "search_places", {**ARGS, **bad})


def good_request():
    transport, client = client_for([mcp_ok(synthetic_payload())])
    client.call_tool(new_run(), "search_places", ARGS)
    return transport.requests[0]


RPC = {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": synthetic_payload()}}


def test_a_gzip_reply_is_decoded_with_a_hard_cap_so_it_cannot_expand_past_it() -> None:
    # The live server sent content-encoding: gzip. We ask for identity, but if it gzips anyway we
    # decode incrementally with a decoded-bytes cap instead of failing every call.
    raw = json.dumps(RPC).encode()
    ok = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r: httpx.Response(
        200, content=gzip.compress(raw), headers={"content-encoding": "gzip", "content-type": "application/json; charset=UTF-8"})))
    assert ok.send(good_request(), Credential(SECRET)).body == RPC
    bomb = gzip.compress(b"x" * 5_000_000)
    assert len(bomb) < 20_000
    refused = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r: httpx.Response(
        200, content=bomb, headers={"content-encoding": "gzip"})))
    with pytest.raises(TransportFailure):
        refused.send(good_request(), Credential(SECRET))
    for encoding, content in (("deflate", zlib.compress(raw)), ("br", b"x"), ("gzip, gzip", gzip.compress(raw)),
                              ("compress", b"x")):
        other = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r, e=encoding, c=content: httpx.Response(
            200, content=c, headers={"content-encoding": e})))
        with pytest.raises(TransportFailure):
            other.send(good_request(), Credential(SECRET))
    broken = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r: httpx.Response(
        200, content=b"not gzip at all", headers={"content-encoding": "gzip"})))
    with pytest.raises(TransportFailure):
        broken.send(good_request(), Credential(SECRET))


def test_a_utf8_charset_json_content_type_is_accepted() -> None:
    http = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r: httpx.Response(
        200, content=json.dumps(RPC).encode(), headers={"content-type": "application/json; charset=UTF-8"})))
    assert http.send(good_request(), Credential(SECRET)).body == RPC
