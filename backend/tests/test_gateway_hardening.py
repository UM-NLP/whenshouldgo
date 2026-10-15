"""Gateway hardening after the security review (H1, H2, M1-M8, L1-L8, contracts)."""
from __future__ import annotations

import ast
import copy
import json
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict
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
    make_ladder,
    make_policy,
    make_request,
    tool_message,
)

from app.domain import (
    ContentClass,
    Evidence,
    Fact,
    TokenCeilings,
    WorkspacePatch,
    evolve,
)
from app.gateway import (
    FRAME_STATE_SCHEMA_ALLOWLIST,
    MODEL_ID_PATTERN,
    BreakerState,
    CallRole,
    CircuitBreaker,
    CircuitOpenError,
    ContextMessage,
    Credential,
    GatewayConfigError,
    GatewayError,
    HttpxTransport,
    MalformedOutputError,
    ModelTurn,
    OpenRouterGateway,
    OutputSchema,
    PrivacyClass,
    PrivacyViolationError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ResiliencePolicy,
    ResilientGateway,
    RunBudget,
    TaintedContextError,
    ToolProvenance,
    ToolSpec,
    TransportRequest,
    TransportResponse,
    check_request,
    placeholder_price_table,
    validate_body_shape,
    validate_url,
)
from gateway_fakes import (
    FakeTransport,
    completion_response,
    synthetic_maps_turn,
    tool_call_entry,
)
from app.gateway.openrouter import MAX_RESPONSE_BYTES, OPENROUTER_CHAT_URL, _guarded

CFG = make_config()
APP_DIR = Path(__file__).resolve().parents[1] / "app"


def good_body() -> dict:
    transport = FakeTransport([completion_response(GOOD)])
    make_gateway(transport).generate(make_request())
    return copy.deepcopy(transport.requests[0].body)


# --- H1: Maps taint is not fail-open ---------------------------------------------------
def test_h1_content_class_is_required_not_defaulted() -> None:
    with pytest.raises(ValueError):
        ContextMessage(role="user", content="x")  # type: ignore[call-arg]


@pytest.mark.parametrize("cls", [ContentClass.USER, ContentClass.NONE])
def test_h1_a_tool_message_cannot_be_user_or_none_content(cls: ContentClass) -> None:
    with pytest.raises(ValueError):
        ToolProvenance(tool="search_places", tool_call_id="c1", content_class=cls)
    with pytest.raises(ValueError):
        ContextMessage(role="tool", content="MAPS", content_class=cls)
    with pytest.raises(ValueError):  # no provenance at all
        ContextMessage(role="tool", content="MAPS", content_class=ContentClass.MAPS_GROUNDED)
    with pytest.raises(ValueError):  # provenance and class disagree
        ContextMessage(
            role="tool", content="MAPS", content_class=ContentClass.MAPS_PLACES,
            provenance=ToolProvenance(tool="t", tool_call_id="c1",
                                      content_class=ContentClass.MAPS_GROUNDED),
        )


def test_h1_a_default_class_tool_message_is_refused_in_frame() -> None:
    legacy = dict(role="tool", content="MAPS-CANARY", tool_call_id="c1")
    with pytest.raises(ValueError):
        ContextMessage(**legacy)  # type: ignore[arg-type]


def test_h1_a_discover_turn_fed_back_stays_maps_grounded_and_cannot_reach_frame() -> None:
    message = ContextMessage.from_turn(synthetic_maps_turn(CFG))
    assert message.content_class == ContentClass.MAPS_GROUNDED and message.role == "assistant"
    user = ContextMessage(role="user", content="u", content_class=ContentClass.USER)
    for role in (CallRole.FRAME, CallRole.COMPOSE):
        transport = FakeTransport([completion_response(GOOD)])
        with pytest.raises(TaintedContextError):
            make_gateway(transport).generate(make_request(call_role=role, context=(user, message)))
        assert transport.requests == []


def test_h1_a_frame_turn_fed_back_stays_user_class() -> None:
    transport = FakeTransport([completion_response(GOOD)])
    turn = make_gateway(transport).generate(make_request())
    assert ContextMessage.from_turn(turn).content_class == ContentClass.USER


def test_h1_turn_classes_follow_the_call_role() -> None:
    discover = synthetic_maps_turn(CFG)
    with pytest.raises(ValueError):
        evolve(discover, content_class=ContentClass.USER)
    with pytest.raises(ValueError):
        evolve(discover, call_role=CallRole.FRAME)


# --- H2: enforcement lives in the transport --------------------------------------------
def _bare_transport(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    return HttpxTransport.for_tests(httpx.MockTransport(handler))


def _request_with(**changes):
    return TransportRequest(
        **{"url": OPENROUTER_CHAT_URL, "headers": {"Content-Type": "application/json"},
           "body": good_body(), "timeout_seconds": 1.0,
           **changes}
    )


def test_h2_bare_transport_refuses_a_weakened_body_before_any_io() -> None:
    calls: list = []
    sent = _request_with()
    sent.body["provider"]["zdr"] = False  # the dict is mutable after construction
    with pytest.raises(PrivacyViolationError):
        _bare_transport(calls).send(sent, Credential(SECRET))
    sent = _request_with()
    sent.body["plugins"] = [{"id": "web"}]
    with pytest.raises(PrivacyViolationError):
        _bare_transport(calls).send(sent, Credential(SECRET))
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://openrouter.ai/api/v1/chat/completions",
        "https://evil.example/api/v1/chat/completions",
        "https://openrouter.ai.evil.example/api/v1/chat/completions",
        "https://user:pw@openrouter.ai/api/v1/chat/completions",
        "https://openrouter.ai:8443/api/v1/chat/completions",
        "https://OPENROUTER.ai/api/v1/chat/completions",
        "https://openrouter.ai/api/v1/other",
        "https://openrouter.ai/api/v1/chat/completions?x=1",
        "https://openrouter.ai/api/v1/chat/completions#frag",
        "ftp://openrouter.ai/api/v1/chat/completions",
        "",
    ],
)
def test_h2_bad_urls_are_refused_everywhere(url: str) -> None:
    calls: list = []
    with pytest.raises(GatewayConfigError):
        validate_url(url)
    with pytest.raises(GatewayConfigError):
        _request_with(url=url)  # TransportRequest cannot be built with it
    duck = SimpleNamespace(url=url, headers={}, body=good_body(), timeout_seconds=1.0)
    with pytest.raises(GatewayConfigError):
        _bare_transport(calls).send(duck, Credential(SECRET))  # type: ignore[arg-type]
    assert calls == []
    with pytest.raises(GatewayConfigError):
        OpenRouterGateway(FakeTransport([]), lambda: SECRET, CFG, make_policy(),
                          placeholder_price_table(MODEL, PROVIDER), url=url)


def test_h2_transport_request_cannot_be_built_with_a_weak_body() -> None:
    body = good_body()
    body["provider"]["allow_fallbacks"] = True
    with pytest.raises(PrivacyViolationError):
        _request_with(body=body)


# --- M1: the body guard is an allowlist -------------------------------------------------
@pytest.mark.parametrize(
    "top", ["tool_choice", "preset", "reasoning", "include", "user", "session_id", "metadata",
            "usage", "stop", "plugins", "models", "route", "transforms", "parallel_tool_calls"],
)
def test_m1_unknown_top_level_keys_are_refused(top: str) -> None:
    body = good_body()
    body[top] = {"x": 1}
    with pytest.raises(PrivacyViolationError):
        validate_body_shape(body)


@pytest.mark.parametrize(
    "key", ["ignore", "sort", "max_price", "quantizations", "preferred_max_latency",
            "enforce_distillable_text"],
)
def test_m1_unknown_provider_keys_are_refused(key: str) -> None:
    body = good_body()
    body["provider"][key] = ["x"]
    with pytest.raises(PrivacyViolationError):
        validate_body_shape(body)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["provider"].update(only=[PROVIDER, "hosttwo"]),
        lambda b: b["provider"].update(order=[PROVIDER, "hosttwo"]),
        lambda b: b["provider"].update(only=["hosttwo"]),
        lambda b: b["provider"].update(require_parameters=False),
        lambda b: b["provider"].pop("require_parameters"),
        lambda b: b.update(model="openrouter/auto"),
        lambda b: b.update(stream=True),
        lambda b: b.update(temperature=1),
        lambda b: b.update(max_tokens=True),
        lambda b: b["response_format"].update(type="json_object"),
        lambda b: b["response_format"]["json_schema"].update(strict=False),
        lambda b: b["messages"][0].update(name="x"),
        lambda b: b["messages"][0].update(role="developer"),
        lambda b: b.update(tools=[{"type": "openrouter:web_search"}]),
        lambda b: b.update(tools=[{"type": "function", "function": {"name": "a"}}]),
        lambda b: b.update(tools=[{"type": "function", "function": {
            "name": "a", "description": "d", "parameters": {}}, "strict": True}]),
    ],
)
def test_m1_each_weakening_or_odd_shape_is_refused(mutate) -> None:
    body = good_body()
    mutate(body)
    with pytest.raises(PrivacyViolationError):
        validate_body_shape(body)


def test_m1_a_sound_body_with_tools_passes() -> None:
    request = make_request(call_role=CallRole.DISCOVER, allowed_tools=(SEARCH_TOOL,),
                           privacy_class=PrivacyClass.MAPS_CONTENT)
    transport = FakeTransport([completion_response(GOOD)])
    make_gateway(transport).generate(request)
    validate_body_shape(transport.requests[0].body)


# --- M2: no key or text in exception context --------------------------------------------
def _assert_clean(error: BaseException) -> None:
    assert error.__context__ is None and error.__cause__ is None
    rendered = "".join(traceback.format_exception(error)) + repr(error) + str(error)
    assert SECRET not in rendered and "USER-CANARY" not in rendered


def test_m2_errors_are_raised_outside_except_blocks() -> None:
    from app.gateway.errors import TransportFailure, TransportTimeout

    for script, error_type in (
        ([TransportTimeout()], GatewayError),
        ([TransportFailure()], GatewayError),
        ([completion_response("USER-CANARY not json")], MalformedOutputError),
        ([completion_response(GOOD, finish_reason="length")], GatewayError),
    ):
        with pytest.raises(error_type) as caught:
            make_gateway(FakeTransport(script)).generate(make_request(allow_repair=False))
        _assert_clean(caught.value)


def test_m2_secret_provider_failures_do_not_chain_the_original_exception() -> None:
    def broken() -> str:
        raise RuntimeError(f"vault said {SECRET}")

    gateway = OpenRouterGateway(FakeTransport([]), broken, CFG, make_policy(),
                                placeholder_price_table(MODEL, PROVIDER), clock=Clock())
    with pytest.raises(GatewayConfigError) as caught:
        gateway.generate(make_request())
    _assert_clean(caught.value)


def test_m2_httpx_key_travels_in_an_auth_object_not_a_headers_dict() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    transport = HttpxTransport.for_tests(httpx.MockTransport(handler))
    sent = _request_with()
    transport.send(sent, Credential(SECRET))
    assert seen["auth"] == f"Bearer {SECRET}"
    assert SECRET not in repr(sent.headers) and "Authorization" not in sent.headers


def test_m2_httpx_failures_do_not_chain() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy said {SECRET}", request=request)

    transport = HttpxTransport.for_tests(httpx.MockTransport(boom))
    from app.gateway.errors import TransportFailure

    with pytest.raises(TransportFailure) as caught:
        transport.send(_request_with(), Credential(SECRET))
    _assert_clean(caught.value)


# --- M3: calls are counted, including the repair ---------------------------------------
def test_m3_error_reports_every_call_sent_including_the_repair() -> None:
    transport = FakeTransport(
        [completion_response("bad"), TransportResponse(status_code=429, body=None)]
    )
    with pytest.raises(ProviderRateLimitError) as caught:
        make_gateway(transport).generate(make_request())
    assert caught.value.calls == 2


def test_m3_errors_raised_before_sending_report_zero_calls() -> None:
    transport = FakeTransport([])
    with pytest.raises(TaintedContextError) as caught:
        make_gateway(transport).generate(make_request(privacy_class=PrivacyClass.MAPS_CONTENT))
    assert caught.value.calls == 0


def test_m3_ladder_does_not_retry_past_the_call_budget_after_a_repair() -> None:
    budget = RunBudget.from_ceilings(CFG.token_ceilings).consume(calls=4)  # two calls left
    transport = FakeTransport(
        [completion_response("bad"), TransportResponse(status_code=429, body=None),
         completion_response(GOOD)]
    )
    clock = Clock()
    ladder = make_ladder(make_gateway(transport, clock=clock), ResiliencePolicy(),
                              clock=clock, sleep=lambda s: None)
    with pytest.raises(ProviderRateLimitError):
        ladder.generate(make_request(budget=budget))
    assert len(transport.requests) == 2  # the repair used the second call; no third


def test_m3_returned_turn_counts_failed_attempts_and_repairs() -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=429, body=None), completion_response("bad"),
         completion_response(GOOD)]
    )
    clock = Clock()
    ladder = make_ladder(make_gateway(transport, clock=clock), ResiliencePolicy(),
                              clock=clock, sleep=lambda s: None)
    turn = ladder.generate(make_request())
    assert turn.model_calls == 3 and turn.repairs == 1


# --- M4: circuit breaker ----------------------------------------------------------------
def _open_breaker(clock: Clock, **kw) -> CircuitBreaker:
    breaker = CircuitBreaker(ResiliencePolicy(breaker_failure_threshold=1, breaker_open_seconds=10, **kw), clock)
    breaker.before_call()
    breaker.record_failure()
    clock.advance(10)
    return breaker


@pytest.mark.parametrize("error", [TypeError("x"), RecursionError(), KeyError("k"), MemoryError()])
def test_m4_a_non_gateway_error_releases_the_probe(error: BaseException) -> None:
    clock = Clock()

    class Boom:
        def generate(self, request):
            raise error

    ladder = make_ladder(Boom(), ResiliencePolicy(breaker_failure_threshold=1,
                              breaker_open_seconds=10), clock=clock)
    breaker = ladder.breaker_for(PROVIDER)
    breaker.before_call()
    breaker.record_failure()
    clock.advance(10)
    for _ in range(2):  # the second call proves the probe slot was given back
        with pytest.raises(type(error)):
            ladder.generate(make_request())
    assert breaker.state == BreakerState.HALF_OPEN


def test_m4_local_refusals_neither_close_the_breaker_nor_consume_the_probe() -> None:
    clock = Clock()
    transport = FakeTransport([])
    ladder = make_ladder(make_gateway(transport, clock=clock),
                              ResiliencePolicy(breaker_failure_threshold=1, breaker_open_seconds=10),
                              clock=clock)
    breaker = ladder.breaker_for(PROVIDER)
    breaker.before_call()
    breaker.record_failure()
    clock.advance(10)
    refusals = (
        make_request(privacy_class=PrivacyClass.MAPS_CONTENT),  # TaintedContextError
        make_request(allowed_tools=(evolve(SEARCH_TOOL, name="web_search"),)),  # ToolNotAllowed
    )
    for request in refusals:
        with pytest.raises(Exception):  # noqa: B017
            ladder.generate(request)
        assert breaker.state == BreakerState.HALF_OPEN
    assert breaker.before_call() is True  # the probe is still available
    assert transport.requests == []


def test_m4_a_provider_answer_closes_a_half_open_breaker() -> None:
    clock = Clock()
    transport = FakeTransport([completion_response(GOOD, finish_reason="content_filter")])
    ladder = make_ladder(make_gateway(transport, clock=clock),
                              ResiliencePolicy(breaker_failure_threshold=1, breaker_open_seconds=10),
                              clock=clock)
    breaker = ladder.breaker_for(PROVIDER)
    breaker.before_call()
    breaker.record_failure()
    clock.advance(10)
    with pytest.raises(Exception):  # noqa: B017
        ladder.generate(make_request())
    assert breaker.state == BreakerState.CLOSED


def test_m4_exactly_one_thread_wins_the_half_open_probe() -> None:
    clock = Clock()
    breaker = _open_breaker(clock)
    barrier = threading.Barrier(16)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            breaker.before_call()
            verdict = True
        except CircuitOpenError:
            verdict = False
        with lock:
            outcomes.append(verdict)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(True) == 1 and outcomes.count(False) == 15


def test_m4_concurrent_failures_open_the_breaker_exactly_at_the_threshold() -> None:
    breaker = CircuitBreaker(ResiliencePolicy(breaker_failure_threshold=50), Clock())
    barrier = threading.Barrier(50)

    def worker() -> None:
        barrier.wait()
        breaker.before_call()
        breaker.record_failure()

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert breaker.state == BreakerState.OPEN


# --- M7: tool arguments and response size -----------------------------------------------
STRICT_TOOL = ToolSpec(
    name="search_places", description="d",
    parameters={"type": "object", "required": ["q"], "additionalProperties": False,
                "properties": {"q": {"type": "string", "maxLength": 5},
                               "n": {"type": "integer", "minimum": 1, "maximum": 3}}},
)


def _tool_reply(*entries) -> TransportResponse:
    return completion_response(None, finish_reason="tool_calls", tool_calls=list(entries))


def _run_tools(*entries):
    request = make_request(allowed_tools=(STRICT_TOOL,), allow_repair=False)
    return make_gateway(FakeTransport([_tool_reply(*entries)])).generate(request)


def test_m7_valid_arguments_pass() -> None:
    turn = _run_tools(tool_call_entry("search_places", {"q": "cafe", "n": 2}))
    assert turn.tool_calls[0].arguments == {"q": "cafe", "n": 2}


@pytest.mark.parametrize(
    "arguments",
    [{}, {"q": 1}, {"q": "toolong"}, {"q": "ok", "n": 0}, {"q": "ok", "n": 1.5},
     {"q": "ok", "n": True}, {"q": "ok", "extra": 1}],
)
def test_m7_arguments_violating_the_tool_schema_are_malformed(arguments) -> None:
    with pytest.raises(MalformedOutputError):
        _run_tools(tool_call_entry("search_places", arguments))


def test_m7_duplicate_tool_call_ids_are_rejected() -> None:
    with pytest.raises(MalformedOutputError):
        _run_tools(tool_call_entry("search_places", {"q": "a"}, "c1"),
                   tool_call_entry("search_places", {"q": "b"}, "c1"))


def test_m7_oversized_and_deeply_nested_arguments_are_rejected() -> None:
    big = tool_call_entry("search_places", {"q": "a"})
    big["function"]["arguments"] = json.dumps({"q": "a", "pad": "x" * 9_000})
    deep = tool_call_entry("search_places", {"q": "a"})
    deep["function"]["arguments"] = '{"q": "a", "d": ' + "[" * 30 + "]" * 30 + "}"
    hostile = tool_call_entry("search_places", {"q": "a"})
    hostile["function"]["arguments"] = "[" * 7_000  # recursion bait under the byte cap
    for entry in (big, deep, hostile):
        with pytest.raises(MalformedOutputError):
            _run_tools(entry)


def test_m7_guarded_swallows_recursion_and_any_parser_error() -> None:
    def recurse() -> None:
        raise RecursionError

    assert _guarded(recurse) == (None, False)
    assert _guarded(json.loads, "[" * 200_000)[1] is False


def test_m7_unsupported_schema_keywords_fail_closed_at_toolspec_construction() -> None:
    for parameters in (
        {"type": "object", "properties": {"a": {"$ref": "#/x"}}},
        {"type": "object", "anyOf": [{"type": "string"}]},
        {"type": "object", "properties": {"a": {"type": "string", "pattern": "(a+)+$"}}},
        {"type": "string"},
    ):
        with pytest.raises(ValueError):
            ToolSpec(name="t", description="d", parameters=parameters)


def test_m7_oversized_provider_responses_are_rejected() -> None:
    huge = TransportResponse(status_code=200, body=completion_response(GOOD).body,
                             size_bytes=MAX_RESPONSE_BYTES + 1)
    with pytest.raises(MalformedOutputError) as caught:
        make_gateway(FakeTransport([huge])).generate(make_request(allow_repair=False))
    assert caught.value.usage is not None and caught.value.usage.input_tokens == 1_000


def test_m7_httpx_transport_caps_the_bytes_it_reads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 10))

    transport = HttpxTransport.for_tests(httpx.MockTransport(handler))
    from app.gateway.errors import TransportFailure

    with pytest.raises(TransportFailure):
        transport.send(_request_with(), Credential(SECRET))


# --- M8: output schemas cannot carry state ----------------------------------------------
class WithFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[Fact]


class WithEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nested: dict[str, tuple[Evidence | None, ...]]


class WrapsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: WorkspacePatch


def _schema_request(role: CallRole, model: type[BaseModel]):
    maps = role == CallRole.DISCOVER
    return make_request(
        call_role=role, output_schema=OutputSchema(name="out", model=model),
        privacy_class=PrivacyClass.MAPS_CONTENT if maps else PrivacyClass.USER_PRIVATE,
    )


@pytest.mark.parametrize("role", [CallRole.DISCOVER, CallRole.COMPOSE])
@pytest.mark.parametrize("model", [WorkspacePatch, WithFacts, WithEvidence, WrapsPatch])
def test_m8_state_bearing_schemas_are_refused_for_discover_and_compose(role, model) -> None:
    with pytest.raises(TaintedContextError):
        check_request(_schema_request(role, model), make_policy())


def test_m8_frame_may_use_only_explicitly_allowlisted_patch_schemas() -> None:
    assert WorkspacePatch in FRAME_STATE_SCHEMA_ALLOWLIST
    check_request(_schema_request(CallRole.FRAME, WorkspacePatch), make_policy())
    check_request(_schema_request(CallRole.FRAME, Reply), make_policy())
    for model in (WithFacts, WithEvidence, WrapsPatch):
        with pytest.raises(TaintedContextError):
            check_request(_schema_request(CallRole.FRAME, model), make_policy())

    class SubPatch(WorkspacePatch):  # a subclass is not the allowlisted class
        pass

    with pytest.raises(TaintedContextError):
        check_request(_schema_request(CallRole.FRAME, SubPatch), make_policy())


# --- M6: shared model-id pattern ---------------------------------------------------------
def test_m6_model_id_pattern_is_public_and_unchanged() -> None:
    assert MODEL_ID_PATTERN.pattern == r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$"
    assert MODEL_ID_PATTERN.fullmatch("vendor/pinned-model-1")
    assert not MODEL_ID_PATTERN.fullmatch("vendor/m:online")


# --- L1/L2: provider echo and error text ------------------------------------------------
def test_l2_validation_errors_do_not_echo_prompt_or_provider_text() -> None:
    for build in (
        lambda: ContextMessage(role="user", content="USER-CANARY" + "x" * 200_000,
                               content_class=ContentClass.USER),
        lambda: ContextMessage(role="tool", content="USER-CANARY", content_class=ContentClass.USER),
        lambda: ModelTurn(call_role=CallRole.FRAME, content_class=ContentClass.USER,
                          output=Reply(answer="USER-CANARY", count=1), finish_reason="LENGTH",
                          usage={"input_tokens": 1, "output_tokens": 1}, latency_ms=1,  # type: ignore[arg-type]
                          estimated_cost_usd=0, price_table_version="v",
                          price_is_placeholder=True, model_id=MODEL, provider=PROVIDER,
                          runtime_config_version="v"),
    ):
        with pytest.raises(ValueError) as caught:
            build()
        assert "USER-CANARY" not in str(caught.value) and "input_value" not in str(caught.value)


# --- L3: the real client is locked down --------------------------------------------------
def test_l3_default_client_does_not_follow_redirects_or_read_the_environment() -> None:
    client = HttpxTransport()._client  # noqa: SLF001
    assert client.trust_env is False and client.follow_redirects is False


# --- L5: construction-time validation ---------------------------------------------------
def test_l5_construction_runs_self_check_and_binds_the_config() -> None:
    other = make_config(model_id="vendor/other-model")
    with pytest.raises(GatewayConfigError):  # placeholder for model A admits no model B
        OpenRouterGateway(FakeTransport([]), lambda: SECRET, other, make_policy(),
                          placeholder_price_table(MODEL, PROVIDER))
    transport = FakeTransport([completion_response(GOOD)])
    gateway = make_gateway(transport)
    with pytest.raises(GatewayConfigError):  # a request carrying another config is refused
        gateway.generate(make_request(runtime_config=make_config(version="cfg-2")))
    assert transport.requests == []


# --- L6: quota kill switch ----------------------------------------------------------------
def test_l6_a_402_latches_every_later_request_until_reset() -> None:
    transport = FakeTransport(
        [TransportResponse(status_code=402, body=None), completion_response(GOOD),
         completion_response(GOOD)]
    )
    gateway = make_gateway(transport)
    with pytest.raises(ProviderQuotaError):
        gateway.generate(make_request())
    for _ in range(3):
        with pytest.raises(ProviderQuotaError) as caught:
            gateway.generate(make_request())
        assert caught.value.calls == 0
    assert len(transport.requests) == 1 and gateway.quota_latch.tripped
    gateway.quota_latch.reset()
    assert gateway.generate(make_request()).output == Reply(answer="ok", count=2)


def test_l6_a_latched_quota_is_not_a_retryable_breaker_failure() -> None:
    clock = Clock()
    transport = FakeTransport([TransportResponse(status_code=402, body=None)])
    ladder = make_ladder(make_gateway(transport, clock=clock), ResiliencePolicy(), clock=clock,
                              sleep=lambda s: None)
    with pytest.raises(ProviderQuotaError):
        ladder.generate(make_request())
    assert len(transport.requests) == 1


# --- contracts ---------------------------------------------------------------------------
def test_contract_runtime_config_carries_the_audited_privacy_settings() -> None:
    assert (CFG.zdr, CFG.data_collection, CFG.allow_fallbacks) == (True, "deny", False)
    assert CFG.provider in CFG.provider_allowlist and CFG.price_table_version == "placeholder-0"
    for bad in (
        {"provider_allowlist": ("other",)},  # must contain provider
        {"provider_allowlist": (PROVIDER, PROVIDER)},  # duplicates
        {"provider_allowlist": ("UPPER",)},
        {"provider_allowlist": ()},
        {"price_table_version": ""},
    ):
        with pytest.raises(ValueError):
            make_config(**bad)


def test_contract_planner_ceilings_live_in_token_ceilings() -> None:
    ceilings = TokenCeilings()
    assert (ceilings.planner_input_tokens, ceilings.planner_output_tokens) == (12_000, 1_200)
    budget = RunBudget.from_ceilings(TokenCeilings(planner_input_tokens=5_000, planner_output_tokens=800))
    assert (budget.per_call_max_input_tokens, budget.per_call_max_output_tokens) == (5_000, 800)


# --- L8 ---------------------------------------------------------------------------------
def test_l8_ruff_bans_print_and_app_has_none() -> None:
    pyproject = (APP_DIR.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'extend-select = ["T20"]' in pyproject
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in APP_DIR.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
    ]
    assert offenders == []


def test_provider_unavailable_after_oversized_http_body_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    gateway = OpenRouterGateway(HttpxTransport.for_tests(httpx.MockTransport(handler)), lambda: SECRET, CFG, make_policy(),
                                placeholder_price_table(MODEL, PROVIDER), clock=Clock())
    with pytest.raises(ProviderUnavailableError):
        gateway.generate(make_request())
