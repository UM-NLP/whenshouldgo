"""Grounding Lite MCP adapter on the documented shapes: fail-closed config, tool allowlist and
argument schemas, budgets, kill switch, typed errors, no credentials or content in traces,
and the locked-down HTTP transport (JSON and single-event SSE replies)."""
from __future__ import annotations

import gc
import gzip
import json
import traceback
from pathlib import Path

import httpx
import pytest
from maps_simulator import (
    INJECTION,
    NOW,
    FakeMcpTransport,
    SyntheticMapsClient,
    mcp_ok,
    mcp_status,
    new_run,
    synth_id,
    synthetic_payload,
    synthetic_weather,
)
from test_gateway_support import Clock, make_config

from app.discovery import (
    ACCESS_DENIED_TRIP_THRESHOLD,
    ALLOWED_HEADERS,
    GROUNDING_LITE_ENDPOINT,
    GROUNDING_LITE_HOST,
    TOOL_SPECS,
    DiscoveryAccessDenied,
    DiscoveryBadRequest,
    DiscoveryConfigError,
    DiscoveryDisabledError,
    DiscoveryError,
    DiscoveryProtocolError,
    DiscoveryRateLimited,
    DiscoverySessionRequired,
    DiscoveryTimeout,
    DiscoveryToolError,
    DiscoveryUnavailable,
    GroundingLiteClient,
    GroundingLiteMcpClient,
    HttpxMcpTransport,
    InvalidToolArguments,
    KillSwitch,
    KillSwitchProbe,
    MalformedToolResult,
    McpRequest,
    McpResponse,
    OversizeResult,
    ToolBudgetExceeded,
    ToolId,
    ToolNotAllowed,
    ToolRun,
    parse_grounded_answer,
    parse_single_sse_event,
    validate_endpoint,
)
from app.gateway.errors import TransportFailure, TransportTimeout
from app.gateway.openrouter import Credential
from app.telemetry import InMemorySink, ToolCall, ToolName, ToolOutcome

SECRET = "AIza-synthetic-test-key-0123456789"
CFG = make_config()
ARGS = {"text_query": "quiet vegetarian cafe"}
APP_DIR = Path(__file__).resolve().parents[1] / "app"
LOC = {"lat_lng": {"latitude": 43.65, "longitude": -79.38}}


def make_client(script, *, secret=SECRET, kill_switch=None, sink=None, clock=None, **kw):
    transport = FakeMcpTransport(script)
    client = GroundingLiteMcpClient(
        transport, (lambda: secret) if isinstance(secret, str) else secret, config=CFG,
        kill_switch=kill_switch, sink=sink, clock=clock or Clock(), now=lambda: NOW, **kw)
    return transport, client


def ok():
    return mcp_ok(synthetic_payload())


# --- the documented endpoint and headers --------------------------------------------------------------
def test_the_endpoint_and_headers_match_the_documentation() -> None:
    # Documented (developers.google.com/maps/ai/grounding-lite/reference/mcp, 2026-10-15).
    assert GROUNDING_LITE_HOST == "mapstools.googleapis.com"
    assert GROUNDING_LITE_ENDPOINT == "https://mapstools.googleapis.com/mcp"
    assert ALLOWED_HEADERS == {"Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"}
    validate_endpoint(GROUNDING_LITE_ENDPOINT)


@pytest.mark.parametrize("url", [
    "http://mapstools.googleapis.com/mcp", "https://evil.example/mcp",
    "https://mapstools.googleapis.com.evil.example/mcp", "https://user@mapstools.googleapis.com/mcp",
    "https://mapstools.googleapis.com:8443/mcp", "https://MAPSTOOLS.googleapis.com/mcp",
    "https://mapstools.googleapis.com/other", "https://mapstools.googleapis.com/mcp?x=1",
    "https://mapstools.googleapis.com/mcp#f", "https://mapstools.googleapis.com/mcp\n",
    "https://mapstools.googleapis.com/mcp ", "https://mapstools.googleapis.com/mcé", "", "ftp://x/mcp",
])
def test_bad_endpoints_are_refused_at_validation_and_at_construction(url: str) -> None:
    with pytest.raises(DiscoveryConfigError):
        validate_endpoint(url)
    with pytest.raises(DiscoveryConfigError):
        GroundingLiteMcpClient(FakeMcpTransport([]), lambda: SECRET, config=CFG, endpoint=url)


def test_only_the_five_documented_tools_are_allowed_with_closed_schemas() -> None:
    assert {t.value for t in ToolId} == {
        "search_places", "lookup_weather", "compute_routes", "resolve_names", "resolve_maps_urls"}
    assert set(TOOL_SPECS) == set(ToolId)
    for spec in TOOL_SPECS.values():
        assert spec.parameters["additionalProperties"] is False


def test_the_client_satisfies_the_protocol_and_so_does_the_simulator() -> None:
    _, client = make_client([])
    assert isinstance(client, GroundingLiteClient) and isinstance(SyntheticMapsClient(), GroundingLiteClient)


@pytest.mark.parametrize("tool", ["web_search", "fetch_url", "search_places ", "SEARCH_PLACES", "", "../x",
                                  "openrouter:web_search"])
def test_tools_outside_the_allowlist_are_denied_before_any_request(tool: str) -> None:
    sink = InMemorySink()
    transport, client = make_client([ok()], sink=sink)
    with pytest.raises(ToolNotAllowed):
        client.call_tool(new_run(), tool, ARGS)
    assert transport.requests == []
    [event] = sink.events
    assert event.outcome == ToolOutcome.DENIED and event.billable_units == 0


# --- arguments: the documented schemas (snake_case; TODO(verify-live) casing) -----------------------------
CIRCLE = {"circle": {"center": {"latitude": 43.65, "longitude": -79.38}, "radius_meters": 5000}}
VALID_CALLS = [
    ("search_places", ARGS),
    ("search_places", {**ARGS, "location_bias": CIRCLE, "language_code": "en"}),
    ("search_places", {**ARGS, "location_bias": {"circle": {"center": {"latitude": 1.0, "longitude": 2.0}}}}),
    ("lookup_weather", {"location": LOC}),
    ("lookup_weather", {"location": {"place_id": "SYNTH_PLACE_000000000000001"}, "units_system": "IMPERIAL",
                        "date": {"year": 2026, "month": 10, "day": 16}, "hour": 17}),
    ("lookup_weather", {"location": {"address": "Invented Street, Toronto, Canada"}}),
    ("compute_routes", {"origin": {"address": "A"}, "destination": {"place_id": "SYNTH_PLACE_000000000000002"}}),
    ("compute_routes", {"origin": {"lat_lng": {"latitude": 1.0, "longitude": 2.0}},
                        "destination": {"address": "B"}, "travel_mode": "WALK"}),
    ("resolve_names", {"queries": [{"text": "Invented Tower, Paris"}], "region_code": "FR",
                       "location_bias": {"viewport": {"low": {"latitude": 48.8, "longitude": 2.2},
                                                      "high": {"latitude": 48.9, "longitude": 2.4}}}}),
    ("resolve_maps_urls", {"urls": ["https://maps.app.goo.gl/SYNTH0001",
                                    "https://www.google.com/maps/place/SYNTH_PLACE_000000000000001"]}),
]


@pytest.mark.parametrize(("tool", "arguments"), VALID_CALLS)
def test_documented_argument_shapes_are_accepted(tool, arguments) -> None:
    transport, client = make_client([ok()])
    client.call_tool(new_run(), tool, arguments)
    assert transport.requests[0].body["params"] == {"name": tool, "arguments": arguments}


INVALID_CALLS = [
    ("search_places", {}), ("search_places", {"text_query": ""}), ("search_places", {"text_query": "x" * 301}),
    ("search_places", {"text_query": 5}), ("search_places", {"text_query": "ok", "max_results": 5}),
    ("search_places", {"text_query": "ok", "location": {"latitude": 1.0, "longitude": 2.0}}),
    ("search_places", {"text_query": "ok", "location_bias": {"circle": {"center": {"latitude": 1.0, "longitude": 2.0}, "radius_meters": 50_001}}}),
    ("search_places", {"text_query": "ok", "location_bias": {"circle": {"center": {"latitude": 1.0, "longitude": 2.0}, "radius_meters": 0}}}),
    ("search_places", {"text_query": "ok", "location_bias": {"circle": {"radius_meters": 5}}}),
    ("search_places", {"text_query": "ok", "location_bias": {"circle": {"center": {"latitude": 91, "longitude": 0}}}}),
    ("search_places", {"text_query": "ok", "location_bias": {"circle": {"center": {"latitude": float("nan"), "longitude": 0}}}}),
    ("search_places", {"text_query": "ok", "language_code": "x" * 20}),
    ("search_places", {"text_query": "bad\x00char"}),
    ("lookup_weather", {}), ("lookup_weather", {"location": {}}),
    ("lookup_weather", {"location": {**LOC, "place_id": "x"}}),  # exactly one of
    ("lookup_weather", {"location": {"place_id": "x", "address": "y"}}),
    ("lookup_weather", {"location": LOC, "language_code": "en"}),  # undocumented for this tool
    ("lookup_weather", {"location": LOC, "hour": 24}), ("lookup_weather", {"location": LOC, "hour": -1}),
    ("lookup_weather", {"location": LOC, "date": {"year": 2026, "month": 13, "day": 1}}),
    ("lookup_weather", {"location": LOC, "date": {"year": 2026, "month": 1}}),
    ("lookup_weather", {"location": LOC, "units_system": "KELVIN"}),
    ("compute_routes", {"origin": {"address": "A"}}),
    ("compute_routes", {"origin": {"address": "A", "place_id": "p"}, "destination": {"address": "B"}}),
    ("compute_routes", {"origin": {}, "destination": {"address": "B"}}),
    ("compute_routes", {"origin": {"address": "A"}, "destination": {"address": "B"}, "travel_mode": "TRANSIT"}),
    ("resolve_names", {}), ("resolve_names", {"queries": []}),
    ("resolve_names", {"queries": [{"text": "x"}] * 21}), ("resolve_names", {"queries": [{"text": ""}]}),
    ("resolve_names", {"queries": [{"query": "x"}]}), ("resolve_names", {"queries": ["x"]}),
    ("resolve_maps_urls", {"urls": []}), ("resolve_maps_urls", {"urls": ["https://maps.app.goo.gl/SYNTH0001"] * 21}),
    ("resolve_maps_urls", {"urls": ["https://evil.example/x"]}),
    ("resolve_maps_urls", {"urls": ["http://maps.app.goo.gl/SYNTH0001"]}),
    ("resolve_maps_urls", {"urls": ["https://www.google.com/maps/../url?q=x"]}),
    ("resolve_maps_urls", {"urls": ["https://maps.app.goo.gl/SYNTH0001\n"]}),
    ("resolve_maps_urls", {"urls": ["https://www.google.com/maps"]}),
]


@pytest.mark.parametrize(("tool", "arguments"), INVALID_CALLS)
def test_invalid_arguments_are_rejected_before_sending_and_cost_no_budget(tool, arguments) -> None:
    transport, client = make_client([ok()])
    run = new_run()
    with pytest.raises(InvalidToolArguments):
        client.call_tool(run, tool, arguments)
    assert transport.requests == [] and run.calls_used == 0


def test_argument_size_and_depth_are_capped() -> None:
    _, client = make_client([ok()])
    for arguments in ({"text_query": "ok", "pad": "x" * 5_000},
                      {"text_query": "ok", "location_bias": {"circle": {"center": {
                          "latitude": 1.0, "longitude": 1.0, "n": {"a": {"b": {"c": {"d": {}}}}}}}}}):
        with pytest.raises(InvalidToolArguments):
            client.call_tool(new_run(), "search_places", arguments)


# --- the request on the wire ------------------------------------------------------------------------------
def test_the_request_is_exact_json_rpc_with_allowlisted_headers_and_no_key() -> None:
    transport, client = make_client([ok()])
    client.call_tool(new_run(), "search_places", ARGS)
    [sent] = transport.requests
    assert sent.url == GROUNDING_LITE_ENDPOINT and sent.headers == ALLOWED_HEADERS
    assert set(sent.body) == {"jsonrpc", "id", "method", "params"}
    assert sent.body["method"] == "tools/call" and sent.body["jsonrpc"] == "2.0"
    assert SECRET not in repr(sent) and SECRET not in str(sent.model_dump())
    assert transport.keys == [SECRET]


def test_mcp_requests_cannot_be_built_with_other_headers_methods_or_hosts() -> None:
    good = dict(url=GROUNDING_LITE_ENDPOINT, headers=dict(ALLOWED_HEADERS), timeout_seconds=1.0,
                body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "search_places", "arguments": ARGS}})
    McpRequest(**good)
    for change in ({"headers": {**ALLOWED_HEADERS, "Cookie": "a=b"}}, {"headers": {}},
                   {"headers": {**ALLOWED_HEADERS, "Accept": "application/json"}},
                   {"url": "https://evil.example/mcp"},
                   {"body": {**good["body"], "method": "tools/list"}},
                   {"body": {**good["body"], "method": "initialize"}},
                   {"body": {**good["body"], "params": {"name": "web_search", "arguments": {}}}},
                   {"body": {**good["body"], "extra": 1}},
                   {"body": {**good["body"], "params": {"name": "search_places", "arguments": ARGS, "_meta": {}}}}):
        with pytest.raises((ValueError, DiscoveryError)):
            McpRequest(**{**good, **change})


# --- credentials -------------------------------------------------------------------------------------------
def test_the_key_is_stripped_and_malformed_keys_are_refused_before_sending() -> None:
    transport, client = make_client([ok()], secret="  " + SECRET + "\n")
    client.call_tool(new_run(), "search_places", ARGS)
    assert transport.keys == [SECRET]
    for secret in ("", "  ", "a b", "a\nb", "a\tb", "a\x00b", "é"):
        transport, client = make_client([ok()], secret=secret)
        run = new_run()
        with pytest.raises(DiscoveryConfigError):
            client.call_tool(run, "search_places", ARGS)
        assert transport.requests == [] and run.calls_used == 0


def test_secret_provider_failures_do_not_leak_or_chain() -> None:
    def broken() -> str:
        raise RuntimeError(f"vault said {SECRET}")

    transport, client = make_client([ok()], secret=broken)
    with pytest.raises(DiscoveryConfigError) as caught:
        client.call_tool(new_run(), "search_places", ARGS)
    error = caught.value
    assert error.__context__ is None and error.__cause__ is None
    assert SECRET not in "".join(traceback.format_exception(error)) and transport.requests == []


# --- results ---------------------------------------------------------------------------------------------------
def test_structured_and_text_json_results_are_both_accepted_and_ledgered() -> None:
    for response in (mcp_ok(synthetic_payload()), mcp_ok(synthetic_payload(), structured=False)):
        run = new_run()
        _, client = make_client([response])
        result = client.call_tool(run, "search_places", ARGS)
        assert result.tool == ToolId.SEARCH_PLACES and result.retrieved_at == NOW
        assert result.billable_units == 1 and result.tool_call_id.startswith("tc-") and result.run_id == "run-1"
        record = run.record_for(result.tool_call_id)
        assert record is not None and record.tool == ToolId.SEARCH_PLACES
        assert record.place_ids == frozenset(synth_id(i) for i in range(1, 6))
        assert record.text_hash and record.display_hash and len(record.display_hash) == 64
        answer = parse_grounded_answer(result, CFG.ttls)
        assert answer.display_hash == record.display_hash


def test_non_search_tools_get_a_ledger_entry_without_a_display_hash() -> None:
    run = new_run()
    _, client = make_client([mcp_ok(synthetic_weather())])
    result = client.call_tool(run, "lookup_weather", {"location": LOC})
    record = run.record_for(result.tool_call_id)
    assert record is not None and record.display_hash is None and record.place_ids == frozenset()


@pytest.mark.parametrize(("body", "error"), [
    (None, MalformedToolResult), ([], MalformedToolResult), ({"jsonrpc": "2.0", "id": 1}, MalformedToolResult),
    ({"jsonrpc": "2.0", "id": 1, "result": []}, MalformedToolResult),
    ({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "image", "data": "x"}]}}, MalformedToolResult),
    ({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "not json"}]}}, MalformedToolResult),
    ({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "[1,2]"}]}}, MalformedToolResult),
    ({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": '{"a": NaN}'}]}}, MalformedToolResult),
    ({"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": "str"}}, MalformedToolResult),
    ({"jsonrpc": "1.0", "id": 1, "result": {"content": [], "structuredContent": {}}}, MalformedToolResult),
    ({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "SECRET-PROVIDER-TEXT"}], "isError": True}}, DiscoveryToolError),
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "SECRET-PROVIDER-TEXT"}}, DiscoveryProtocolError),
])
def test_malformed_and_error_results_are_typed_and_never_echo_provider_text(body, error) -> None:
    _, client = make_client([McpResponse(status_code=200, body=body)])
    with pytest.raises(error) as caught:
        client.call_tool(new_run(), "search_places", ARGS)
    assert "SECRET-PROVIDER-TEXT" not in str(caught.value) + repr(caught.value)
    assert caught.value.__context__ is None and isinstance(caught.value, MalformedToolResult)


@pytest.mark.parametrize("payload", [
    synthetic_payload(text="a\ud800b"),
    {"summary": "t", "places": [], "x": "\udfff"},
    {"summary": "t", "places": [{"id": "a", "attribution": {"title": "lone\ud83d", "url": "u"}}]},
])
def test_a_lone_surrogate_anywhere_in_a_payload_is_a_typed_rejection_not_a_unicode_error(payload) -> None:
    sink = InMemorySink()
    run = new_run()
    _, client = make_client([McpResponse(status_code=200, body={
        "jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": payload}})], sink=sink)
    with pytest.raises(MalformedToolResult) as caught:
        client.call_tool(run, "search_places", ARGS)
    assert "ud800" not in str(caught.value).lower() and len(sink.events) == 1  # no telemetry gap
    assert sink.events[0].outcome == ToolOutcome.REJECTED


def test_deeply_nested_and_oversized_results_are_refused() -> None:
    nested: object = {}
    for _ in range(30):
        nested = {"a": nested}
    for response in (mcp_ok({"summary": "t", "deep": nested}),
                     McpResponse(status_code=200, body={"jsonrpc": "2.0", "id": 1, "result": {
                         "content": [], "structuredContent": synthetic_payload()}}, size_bytes=10_000_000)):
        _, client = make_client([response])
        with pytest.raises((MalformedToolResult, OversizeResult)):
            client.call_tool(new_run(), "search_places", ARGS)


def test_injection_text_in_results_is_inert_data_that_never_reaches_events_or_reprs() -> None:
    sink = InMemorySink()
    _, client = make_client([mcp_ok(synthetic_payload(text=INJECTION))], sink=sink)
    result = client.call_tool(new_run(), "search_places", ARGS)
    assert result.payload["summary"] == INJECTION  # carried as data, untouched
    assert "Ignore previous" not in repr(result) and "alert(1)" not in repr(result)
    dumped = "".join(e.model_dump_json() for e in sink.events)
    assert "Ignore" not in dumped and "script" not in dumped and "SYNTH_PLACE" not in dumped


# --- HTTP and protocol errors, kill switch ---------------------------------------------------------------
@pytest.mark.parametrize(("code", "error", "retryable"), [
    (400, DiscoveryBadRequest, False), (404, DiscoverySessionRequired, False),
    (401, DiscoveryAccessDenied, False), (402, DiscoveryAccessDenied, False), (403, DiscoveryAccessDenied, False),
    (429, DiscoveryRateLimited, True), (500, DiscoveryUnavailable, True), (503, DiscoveryUnavailable, True),
    (504, DiscoveryTimeout, True), (408, DiscoveryTimeout, True), (302, MalformedToolResult, False),
])
def test_http_statuses_map_to_typed_errors(code, error, retryable) -> None:
    _, client = make_client([mcp_status(code)])
    with pytest.raises(error) as caught:
        client.call_tool(new_run(), "search_places", ARGS)
    assert caught.value.retryable is retryable and caught.value.calls == 1


def test_a_session_required_failure_fails_closed_and_is_not_retried_or_tripping() -> None:
    # TODO(verify-live): the docs never mention an initialize handshake or session id; the MCP
    # spec requires one. We run the stateless path and treat 400/404 as typed, non-retryable.
    switch = KillSwitch()
    _, client = make_client([mcp_status(404)], kill_switch=switch)
    with pytest.raises(DiscoverySessionRequired):
        client.call_tool(new_run(), "search_places", ARGS)
    assert not switch.tripped


@pytest.mark.parametrize(("raised", "error"), [
    (TransportTimeout(), DiscoveryTimeout), (TransportFailure(), DiscoveryUnavailable),
    (RuntimeError("x"), DiscoveryUnavailable), (httpx.InvalidURL("z"), DiscoveryUnavailable),
    (KeyError("k"), DiscoveryUnavailable),
])
def test_transport_exceptions_become_typed_errors_without_chaining(raised, error) -> None:
    _, client = make_client([raised])
    with pytest.raises(error) as caught:
        client.call_tool(new_run(), "search_places", ARGS)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize("code", [401, 402])
def test_invalid_credentials_or_billing_trip_the_kill_switch_at_once(code: int) -> None:
    switch = KillSwitch()
    transport, client = make_client([mcp_status(code), ok(), ok()], kill_switch=switch)
    with pytest.raises(DiscoveryAccessDenied):
        client.call_tool(new_run(), "search_places", ARGS)
    assert switch.tripped
    for _ in range(3):
        with pytest.raises(DiscoveryDisabledError) as caught:
            client.call_tool(new_run(), "search_places", ARGS)
        assert caught.value.calls == 0
    assert len(transport.requests) == 1
    switch.reset()
    client.call_tool(new_run(), "search_places", ARGS)
    assert len(transport.requests) == 2


def test_one_403_cannot_disable_discovery_for_everyone_but_consecutive_ones_do() -> None:
    assert ACCESS_DENIED_TRIP_THRESHOLD == 3
    switch = KillSwitch()
    script = [mcp_status(403), mcp_status(403), ok(), mcp_status(403), mcp_status(403), mcp_status(403), ok()]
    transport, client = make_client(script, kill_switch=switch)
    for _ in range(2):
        with pytest.raises(DiscoveryAccessDenied):
            client.call_tool(new_run(), "search_places", ARGS)
    assert not switch.tripped  # two in a row is below the threshold
    client.call_tool(new_run(), "search_places", ARGS)  # a success resets the streak
    for _ in range(2):
        with pytest.raises(DiscoveryAccessDenied):
            client.call_tool(new_run(), "search_places", ARGS)
    assert not switch.tripped
    with pytest.raises(DiscoveryAccessDenied):
        client.call_tool(new_run(), "search_places", ARGS)
    assert switch.tripped
    with pytest.raises(DiscoveryDisabledError):
        client.call_tool(new_run(), "search_places", ARGS)
    assert len(transport.requests) == 6


def test_the_probe_protocol_cannot_reset_and_other_failures_do_not_trip() -> None:
    assert not hasattr(KillSwitchProbe, "reset") and hasattr(KillSwitch(), "reset")
    for response in (mcp_status(429), mcp_status(503), mcp_status(400), mcp_status(404)):
        switch = KillSwitch()
        _, client = make_client([response], kill_switch=switch)
        with pytest.raises(DiscoveryError):
            client.call_tool(new_run(), "search_places", ARGS)
        assert not switch.tripped


def test_a_pre_tripped_switch_blocks_even_the_first_call() -> None:
    switch = KillSwitch()
    switch.trip()
    transport, client = make_client([ok()], kill_switch=switch)
    with pytest.raises(DiscoveryDisabledError):
        client.call_tool(new_run(), "search_places", ARGS)
    assert transport.requests == []


# --- the run: budget, ledger authority, binding ------------------------------------------------------------
def test_twelve_calls_per_run_then_the_budget_stops_the_thirteenth() -> None:
    run = new_run()
    transport, client = make_client([ok() for _ in range(13)])
    for _ in range(12):
        client.call_tool(run, "search_places", ARGS)
    with pytest.raises(ToolBudgetExceeded) as caught:
        client.call_tool(run, "search_places", ARGS)
    assert len(transport.requests) == 12 and caught.value.calls == 0
    assert run.calls_used == 12 and ToolRun.from_config(CFG, principal_id="u", run_id="r").max_calls == 12


def test_failed_calls_still_count_toward_the_run_budget() -> None:
    run = new_run(max_calls=2)
    _, client = make_client([mcp_status(503), TransportTimeout(), ok()])
    for _ in range(2):
        with pytest.raises(DiscoveryError):
            client.call_tool(run, "search_places", ARGS)
    with pytest.raises(ToolBudgetExceeded):
        client.call_tool(run, "search_places", ARGS)


def test_the_run_budget_is_thread_safe() -> None:
    import threading

    from app.discovery._mint import AUTHORITY

    run = new_run(max_calls=12)
    ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            call_id = run.begin(ToolId.SEARCH_PLACES, authority=AUTHORITY).call_id
        except ToolBudgetExceeded:
            return
        with lock:
            ids.append(call_id)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ids) == 12 == len(set(ids))


def test_a_run_is_bound_to_a_principal_and_a_run_id() -> None:
    run = new_run(principal_id="user-9", run_id="run-9")
    assert (run.principal_id, run.run_id) == ("user-9", "run-9")
    for bad in ({"principal_id": ""}, {"run_id": "has space"}, {"principal_id": "x" * 300}):
        with pytest.raises(ValueError):
            new_run(**bad)


# --- telemetry ------------------------------------------------------------------------------------------------
def test_each_call_emits_one_content_free_tool_event_with_latency_and_units() -> None:
    clock = Clock()

    class Slow(FakeMcpTransport):
        def send(self, request, credential):
            clock.advance(0.25)
            return super().send(request, credential)

    sink = InMemorySink()
    transport = Slow([ok(), mcp_status(429), TransportTimeout()])
    client = GroundingLiteMcpClient(transport, lambda: SECRET, config=CFG, sink=sink, clock=clock, now=lambda: NOW)
    run = new_run()
    client.call_tool(run, "search_places", ARGS)
    for _ in range(2):
        with pytest.raises(DiscoveryError):
            client.call_tool(run, "lookup_weather", {"location": LOC})
    first, limited, timed_out = sink.events
    assert all(isinstance(e, ToolCall) for e in sink.events)
    assert (first.tool, first.outcome, first.latency_ms, first.billable_units) == (
        ToolName.SEARCH_PLACES, ToolOutcome.OK, 250, 1)
    assert (limited.tool, limited.outcome) == (ToolName.LOOKUP_WEATHER, ToolOutcome.ERROR)
    assert timed_out.outcome == ToolOutcome.TIMEOUT
    assert first.evidence_ids and first.evidence_ids[0].startswith("ev-tc-")
    dumped = "".join(e.model_dump_json() for e in sink.events)
    for forbidden in ("quiet", "SYNTH", "43.65", "-79.38", "Synthetic"):
        assert forbidden not in dumped


def test_the_five_discovery_tools_exist_in_the_telemetry_enum() -> None:
    names = {"SEARCH_PLACES", "LOOKUP_WEATHER", "COMPUTE_ROUTES", "RESOLVE_NAMES", "RESOLVE_MAPS_URLS"}
    assert {n.value for n in ToolName if n.name in names} == {t.value for t in ToolId}


def test_a_telemetry_failure_never_breaks_the_call() -> None:
    class Broken:
        def emit(self, event):
            raise RuntimeError("sink down")

    _, client = make_client([ok()], sink=Broken())
    assert client.call_tool(new_run(), "search_places", ARGS).billable_units == 1


# --- L11: raised errors do not hold the payload, arguments or key ----------------------------------------
CANARY_QUERY = "CANARYQUERY-private-trip-text"


def _frames_hold(error: BaseException, needles: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    tb = error.__traceback__
    tb = tb.tb_next if tb is not None else None  # skip the CALLER's own frame (see limits below)
    while tb is not None:
        for name, value in list(tb.tb_frame.f_locals.items()):
            if any(needle in repr(value) for needle in needles):
                hits.append(f"{tb.tb_frame.f_code.co_name}.{name}")
        tb = tb.tb_next
    return hits


@pytest.mark.parametrize("script", [[mcp_status(429)], [mcp_status(503)], [TransportTimeout()],
                                    [RuntimeError(SECRET)],
                                    [McpResponse(status_code=200, body={"jsonrpc": "2.0", "id": 1, "result": {
                                        "content": [{"type": "text", "text": "CANARYPAYLOAD not json"}]}})]])
def test_l11_raised_errors_do_not_reference_the_query_key_or_payload_in_their_frames(script) -> None:
    transport, client = make_client(script)
    with pytest.raises(DiscoveryError) as caught:
        client.call_tool(new_run(), "search_places", {"text_query": CANARY_QUERY})
    error = caught.value
    assert _frames_hold(error, (CANARY_QUERY, SECRET, "CANARYPAYLOAD")) == []
    assert CANARY_QUERY not in repr(vars(error)) and SECRET not in repr(vars(error))
    # Limits (best effort): the CALLER's own frame still holds the arguments it passed, and the
    # transport double keeps what a test chose to record. Neither is reachable from the error.
    assert not any(CANARY_QUERY in repr(r) for r in gc.get_referents(error))


def test_l11_pre_send_refusals_also_keep_arguments_out_of_their_frames() -> None:
    _, client = make_client([ok()])
    for tool, args in (("web_search", {"text_query": CANARY_QUERY}),
                       ("search_places", {"text_query": CANARY_QUERY, "bogus": 1})):
        with pytest.raises(DiscoveryError) as caught:
            client.call_tool(new_run(), tool, args)
        assert _frames_hold(caught.value, (CANARY_QUERY,)) == []


# --- the real HTTP transport (httpx.MockTransport, no network) -------------------------------------------
def good_request() -> McpRequest:
    transport = FakeMcpTransport([ok()])
    GroundingLiteMcpClient(transport, lambda: SECRET, config=CFG, now=lambda: NOW).call_tool(
        new_run(), "search_places", ARGS)
    return transport.requests[0]


RPC_OK = {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "structuredContent": synthetic_payload()}}


def test_the_http_transport_sends_the_key_only_through_an_auth_object() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-goog-api-key")  # documented header
        seen["enc"] = request.headers.get("accept-encoding")
        seen["accept"] = request.headers.get("accept")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=RPC_OK)

    transport = HttpxMcpTransport.for_tests(httpx.MockTransport(handler))
    sent = good_request()
    response = transport.send(sent, Credential(SECRET))
    assert seen["key"] == SECRET and seen["enc"] == "identity" and response.status_code == 200
    assert seen["accept"] == "application/json, text/event-stream"
    assert SECRET not in repr(sent) and "x-goog-api-key" not in {k.lower() for k in sent.headers}


def test_json_and_single_event_sse_replies_are_both_parsed() -> None:
    sse = ("event: message\r\ndata: " + json.dumps(RPC_OK) + "\r\n\r\n").encode()
    for response in (httpx.Response(200, json=RPC_OK),
                     httpx.Response(200, content=json.dumps(RPC_OK).encode(),
                                    headers={"content-type": "application/json; charset=utf-8"}),
                     httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})):
        transport = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r, resp=response: resp))
        assert transport.send(good_request(), Credential(SECRET)).body == RPC_OK


def test_unknown_content_types_and_multi_event_streams_yield_no_body() -> None:
    two = ("data: " + json.dumps(RPC_OK) + "\n\ndata: " + json.dumps(RPC_OK) + "\n\n").encode()
    for response in (httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
                     httpx.Response(200, content=two, headers={"content-type": "text/event-stream"}),
                     httpx.Response(200, content=b"data: not json\n\n", headers={"content-type": "text/event-stream"})):
        transport = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r, resp=response: resp))
        assert transport.send(good_request(), Credential(SECRET)).body is None


def test_parse_single_sse_event_is_strict() -> None:
    payload = json.dumps({"a": 1})
    assert parse_single_sse_event(f"data: {payload}\n\n".encode()) == {"a": 1}
    assert parse_single_sse_event(f"id: 1\nevent: message\ndata: {payload}\n\n".encode()) == {"a": 1}
    assert parse_single_sse_event(f": comment\ndata: {payload}\n\n".encode()) == {"a": 1}
    assert parse_single_sse_event(f"data: {payload[:1]}\ndata: {payload[1:]}\n\n".encode()) == {"a": 1}  # multi-line data
    for bad in (b"", b"event: x\n\n", f"data: {payload}\n\ndata: {payload}\n\n".encode(), b"data: NaN\n\n",
                b"\xff\xfe", b"data: [1]\n\n"):
        assert parse_single_sse_event(bad) is None


def test_the_http_transport_refuses_bad_urls_headers_and_bodies_before_io() -> None:
    from types import SimpleNamespace

    calls: list = []
    transport = HttpxMcpTransport.for_tests(httpx.MockTransport(
        lambda r: (calls.append(r), httpx.Response(200, json={}))[1]))
    good = good_request()
    for forged in (
        SimpleNamespace(url="http://mapstools.googleapis.com/mcp", headers=good.headers, body=good.body, timeout_seconds=1.0),
        SimpleNamespace(url=good.url, headers={**good.headers, "Cookie": "a=b"}, body=good.body, timeout_seconds=1.0),
        SimpleNamespace(url=good.url, headers=good.headers, body={**good.body, "method": "tools/list"}, timeout_seconds=1.0),
    ):
        with pytest.raises(DiscoveryError):
            transport.send(forged, Credential(SECRET))  # type: ignore[arg-type]
    assert calls == []


def test_the_real_client_is_locked_down(monkeypatch) -> None:
    captured: dict = {}
    real = httpx.Client

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    transport = HttpxMcpTransport()
    assert captured["trust_env"] is False and captured["follow_redirects"] is False and captured["verify"] is True
    for forbidden in ("proxy", "proxies", "mounts", "event_hooks", "headers", "cookies", "base_url", "auth"):
        assert forbidden not in captured
    with pytest.raises(TypeError):
        HttpxMcpTransport(httpx.Client())  # type: ignore[call-arg]
    assert transport._client.trust_env is False  # noqa: SLF001


def test_the_http_transport_refuses_compressed_oversize_and_slow_replies() -> None:
    sent = good_request()
    # (gzip is now decoded under a hard cap on decoded bytes; see test_discovery_live_shapes.py)
    unknown = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r: httpx.Response(
        200, content=gzip.compress(b"{}"), headers={"content-encoding": "compress"})))
    with pytest.raises(TransportFailure):
        unknown.send(sent, Credential(SECRET))
    big = HttpxMcpTransport.for_tests(httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 600_000)))
    with pytest.raises(TransportFailure):
        big.send(sent, Credential(SECRET))
    clock = Clock()

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                clock.advance(2.0)
                yield b" "

    slow = HttpxMcpTransport.for_tests(
        httpx.MockTransport(lambda r: httpx.Response(200, stream=SlowStream())), clock=clock)
    with pytest.raises(TransportTimeout):
        slow.send(sent, Credential(SECRET))


def test_redirects_are_not_followed() -> None:
    hits: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    transport = HttpxMcpTransport.for_tests(httpx.MockTransport(handler))
    response = transport.send(good_request(), Credential(SECRET))
    assert response.status_code == 302 and hits == [GROUNDING_LITE_ENDPOINT]


def test_per_call_timeout_is_bounded_by_the_configured_value() -> None:
    transport, client = make_client([ok()], timeout_seconds=3.0)
    client.call_tool(new_run(), "search_places", ARGS)
    assert transport.requests[0].timeout_seconds == 3.0
    for bad in (60.0, 0):
        with pytest.raises(DiscoveryConfigError):
            make_client([], timeout_seconds=bad)
