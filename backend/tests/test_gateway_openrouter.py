"""OpenRouter adapter: fail-closed config, request body, output validation, budgets."""
from __future__ import annotations

from decimal import Decimal

import pytest
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
    tool_message,
)

from app.domain import ContentClass, WorkspacePatch, evolve
from app.gateway import (
    BudgetExceededError,
    CallRole,
    ContentFilterError,
    ContextMessage,
    FinishReason,
    GatewayConfigError,
    GatewayError,
    GatewayPolicy,
    MalformedOutputError,
    ModelPrice,
    OutputSchema,
    PriceTable,
    PrivacyClass,
    PrivacyViolationError,
    ProviderRejectedError,
    ProviderUnavailableError,
    RunBudget,
    TaintedContextError,
    ToolNotAllowedError,
    TruncationError,
    Usage,
    UsageMismatchError,
    is_pinned_model_id,
    placeholder_price_table,
    self_check,
)
from gateway_fakes import (
    FakeGateway,
    FakeTransport,
    SyntheticMapsAnswer,
    completion_response,
    synthetic_maps_response,
    synthetic_maps_turn,
    tool_call_entry,
)
from app.gateway.openrouter import REPAIR_INSTRUCTION

CFG = make_config()


def run(script, request=None, **gateway_kwargs):
    transport = FakeTransport(script)
    gateway = make_gateway(transport, **gateway_kwargs)
    return transport, gateway, gateway.generate(request or make_request())


# --- the request body -------------------------------------------------------------------
def test_body_carries_exact_routing_parameters_on_every_request() -> None:
    transport, _, turn = run([completion_response(GOOD)])
    body = transport.requests[0].body
    assert body["provider"] == {
        "order": [PROVIDER],
        "only": [PROVIDER],
        "allow_fallbacks": False,
        "data_collection": "deny",
        "zdr": True,
        "require_parameters": True,
    }
    assert body["model"] == MODEL
    assert set(body) == {
        "model", "messages", "max_tokens", "temperature", "stream", "response_format", "provider",
    }
    assert body["max_tokens"] == 600 and body["stream"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert turn.output == Reply(answer="ok", count=2)


def test_repair_request_also_carries_privacy_parameters() -> None:
    transport, _, _ = run([completion_response("not json"), completion_response(GOOD)])
    assert len(transport.requests) == 2
    for sent in transport.requests:
        assert sent.body["provider"]["zdr"] is True
        assert sent.body["provider"]["allow_fallbacks"] is False
    assert transport.requests[1].body["messages"][-1]["content"] == REPAIR_INSTRUCTION


def test_tools_are_function_tools_only_and_no_plugins_are_sent() -> None:
    request = make_request(call_role=CallRole.DISCOVER, allowed_tools=(SEARCH_TOOL,),
                           privacy_class=PrivacyClass.MAPS_CONTENT)
    transport, _, _ = run([completion_response(GOOD)], request)
    body = transport.requests[0].body
    assert [t["type"] for t in body["tools"]] == ["function"]
    assert not {"plugins", "models", "route", "transforms"} & set(body)


def _weak_gateway(transport, mutate):
    base = type(make_gateway(FakeTransport([])))

    class Weak(base):  # type: ignore[misc, valid-type]
        def _build_body(self, request, repair):
            body = super()._build_body(request, repair)
            mutate(body)
            return body

    return Weak(transport, lambda: SECRET, make_config(), make_policy(),
                placeholder_price_table(MODEL, PROVIDER), clock=Clock())


def test_request_without_privacy_parameters_is_rejected_before_the_transport() -> None:
    transport = FakeTransport([completion_response(GOOD)])
    weak = _weak_gateway(transport, lambda body: body["provider"].pop("zdr"))
    with pytest.raises(PrivacyViolationError):
        weak.generate(make_request())
    assert transport.requests == []


@pytest.mark.parametrize("drop", ["provider", "fallback", "deny", "plugins", "models"])
def test_body_guard_rejects_each_weakening(drop: str) -> None:
    def mutate(body):
        if drop == "provider":
            del body["provider"]
        elif drop == "fallback":
            body["provider"]["allow_fallbacks"] = True
        elif drop == "deny":
            body["provider"]["data_collection"] = "allow"
        elif drop == "plugins":
            body["plugins"] = [{"id": "web"}]
        else:
            body["models"] = ["other/model"]

    transport = FakeTransport([completion_response(GOOD)])
    with pytest.raises(PrivacyViolationError):
        _weak_gateway(transport, mutate).generate(make_request())
    assert transport.requests == []


def test_authorization_never_reaches_the_transport_request_or_its_repr() -> None:
    transport, _, turn = run([completion_response(GOOD)])
    sent = transport.requests[0]
    assert all(name.lower() != "authorization" for name in sent.headers)
    assert SECRET not in repr(sent) and SECRET not in str(sent.model_dump())
    assert SECRET not in repr(turn) and "USER-CANARY" not in repr(turn)
    assert transport.credential_types == ["Credential"]


# --- fail closed ------------------------------------------------------------------------
def test_startup_self_check_accepts_a_sound_config() -> None:
    self_check(CFG, placeholder_price_table(MODEL, PROVIDER))


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider_allowlist": ("other",)},
        {"provider_allowlist": ()},
        {"zdr": False},
        {"data_collection": "allow"},
        {"allow_fallbacks": True},
        {"provider": "  "},
        {"provider": "HostOne"},
    ],
)
def test_runtime_config_cannot_represent_a_weakened_privacy_setting(overrides) -> None:
    with pytest.raises(ValueError):
        make_config(**overrides)


@pytest.mark.parametrize(
    "model_id",
    ["auto", "openrouter/auto", "openrouter/anything", "vendor/*", "vendor/m:online",
     "vendor/m:free", "Vendor/Model", "justaname", "vendor/model/extra", " vendor/m"],
)
def test_unpinned_model_ids_are_refused_at_construction(model_id: str) -> None:
    assert not is_pinned_model_id(model_id)
    config = make_config(model_id=model_id)
    with pytest.raises(GatewayConfigError):
        make_gateway(FakeTransport([]), config=config,
                     prices=placeholder_price_table(model_id, PROVIDER))


def test_missing_price_entry_version_mismatch_and_placeholder_in_production_are_refused() -> None:
    with pytest.raises(GatewayConfigError):
        self_check(CFG, placeholder_price_table("vendor/other", PROVIDER))
    with pytest.raises(GatewayConfigError):
        self_check(CFG, placeholder_price_table(MODEL, PROVIDER, version="other-version"))
    with pytest.raises(GatewayConfigError):
        self_check(CFG, placeholder_price_table(MODEL, PROVIDER), production=True)
    with pytest.raises(GatewayConfigError):
        make_gateway(FakeTransport([]), production=True)  # placeholder table in production


def test_missing_api_key_fails_before_sending() -> None:
    transport = FakeTransport([completion_response(GOOD)])
    gateway = make_gateway(transport, secret="  ")
    with pytest.raises(GatewayConfigError):
        gateway.generate(make_request())
    assert transport.requests == []


def test_tool_outside_the_server_allowlist_is_refused_without_sending() -> None:
    other = evolve(SEARCH_TOOL, name="web_search")
    transport = FakeTransport([completion_response(GOOD)])
    gateway = make_gateway(transport)
    with pytest.raises(ToolNotAllowedError):
        gateway.generate(make_request(allowed_tools=(other,)))
    assert transport.requests == []


def test_model_requested_tool_outside_the_request_allowlist_is_refused() -> None:
    reply = completion_response(
        None, finish_reason="tool_calls", tool_calls=[tool_call_entry("compute_routes", {})]
    )
    with pytest.raises(ToolNotAllowedError) as caught:
        run([reply], make_request(allowed_tools=(SEARCH_TOOL,)))
    assert caught.value.usage is not None


def test_model_tool_calls_are_returned_when_allowed() -> None:
    reply = completion_response(
        None, finish_reason="tool_calls",
        tool_calls=[tool_call_entry("search_places", {"q": "x"})],
    )
    _, _, turn = run([reply], make_request(allowed_tools=(SEARCH_TOOL,)))
    assert turn.finish_reason == FinishReason.TOOL_CALLS
    assert [call.name for call in turn.tool_calls] == ["search_places"]
    assert turn.output is None


def test_too_many_tool_calls_for_the_remaining_budget_is_refused() -> None:
    budget = RunBudget.from_ceilings(CFG.token_ceilings).consume(tool_calls=11)
    reply = completion_response(
        None, finish_reason="tool_calls",
        tool_calls=[tool_call_entry("search_places", {}, "c1"),
                    tool_call_entry("search_places", {}, "c2")],
    )
    with pytest.raises(BudgetExceededError):
        run([reply], make_request(allowed_tools=(SEARCH_TOOL,), budget=budget))


@pytest.mark.parametrize(
    "budget_change",
    [{"calls": 6}, {"seconds": 20.0}, {"usage": "input"}, {"usage": "output"}],
)
def test_exhausted_budget_is_refused_without_sending(budget_change) -> None:
    budget = RunBudget.from_ceilings(CFG.token_ceilings)
    if budget_change.get("usage") == "input":
        budget = budget.consume(usage=Usage(input_tokens=39_500, output_tokens=0))
    elif budget_change.get("usage") == "output":
        budget = budget.consume(usage=Usage(input_tokens=0, output_tokens=3_700))
    else:
        budget = budget.consume(**budget_change)
    transport = FakeTransport([completion_response(GOOD)])
    with pytest.raises(BudgetExceededError):
        make_gateway(transport).generate(make_request(budget=budget))
    assert transport.requests == []


def test_per_call_ceilings_and_escalation_budget() -> None:
    transport = FakeTransport([completion_response(GOOD)])
    gateway = make_gateway(transport)
    with pytest.raises(BudgetExceededError):
        gateway.generate(make_request(estimated_input_tokens=12_001))
    with pytest.raises(BudgetExceededError):
        gateway.generate(make_request(max_output_tokens=1_201))
    spent = RunBudget.from_ceilings(CFG.token_ceilings).consume(escalation=True)
    with pytest.raises(BudgetExceededError):
        gateway.generate(make_request(escalation=True, budget=spent))
    assert transport.requests == []


def test_a_caller_budget_cannot_loosen_the_audited_per_call_ceilings() -> None:
    loose = evolve(RunBudget.from_ceilings(CFG.token_ceilings), per_call_max_input_tokens=50_000)
    transport = FakeTransport([completion_response(GOOD)])
    with pytest.raises(BudgetExceededError):
        make_gateway(transport).generate(make_request(budget=loose, estimated_input_tokens=20_000))
    assert transport.requests == []


def test_response_arriving_after_the_deadline_is_discarded_and_charged() -> None:
    clock = Clock()

    class Slow(FakeTransport):
        def send(self, request, credential):
            clock.advance(21)
            return super().send(request, credential)

    transport = Slow([completion_response(GOOD)])
    with pytest.raises(BudgetExceededError) as caught:
        make_gateway(transport, clock=clock).generate(make_request())
    assert caught.value.usage is not None and caught.value.usage.input_tokens == 100


def test_transport_timeout_is_bounded_by_the_remaining_deadline() -> None:
    budget = RunBudget.from_ceilings(CFG.token_ceilings).consume(seconds=17.5)
    transport, _, _ = run([completion_response(GOOD)], make_request(budget=budget))
    assert transport.requests[0].timeout_seconds == pytest.approx(2.5)


# --- Maps taint (call A versus call B) --------------------------------------------------
@pytest.mark.parametrize(
    "tainted", [ContentClass.MAPS_GROUNDED, ContentClass.MAPS_PLACES, ContentClass.MAPS_ROUTE]
)
def test_call_a_frame_rejects_maps_tainted_context(tainted: ContentClass) -> None:
    context = (
        ContextMessage(role="user", content="USER-CANARY", content_class=ContentClass.USER),
        tool_message("MAPS-CANARY", tainted),
    )
    transport = FakeTransport([completion_response(GOOD)])
    with pytest.raises(TaintedContextError):
        make_gateway(transport).generate(make_request(context=context))
    assert transport.requests == []
    # The fake enforces the same rule, so tests cannot pass with a laxer double.
    fake = FakeGateway([synthetic_maps_turn(CFG)], make_policy())
    with pytest.raises(TaintedContextError):
        fake.generate(make_request(context=context))
    assert fake.requests == []


def test_call_a_frame_rejects_the_maps_privacy_class_and_compose_too() -> None:
    gateway = make_gateway(FakeTransport([]))
    with pytest.raises(TaintedContextError):
        gateway.generate(make_request(privacy_class=PrivacyClass.MAPS_CONTENT))
    with pytest.raises(TaintedContextError):
        gateway.generate(make_request(call_role=CallRole.COMPOSE, context=(tool_message("x"),)))


def test_direct_places_and_routes_are_never_model_visible_even_for_discover() -> None:
    context = (tool_message("x", ContentClass.MAPS_PLACES),)
    with pytest.raises(TaintedContextError):
        make_gateway(FakeTransport([])).generate(
            make_request(call_role=CallRole.DISCOVER, context=context,
                         privacy_class=PrivacyClass.MAPS_CONTENT)
        )


def test_discover_with_maps_context_needs_the_maps_privacy_class() -> None:
    with pytest.raises(PrivacyViolationError):
        make_gateway(FakeTransport([])).generate(
            make_request(call_role=CallRole.DISCOVER, context=(tool_message("x"),))
        )


def test_discover_output_is_maps_grounded_and_never_a_patch() -> None:
    context = (
        ContextMessage(role="user", content="USER-CANARY", content_class=ContentClass.USER),
        tool_message("x"),
    )
    request = make_request(
        call_role=CallRole.DISCOVER, context=context, privacy_class=PrivacyClass.MAPS_CONTENT,
        output_schema=OutputSchema(name="grounded", model=SyntheticMapsAnswer),
    )
    _, _, turn = run([synthetic_maps_response()], request)
    assert turn.content_class == ContentClass.MAPS_GROUNDED
    assert turn.output.places[0].place_id.startswith("SYNTH_PLACE_")  # type: ignore[union-attr]
    assert "Synthetic fixture" not in repr(turn)  # content stays out of repr and logs
    patch_request = evolve(request, output_schema=OutputSchema(name="patch", model=WorkspacePatch))
    with pytest.raises(TaintedContextError):
        make_gateway(FakeTransport([])).generate(patch_request)


def test_frame_output_is_not_maps_content() -> None:
    _, _, turn = run([completion_response(GOOD)])
    assert turn.content_class == ContentClass.USER


# --- finish reasons and output validation -----------------------------------------------
def test_each_finish_reason_is_normalised_or_rejected() -> None:
    _, _, turn = run([completion_response(GOOD, finish_reason="stop")])
    assert turn.finish_reason == FinishReason.STOP
    cases = [
        ("length", TruncationError),
        ("content_filter", ContentFilterError),
        ("error", ProviderUnavailableError),
        ("weird_new_reason", ProviderRejectedError),
        (None, ProviderRejectedError),
    ]
    for reason, error in cases:
        with pytest.raises(error):
            run([completion_response(GOOD, finish_reason=reason)], make_request(allow_repair=False))


def test_truncated_output_is_never_valid_even_if_it_parses() -> None:
    with pytest.raises(TruncationError) as caught:
        run([completion_response(GOOD, finish_reason="length")], make_request(allow_repair=False))
    assert caught.value.usage is not None and caught.value.usage.output_tokens == 20


def test_truncated_tool_call_is_rejected() -> None:
    reply = completion_response(
        None, finish_reason="length", tool_calls=[tool_call_entry("search_places", {})]
    )
    with pytest.raises(TruncationError):
        run([reply], make_request(allowed_tools=(SEARCH_TOOL,), allow_repair=False))


@pytest.mark.parametrize(
    "content",
    ['{"answer": "ok"', "not json", '{"answer": "ok", "count": "x"}',
     '{"answer": "ok", "count": 1, "extra": true}', "```json\n" + GOOD + "\n```", "", None],
)
def test_malformed_or_schema_violating_output_is_rejected(content) -> None:
    with pytest.raises(MalformedOutputError):
        run([completion_response(content)], make_request(allow_repair=False))


def test_malformed_tool_arguments_are_rejected() -> None:
    entry = tool_call_entry("search_places", {})
    entry["function"]["arguments"] = "{not json"
    with pytest.raises(MalformedOutputError):
        run([completion_response(None, finish_reason="tool_calls", tool_calls=[entry])],
            make_request(allowed_tools=(SEARCH_TOOL,), allow_repair=False))


def test_one_bounded_repair_succeeds_and_charges_both_calls() -> None:
    transport, _, turn = run([completion_response("not json"), completion_response(GOOD)])
    assert turn.repairs == 1 and turn.model_calls == 2
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (200, 40)
    assert len(transport.requests) == 2


def test_repair_is_attempted_at_most_once() -> None:
    transport = FakeTransport(
        [completion_response("bad"), completion_response("worse"), completion_response(GOOD)]
    )
    with pytest.raises(MalformedOutputError) as caught:
        make_gateway(transport).generate(make_request())
    assert len(transport.requests) == 2
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (200, 40)  # type: ignore[union-attr]
    assert caught.value.calls == 2


def test_repair_is_skipped_when_the_remaining_budget_cannot_cover_it() -> None:
    # After the first call's 100 input tokens only 1,000 remain; a repair needs 1,064.
    budget = RunBudget.from_ceilings(CFG.token_ceilings).consume(
        usage=Usage(input_tokens=38_900, output_tokens=0)
    )
    transport = FakeTransport([completion_response("bad"), completion_response(GOOD)])
    with pytest.raises(MalformedOutputError):
        make_gateway(transport).generate(make_request(budget=budget))
    assert len(transport.requests) == 1


def test_repair_is_skipped_when_the_deadline_cannot_cover_it() -> None:
    clock = Clock()

    class Slow(FakeTransport):
        def send(self, request, credential):
            clock.advance(25)
            return super().send(request, credential)

    transport = Slow([completion_response("bad"), completion_response(GOOD)])
    with pytest.raises(GatewayError):
        make_gateway(transport, clock=clock).generate(make_request())
    assert len(transport.requests) == 1


def test_repair_can_be_disabled_by_the_caller() -> None:
    transport = FakeTransport([completion_response("bad"), completion_response(GOOD)])
    with pytest.raises(MalformedOutputError):
        make_gateway(transport).generate(make_request(allow_repair=False))
    assert len(transport.requests) == 1


def test_content_filter_is_not_repaired() -> None:
    transport = FakeTransport([completion_response(GOOD, finish_reason="content_filter")])
    with pytest.raises(ContentFilterError):
        make_gateway(transport).generate(make_request())
    assert len(transport.requests) == 1


# --- usage metadata ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "response",
    [
        completion_response(GOOD, prompt_tokens=None),  # no usage object at all
        completion_response(GOOD, usage={"prompt_tokens": 10}),
        completion_response(GOOD, usage={"prompt_tokens": "10", "completion_tokens": 5}),
        completion_response(GOOD, usage={"prompt_tokens": -1, "completion_tokens": 5}),
        completion_response(GOOD, usage={"prompt_tokens": True, "completion_tokens": 5}),
        completion_response(GOOD, total_tokens=999),  # total disagrees with the parts
        completion_response(GOOD, prompt_tokens=0),
        completion_response(GOOD, completion_tokens=601),  # more than max_tokens allows
    ],
)
def test_usage_mismatch_is_typed_and_charged_at_the_worst_case(response) -> None:
    with pytest.raises(UsageMismatchError) as caught:
        run([response], make_request(allow_repair=False))
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (1_000, 600)  # type: ignore[union-attr]


def test_reported_usage_above_the_remaining_budget_is_discarded_and_charged() -> None:
    with pytest.raises(BudgetExceededError) as caught:
        run([completion_response(GOOD, prompt_tokens=12_500)])
    assert caught.value.usage.input_tokens == 12_500  # type: ignore[union-attr]


def test_response_from_an_unlisted_provider_is_rejected() -> None:
    with pytest.raises(PrivacyViolationError):
        run([completion_response(GOOD, provider="SomeOtherHost")])
    _, _, turn = run([completion_response(GOOD, provider="HostOne")])
    assert turn.provider == PROVIDER


def test_response_provider_field_is_mandatory_in_production_only() -> None:
    table = PriceTable(
        version="placeholder-0", placeholder=False,
        model_prices=(ModelPrice(model_id=MODEL, provider=PROVIDER,
                                 input_usd_per_million=Decimal(1),
                                 output_usd_per_million=Decimal(1)),),
    )
    run([completion_response(GOOD)])  # tolerated outside production (format unverified)
    with pytest.raises(PrivacyViolationError):
        run([completion_response(GOOD)], prices=table, production=True)
    _, _, turn = run([completion_response(GOOD, provider="hostone")], prices=table,
                     production=True)
    assert turn.provider == PROVIDER


# --- price table and telemetry ----------------------------------------------------------
def test_cost_uses_the_versioned_price_table() -> None:
    table = PriceTable(
        version="2026-10-test",
        placeholder=False,
        model_prices=(
            ModelPrice(model_id=MODEL, provider=PROVIDER,
                       input_usd_per_million=Decimal("1.00"), output_usd_per_million=Decimal("2.00")),
        ),
    )
    priced = make_config(price_table_version="2026-10-test")
    _, _, turn = run(
        [completion_response(GOOD)], make_request(runtime_config=priced), prices=table, config=priced
    )
    assert turn.estimated_cost_usd == Decimal("0.00014000")
    assert turn.price_table_version == "2026-10-test" and turn.price_is_placeholder is False
    _, _, placeholder = run([completion_response(GOOD)])
    assert placeholder.estimated_cost_usd == 0
    assert placeholder.price_table_version == "placeholder-0" and placeholder.price_is_placeholder


def test_price_table_rejects_duplicates_and_unknown_models() -> None:
    price = ModelPrice(model_id=MODEL, provider=PROVIDER, input_usd_per_million=Decimal(1),
                       output_usd_per_million=Decimal(1))
    with pytest.raises(ValueError):
        PriceTable(version="v", placeholder=False, model_prices=(price, price))
    with pytest.raises(GatewayConfigError):
        PriceTable(version="v", placeholder=False, model_prices=(price,)).lookup("a/b", PROVIDER)
    assert {u.name for u in placeholder_price_table(MODEL, PROVIDER).unit_prices} == {
        "grounding_lite", "places", "routes",
    }


def test_telemetry_is_typed_metadata_without_content() -> None:
    _, _, turn = run([completion_response(GOOD)])
    meta = turn.telemetry
    assert meta.model_id == MODEL and meta.provider == PROVIDER
    assert meta.runtime_config_version == "cfg-1" and meta.input_tokens == 100
    assert meta.finish_reason == FinishReason.STOP
    dumped = repr(meta.model_dump())
    assert "USER-CANARY" not in dumped and "ok" not in meta.model_dump().values()
    assert set(type(meta).model_fields) >= {"latency_ms", "estimated_cost_usd", "repairs"}


def test_a_truncated_or_failed_turn_cannot_be_constructed() -> None:
    _, _, good = run([completion_response(GOOD)])
    for reason in (FinishReason.LENGTH, FinishReason.CONTENT_FILTER, FinishReason.ERROR):
        with pytest.raises(ValueError):
            evolve(good, finish_reason=reason)
    with pytest.raises(ValueError):
        evolve(good, output=None)


def test_gateway_policy_holds_only_gateway_local_settings() -> None:
    policy = GatewayPolicy()
    assert set(type(policy).model_fields) == {
        "tool_allowlist", "request_timeout_cap_seconds", "repair_instruction_tokens",
        "max_repairs", "resilience",
    }
    assert policy.max_repairs == 1
