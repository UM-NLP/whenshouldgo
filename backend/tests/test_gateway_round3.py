"""Second security review fixes (F1-F10, D5, T1-T7). Written before the fixes."""
from __future__ import annotations

import ast
import gzip
import json
import math
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel
from test_gateway_support import (
    GOOD,
    MODEL,
    PROVIDER,
    SEARCH_TOOL,
    SECRET,
    Clock,
    Reply,
    make_config,
    make_gateway,
    make_policy,
    make_request,
)

from app.domain import ContentClass, evolve
from app.gateway import (
    BreakerState,
    BudgetExceededError,
    CallRole,
    ContextMessage,
    Credential,
    GatewayConfigError,
    GatewayError,
    HttpxTransport,
    MalformedOutputError,
    ModelPrice,
    OpenRouterGateway,
    OutputSchema,
    PriceTable,
    PrivacyViolationError,
    ProviderUnavailableError,
    ResiliencePolicy,
    ResilientGateway,
    RunBudget,
    ToolSpec,
    TransportRequest,
    TransportResponse,
    placeholder_price_table,
    validate_url,
)
from app.gateway.errors import TransportFailure, TransportTimeout
from gateway_fakes import FakeTransport, completion_response, tool_call_entry
from app.gateway.openrouter import OPENROUTER_CHAT_URL
from app.gateway.schema import schema_is_supported, validate

CFG = make_config()
APP_DIR = Path(__file__).resolve().parents[1] / "app"
JSON_HEADERS = {"Content-Type": "application/json"}


def good_body() -> dict:
    transport = FakeTransport([completion_response(GOOD)])
    make_gateway(transport).generate(make_request())
    return json.loads(json.dumps(transport.requests[0].body))


def tool_request(tool: ToolSpec, **kw):
    return make_request(allowed_tools=(tool,), allow_repair=False, **kw)


def only_tool(parameters: dict) -> ToolSpec:
    return ToolSpec(name="search_places", description="d", parameters=parameters)


def priced_config():
    return make_config(price_table_version="prices-r3")


def priced_table() -> PriceTable:
    return PriceTable(
        version="prices-r3", placeholder=False,
        model_prices=(ModelPrice(model_id=MODEL, provider=PROVIDER,
                                 input_usd_per_million=Decimal("1.00"),
                                 output_usd_per_million=Decimal("2.00")),),
    )


# --- F1: repair overspend ---------------------------------------------------------------
def test_f1_repair_cannot_push_total_usage_past_the_original_remaining_budget() -> None:
    budget = evolve(RunBudget.from_ceilings(CFG.token_ceilings), input_tokens_remaining=1_000)
    request = make_request(budget=budget, estimated_input_tokens=100, max_output_tokens=100)
    transport = FakeTransport([
        completion_response("bad", prompt_tokens=800, completion_tokens=20),
        completion_response(GOOD, prompt_tokens=900, completion_tokens=20),
    ])
    with pytest.raises(BudgetExceededError) as caught:
        make_gateway(transport).generate(request)
    assert caught.value.usage is not None and caught.value.usage.input_tokens == 1_700
    assert caught.value.calls == 2


def test_f1_a_repair_that_fits_the_remaining_budget_still_succeeds() -> None:
    budget = evolve(RunBudget.from_ceilings(CFG.token_ceilings), input_tokens_remaining=1_000)
    request = make_request(budget=budget, estimated_input_tokens=100, max_output_tokens=100)
    transport = FakeTransport([
        completion_response("bad", prompt_tokens=800, completion_tokens=20),
        completion_response(GOOD, prompt_tokens=190, completion_tokens=20),
    ])
    turn = make_gateway(transport).generate(request)
    assert turn.usage.input_tokens == 990 and turn.repairs == 1


# --- F2: headers and injected clients ---------------------------------------------------
@pytest.mark.parametrize(
    "headers",
    [{"Content-Type": "application/json", "Cookie": "a=b"},
     {"Content-Type": "application/json", "X-Evil": "1"},
     {"Content-Type": "application/json", "Host": "evil.example"},
     {"Content-Type": "text/plain"}, {}],
)
def test_f2_headers_must_be_exactly_the_allowlist(headers) -> None:
    with pytest.raises(ValueError):
        TransportRequest(url=OPENROUTER_CHAT_URL, headers=headers, body=good_body(),
                         timeout_seconds=1.0)
    calls: list = []
    transport = HttpxTransport.for_tests(httpx.MockTransport(
        lambda r: (calls.append(r), httpx.Response(200, json={}))[1]))
    forged = TransportRequest.model_construct(
        url=OPENROUTER_CHAT_URL, headers=headers, body=good_body(), timeout_seconds=1.0)
    with pytest.raises(GatewayConfigError):
        transport.send(forged, Credential(SECRET))
    assert calls == []


def test_f2_the_constructor_refuses_an_injected_client() -> None:
    with pytest.raises(TypeError):
        HttpxTransport(httpx.Client(trust_env=False))  # type: ignore[call-arg]


def test_f2_the_real_client_is_built_locked_down(monkeypatch) -> None:
    captured: dict = {}
    real = httpx.Client

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", spy)
    transport = HttpxTransport()
    assert captured["trust_env"] is False and captured["follow_redirects"] is False
    assert captured["verify"] is True
    for forbidden in ("proxy", "proxies", "mounts", "event_hooks", "headers", "cookies",
                      "base_url", "auth", "cert"):
        assert forbidden not in captured
    client = transport._client  # noqa: SLF001
    assert not client._mounts and client.base_url == httpx.URL("")  # noqa: SLF001
    assert not any(client.event_hooks.values()) and len(client.cookies) == 0
    assert client.auth is None


def test_f2_test_factory_still_locks_the_client_settings() -> None:
    client = HttpxTransport.for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={})))._client  # noqa: SLF001
    assert client.trust_env is False and client.follow_redirects is False
    assert not any(client.event_hooks.values()) and len(client.cookies) == 0


# --- F3: slow responses and compression -------------------------------------------------
class SlowStream(httpx.SyncByteStream):
    def __init__(self, clock: Clock, per_chunk: float) -> None:
        self._clock, self._per_chunk = clock, per_chunk

    def __iter__(self):
        for _ in range(10):
            self._clock.advance(self._per_chunk)
            yield b" "


def test_f3_a_slow_stream_is_cut_off_at_the_request_deadline() -> None:
    clock = Clock()
    handler = lambda r: httpx.Response(200, stream=SlowStream(clock, 0.6))  # noqa: E731
    transport = HttpxTransport.for_tests(httpx.MockTransport(handler), clock=clock)
    sent = TransportRequest(url=OPENROUTER_CHAT_URL, headers=JSON_HEADERS, body=good_body(),
                            timeout_seconds=1.0)
    with pytest.raises(TransportTimeout):
        transport.send(sent, Credential(SECRET))


def test_f3_requests_ask_for_identity_encoding_and_compressed_replies_are_refused() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["enc"] = request.headers.get("accept-encoding")
        return httpx.Response(200, content=gzip.compress(b"{}"),
                              headers={"content-encoding": "gzip"})

    transport = HttpxTransport.for_tests(httpx.MockTransport(handler))
    sent = TransportRequest(url=OPENROUTER_CHAT_URL, headers=JSON_HEADERS, body=good_body(),
                            timeout_seconds=1.0)
    with pytest.raises(TransportFailure):
        transport.send(sent, Credential(SECRET))
    assert seen["enc"] == "identity"


# --- F4: retried success reports accumulated usage and cost ------------------------------
def _ladder(script, *, config=None, table=None):
    clock = Clock()
    transport = FakeTransport(script)
    gateway = make_gateway(transport, clock=clock, config=config, prices=table)
    ladder = ResilientGateway(
        gateway, ResiliencePolicy(), price_table=table or placeholder_price_table(MODEL, PROVIDER),
        clock=clock, sleep=lambda s: None)
    return transport, ladder


def test_f4_failed_attempts_without_usage_are_charged_at_the_worst_case() -> None:
    # Updated deliberately: 429 and 503 are rejected before processing and cost nothing
    # (see test_gateway_rejection_charging.py). Only calls that may have been processed
    # without usage coming back (timeout, connection reset) are charged at the worst case.
    config, table = priced_config(), priced_table()
    for failure in (TransportTimeout(), TransportFailure(),
                    TransportResponse(status_code=504, body=None)):
        _, ladder = _ladder([failure, completion_response(GOOD)], config=config, table=table)
        turn = ladder.generate(make_request(runtime_config=config))
        # worst case of the failed call (1,000 in / 600 out) plus the success (100 / 20)
        assert (turn.usage.input_tokens, turn.usage.output_tokens) == (1_100, 620)
        assert turn.estimated_cost_usd == Decimal("0.00234000")  # re-priced on the total
        assert turn.model_calls == 2


def test_f4_failed_attempt_with_reported_usage_adds_that_usage() -> None:
    config, table = priced_config(), priced_table()
    script = [completion_response("bad"), TransportResponse(status_code=429, body=None),
              completion_response(GOOD)]
    _, ladder = _ladder(script, config=config, table=table)
    turn = ladder.generate(make_request(runtime_config=config))
    assert turn.model_calls == 3
    assert turn.usage.input_tokens >= 200  # the first inner call's reported usage is kept


# --- F5: non-finite numbers and bool/int confusion --------------------------------------
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_f5_non_finite_json_constants_are_malformed_tool_arguments(literal: str) -> None:
    tool = only_tool({"type": "object", "additionalProperties": False,
                      "properties": {"n": {"type": "number"}}})
    entry = tool_call_entry("search_places", {})
    entry["function"]["arguments"] = '{"n": %s}' % literal
    reply = completion_response(None, finish_reason="tool_calls", tool_calls=[entry])
    with pytest.raises(MalformedOutputError):
        make_gateway(FakeTransport([reply])).generate(tool_request(tool))


def test_f5_non_finite_numbers_are_malformed_structured_output() -> None:
    class Num(BaseModel):
        model_config = {"extra": "forbid"}
        n: float

    request = make_request(output_schema=OutputSchema(name="num", model=Num), allow_repair=False)
    for literal in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(MalformedOutputError):
            make_gateway(FakeTransport([completion_response('{"n": %s}' % literal)])).generate(request)


def test_f5_validator_rejects_non_finite_floats_even_against_min_max() -> None:
    schema = {"type": "number", "minimum": 0, "maximum": 1}
    for value in (math.nan, math.inf, -math.inf):
        assert validate(schema, value) is False
    assert validate(schema, 0.5) is True
    assert not schema_is_supported({"type": "number", "minimum": math.nan})


def test_f5_enum_and_const_are_type_aware() -> None:
    assert validate({"enum": [1]}, True) is False
    assert validate({"enum": [0, 1]}, False) is False
    assert validate({"enum": [True]}, 1) is False
    assert validate({"const": 0}, False) is False
    assert validate({"const": True}, 1) is False
    assert validate({"enum": [1, "a"]}, 1) is True
    assert validate({"const": False}, False) is True
    assert validate({"enum": [[1]]}, [True]) is False  # nested


def test_f5_non_finite_values_cannot_be_reserialised_into_a_request() -> None:
    from app.gateway import ToolCall

    call = ToolCall(id="c1", name="search_places", arguments={"n": float("nan")})
    context = (
        ContextMessage(role="user", content="u", content_class=ContentClass.USER),
        ContextMessage(role="assistant", content="", content_class=ContentClass.USER,
                       tool_calls=(call,)),
    )
    transport = FakeTransport([completion_response(GOOD)])
    with pytest.raises(GatewayError):
        make_gateway(transport).generate(make_request(context=context))
    assert transport.requests == []


# --- F6: strict tool schemas --------------------------------------------------------------
@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "object", "properties": {}},  # additionalProperties missing
        {"type": "object", "properties": {}, "additionalProperties": True},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"],
         "additionalProperties": False},  # required not in properties
        {"type": "object", "additionalProperties": False, "properties": {
            "o": {"type": "object", "properties": {}}}},  # nested object not closed
        {"type": "object", "additionalProperties": False, "properties": {
            "o": {"type": "array", "items": {"type": "object", "properties": {}}}}},
        {"type": "object", "additionalProperties": False, "properties": {
            "o": {"type": "object", "additionalProperties": False, "required": ["x"],
                  "properties": {}}}},
    ],
)
def test_f6_open_or_inconsistent_object_schemas_are_rejected(parameters) -> None:
    with pytest.raises(ValueError):
        ToolSpec(name="t", description="d", parameters=parameters)


def test_f6_closed_consistent_schemas_pass() -> None:
    only_tool({"type": "object", "additionalProperties": False, "required": ["a"],
               "properties": {"a": {"type": "string"},
                              "o": {"type": "object", "additionalProperties": False,
                                    "properties": {}}}})
    assert schema_is_supported({"type": "object", "additionalProperties": False, "properties": {}})


# --- F7: input-token estimate is a floor, not trusted --------------------------------------
def test_f7_a_tiny_estimate_cannot_hide_a_huge_context() -> None:
    huge = ContextMessage(role="user", content="x" * 100_000, content_class=ContentClass.USER)
    transport = FakeTransport([completion_response(GOOD)])
    with pytest.raises(BudgetExceededError):
        make_gateway(transport).generate(make_request(context=(huge,), estimated_input_tokens=1))
    assert transport.requests == []


def test_f7_the_bound_counts_tool_and_schema_text_and_feeds_the_worst_case() -> None:
    request = make_request(estimated_input_tokens=1)
    assert request.worst_case_usage.input_tokens >= 1
    padded = ToolSpec(name="search_places", description="d" * 900,
                      parameters={"type": "object", "additionalProperties": False,
                                  "properties": {}})
    bigger = make_request(estimated_input_tokens=1, allowed_tools=(padded,))
    assert bigger.worst_case_usage.input_tokens > request.worst_case_usage.input_tokens


def test_f7_total_context_characters_are_capped() -> None:
    parts = tuple(
        ContextMessage(role="user", content="x" * 150_000, content_class=ContentClass.USER)
        for _ in range(4)
    )
    budget = evolve(RunBudget.from_ceilings(CFG.token_ceilings),
                    per_call_max_input_tokens=12_000, input_tokens_remaining=40_000)
    with pytest.raises(BudgetExceededError):
        make_gateway(FakeTransport([])).generate(
            make_request(context=parts, estimated_input_tokens=1, budget=budget))


# --- F10 ------------------------------------------------------------------------------------
class CapturingTransport(FakeTransport):
    def __init__(self, script) -> None:
        super().__init__(script)
        self.keys: list[str] = []

    def send(self, request, credential):
        self.keys.append(credential.reveal())
        return super().send(request, credential)


def test_f10_the_secret_is_stripped_before_use() -> None:
    transport = CapturingTransport([completion_response(GOOD)])
    make_gateway(transport, secret="  sk-test-key \n").generate(make_request())
    assert transport.keys == ["sk-test-key"]


@pytest.mark.parametrize("secret", ["", "  ", "sk-a b", "sk-a\nb", "sk-a\tb", "sk-\x00x", "sk-é"])
def test_f10_secrets_with_whitespace_control_or_odd_chars_are_refused(secret: str) -> None:
    transport = FakeTransport([completion_response(GOOD)])
    with pytest.raises(GatewayConfigError):
        make_gateway(transport, secret=secret).generate(make_request())
    assert transport.requests == []


@pytest.mark.parametrize(
    "url",
    ["https://openrouter.ai/api/v1/chat/\ncompletions", "https://openrouter.ai/api/v1/chat/completions\t",
     "https://openrouter.ai/api/v1/chat/completions\x00", "https://openrouter.ai/api/v1/chat/complétions",
     "https://openrouter.ai/api/v1/chat/completions ", " https://openrouter.ai/api/v1/chat/completions",
     "https://openrouter.ai/‮api/v1/chat/completions"],
)
def test_f10_urls_with_control_whitespace_or_non_ascii_are_refused(url: str) -> None:
    with pytest.raises(GatewayConfigError):
        validate_url(url)


@pytest.mark.parametrize("error", [RuntimeError("x"), ValueError("y"), httpx.InvalidURL("z"),
                                   KeyError("k")])
def test_f10_unexpected_transport_exceptions_become_typed_errors_without_chaining(error) -> None:
    with pytest.raises(ProviderUnavailableError) as caught:
        make_gateway(FakeTransport([error])).generate(make_request())
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    bare = HttpxTransport.for_tests(httpx.MockTransport(lambda r: (_ for _ in ()).throw(error)))
    sent = TransportRequest(url=OPENROUTER_CHAT_URL, headers=JSON_HEADERS, body=good_body(),
                            timeout_seconds=1.0)
    with pytest.raises(TransportFailure) as raised:
        bare.send(sent, Credential(SECRET))
    assert raised.value.__context__ is None


def test_f10_a_response_served_by_another_allowlisted_provider_is_refused() -> None:
    with pytest.raises(PrivacyViolationError):
        make_gateway(FakeTransport([completion_response(GOOD, provider="hosttwo")])).generate(
            make_request())
    turn = make_gateway(FakeTransport([completion_response(GOOD, provider="HostOne")])).generate(
        make_request())
    assert turn.provider == PROVIDER


def test_f10_tool_turns_require_the_tool_calls_finish_reason() -> None:
    call = [tool_call_entry("search_places", {})]
    request = make_request(allowed_tools=(SEARCH_TOOL,), allow_repair=False)
    with pytest.raises(MalformedOutputError):
        make_gateway(FakeTransport([completion_response(None, finish_reason="stop",
                                                        tool_calls=call)])).generate(request)
    turn = make_gateway(FakeTransport([completion_response(None, finish_reason="tool_calls",
                                                           tool_calls=call)])).generate(request)
    assert turn.tool_calls[0].name == "search_places"


def test_f10_output_models_must_forbid_extra_fields() -> None:
    class Loose(BaseModel):
        answer: str

    class Nested(BaseModel):
        model_config = {"extra": "forbid"}
        inner: Loose

    for model in (Loose, Nested):
        with pytest.raises(GatewayConfigError):
            make_gateway(FakeTransport([])).generate(
                make_request(output_schema=OutputSchema(name="o", model=model)))


def test_t7_missing_choices_and_tool_finish_without_calls_are_typed_malformed() -> None:
    no_choices = TransportResponse(
        status_code=200, body={"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    empty = TransportResponse(status_code=200, body={
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": []})
    no_calls = completion_response(None, finish_reason="tool_calls")
    for response in (no_choices, empty, no_calls):
        with pytest.raises(MalformedOutputError):
            make_gateway(FakeTransport([response])).generate(
                make_request(allowed_tools=(SEARCH_TOOL,), allow_repair=False))


# --- F8 -------------------------------------------------------------------------------------
def test_f8_the_acceptance_criterion_is_written_down() -> None:
    from app.gateway import types

    text = types.TOOL_RESULT_ACCEPTANCE_CRITERION
    assert "tool layer" in text and "registry" in text and "tool name" in text
    assert "TOOL_RESULT_ACCEPTANCE_CRITERION" in (types.__doc__ or "")


def test_orchestrator_path_never_sends_maps_text_to_frame_or_compose(caplog) -> None:
    """Full path (FRAME -> server-run discovery -> verbatim block) with synthetic grounded output
    carrying a canary. Phase 0 design decision: the model never sees Maps content, so the canary
    is in the HTTP response exactly once and in no gateway request, event, log, replay record,
    workspace or exception."""
    import logging
    import traceback

    from maps_simulator import FakeMcpTransport, mcp_ok
    from wiring_support import (
        CANARY,
        TITLE_CANARY,
        Env,
        RaisingClient,
        RecordingClient,
        ScriptedGateway,
        canary_payload,
        deep_text,
        frame_turn,
        real_client,
        wire_text,
    )

    from app.discovery import (
        DiscoveryUnavailable,
        GroundedTextPart,
        RenderedBlock,
        SourcePart,
        content_hash,
        verify_verbatim,
    )
    from app.discovery.render import BRANDING
    from app.failures import TurnFailure
    from app.orchestrator import discovery_cache_key
    from app.domain import Principal, PrincipalKind

    caplog.set_level(logging.DEBUG)
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config), frame_turn(config)], config)
    transport = FakeMcpTransport([mcp_ok(canary_payload())])
    env = Env(gateway=gateway, config=config, discovery=lambda e: RecordingClient(
        real_client(config, transport, e.sink, e.clock)))
    response = env.post({"message": "Plan a quiet Toronto restaurant dinner tonight"}, key="canary")
    assert response.status_code == 200
    replay = env.post({"message": "Plan a quiet Toronto restaurant dinner tonight"}, key="canary")
    assert replay.json() == response.json()

    # 1. the model saw only user text: no recorded request mentions the canary or Maps content
    assert len(gateway.requests) == 1
    for request in gateway.requests:
        assert request.call_role is CallRole.FRAME
        assert request.privacy_class.value == "USER_PRIVATE"
        wire = wire_text(request)
        assert CANARY not in wire and TITLE_CANARY not in wire and "google.com/maps" not in wire
        assert {m.content_class for m in request.context} <= {ContentClass.USER, ContentClass.NONE}
    # 2. nowhere else it must not be
    state = [deep_text(env.sink.events), caplog.text, deep_text(env.costs.entries()),
             deep_text(env.orchestrator._committed), deep_text(env.orchestrator.workspaces),
             *(r.getMessage() for r in caplog.records)]
    assert all(CANARY not in blob and TITLE_CANARY not in blob for blob in state)
    failing = Env(gateway=ScriptedGateway([frame_turn(config)], config),
                  discovery=RaisingClient(DiscoveryUnavailable(calls=1)), config=config)
    with pytest.raises(TurnFailure) as caught:
        failing.orchestrator.run_turn(
            Principal("guest-a", PrincipalKind.GUEST, failing.quotas.policies.current().version),
            "Plan a quiet Toronto restaurant dinner tonight", "k")
    rendered_error = str(caught.value) + repr(caught.value) + "".join(
        traceback.format_exception(caught.value))
    assert CANARY not in rendered_error
    # 3. the HTTP response carries it exactly once, verbatim, with the sources after it
    raw = response.text
    assert raw.count(CANARY) == 1 and raw.index('"text"') < raw.index('"places"')
    block = response.json()["discovery"]
    assert block["text"] == canary_payload()["summary"]
    # 4. and the block passes verify_verbatim against the ledger the client wrote
    validated = env.cache.get("guest-a", discovery_cache_key("canary"))
    run = env.discovery.runs[0]
    assert validated is not None and len(block["places"]) == len(validated.answer.places)
    parts = [GroundedTextPart(text=block["text"], content_hash=content_hash(block["text"]))]
    for shown, place in zip(block["places"], validated.answer.places, strict=True):
        assert (shown["title"], shown["attribution_url"], shown["place_url"]) == (
            place.attribution_title, place.attribution_url, place.place_url)
        parts.append(SourcePart(place_id=place.place_id, attribution_title=shown["title"],
                                attribution_url=shown["attribution_url"],
                                place_url=shown["place_url"], cited=place.cited))
    from app.discovery import FramingKind

    rebuilt = RenderedBlock(framing=FramingKind(block["framing"]), parts=tuple(parts))
    assert block["branding"] == BRANDING == rebuilt.branding and block["translate_no"] is True
    verify_verbatim(rebuilt, validated, run)  # raises VerbatimViolation if anything differs


# --- D5 -------------------------------------------------------------------------------------
def test_d5_test_doubles_live_outside_the_app_package() -> None:
    assert not (APP_DIR / "gateway" / "fakes.py").exists()
    assert (APP_DIR.parent / "tests" / "gateway_fakes.py").exists()
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *[a.name for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any("fakes" in name for name in names):
                offenders.append(path.name)
    assert offenders == []


# --- T1: caps are what reject, not the schema -----------------------------------------------
NESTING_TOOL = ToolSpec(
    name="search_places", description="d",
    parameters={"type": "object", "additionalProperties": False,
                "properties": {"q": {"type": "string"}, "d": {"type": "array"},
                               "pad": {"type": "string"}}},
)


def _args_reply(arguments: dict | str):
    entry = tool_call_entry("search_places", {})
    entry["function"]["arguments"] = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return completion_response(None, finish_reason="tool_calls", tool_calls=[entry])


def test_t1_depth_and_size_caps_are_the_only_reason_for_rejection(monkeypatch) -> None:
    deep = {"q": "a", "d": json.loads("[" * 30 + "]" * 30)}
    big = {"q": "a", "pad": "x" * 9_000}
    for arguments in (deep, big):
        # the schema allows both keys, so rejection can only come from a cap
        assert validate(NESTING_TOOL.parameters, arguments) is True
        with pytest.raises(MalformedOutputError):
            make_gateway(FakeTransport([_args_reply(arguments)])).generate(tool_request(NESTING_TOOL))
    monkeypatch.setattr("app.gateway.openrouter.MAX_ARGUMENT_DEPTH", 100)
    monkeypatch.setattr("app.gateway.openrouter.MAX_TOOL_ARGUMENT_BYTES", 100_000)
    for arguments in (deep, big):  # raise the caps and the same input is accepted
        turn = make_gateway(FakeTransport([_args_reply(arguments)])).generate(tool_request(NESTING_TOOL))
        assert turn.tool_calls[0].arguments["q"] == "a"


# --- T5 / T6 ----------------------------------------------------------------------------------
def test_t5_app_never_writes_to_stdout_or_stderr_directly() -> None:
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                offenders.append(f"{path.name}:{node.lineno} print")
            if (isinstance(func, ast.Attribute) and func.attr in {"write", "writelines"}
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr in {"stdout", "stderr"}):
                offenders.append(f"{path.name}:{node.lineno} {func.value.attr}.write")
    assert offenders == []


def test_t6_breaker_decision_provider_answers_close_it_except_off_allowlist_ones() -> None:
    """Design decision: the breaker measures whether the PINNED provider is reachable. A
    definitive answer from it (content filter, 401 rejection, quota, malformed output)
    proves liveness, so it closes a half-open breaker. An answer from an off-allowlist
    provider is a privacy incident, not proof the pinned host is healthy, so it is neutral:
    it releases the probe slot and leaves the breaker half-open."""
    for response, expected in (
        (completion_response(GOOD, finish_reason="content_filter"), BreakerState.CLOSED),
        (TransportResponse(status_code=401, body=None), BreakerState.CLOSED),
        (completion_response(GOOD, provider="SomeOtherHost"), BreakerState.HALF_OPEN),
    ):
        clock = Clock()
        gateway = make_gateway(FakeTransport([response]), clock=clock)
        ladder = ResilientGateway(
            gateway, ResiliencePolicy(breaker_failure_threshold=1, breaker_open_seconds=10),
            price_table=placeholder_price_table(MODEL, PROVIDER), clock=clock)
        breaker = ladder.breaker_for(PROVIDER)
        breaker.before_call()
        breaker.record_failure()
        clock.advance(10)
        with pytest.raises(GatewayError):
            ladder.generate(make_request())
        assert breaker.state == expected
