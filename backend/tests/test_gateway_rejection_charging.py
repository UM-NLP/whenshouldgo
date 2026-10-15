"""A request rejected before processing (429 or 503, no usage) costs zero tokens and zero
dollars but still counts as a call. Worst case is charged only when a call may have been
processed without usage coming back."""
from __future__ import annotations

from decimal import Decimal

import pytest
from gateway_fakes import FakeTransport, completion_response
from test_gateway_support import (
    GOOD,
    MODEL,
    PROVIDER,
    Clock,
    make_config,
    make_gateway,
    make_ladder,
    make_request,
)

from app.domain import evolve
from app.gateway import (
    GatewayError,
    ModelPrice,
    PriceTable,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ResiliencePolicy,
    RunBudget,
    TransportResponse,
)
from app.gateway.errors import TransportFailure, TransportTimeout

CFG = make_config(price_table_version="prices-rc")
TABLE = PriceTable(
    version="prices-rc", placeholder=False,
    model_prices=(ModelPrice(model_id=MODEL, provider=PROVIDER,
                             input_usd_per_million=Decimal("1.00"),
                             output_usd_per_million=Decimal("2.00")),),
)


def status(code: int) -> TransportResponse:
    return TransportResponse(status_code=code, body={"error": {"code": code}})


def ladder(script, clock=None):
    clock = clock or Clock()
    gateway = make_gateway(FakeTransport(script), clock=clock, config=CFG, prices=TABLE)
    from app.gateway import ResilientGateway

    return ResilientGateway(gateway, ResiliencePolicy(), price_table=TABLE, clock=clock,
                            sleep=lambda s: None)


@pytest.mark.parametrize("rejection", [status(429), TransportResponse(status_code=429, body=None)])
def test_a_429_then_success_charges_only_the_successful_call(rejection) -> None:
    turn = ladder([rejection, completion_response(GOOD)]).generate(make_request(runtime_config=CFG))
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (100, 20)
    assert turn.estimated_cost_usd == Decimal("0.00014000")
    assert turn.model_calls == 2  # the rejected call still counts toward the call budget


# Decision (M8): only an HTTP 429 is free. OpenRouter's error page (retrieved 2026-10-15) defines
# 503 as "no available model provider that meets your routing requirements" with a typed
# provider_overloaded code, and says nothing about whether such a request is billed. An
# overloaded upstream may have started work, so a bare 503 is charged at the worst case. An
# error object inside a 200 body is never free: a hostile or buggy provider could otherwise
# make any call free by writing {"error": {"code": 429}} after processing it.
@pytest.mark.parametrize("ambiguous", [
    status(503), TransportResponse(status_code=503, body=None),
    TransportResponse(status_code=200, body={"error": {"code": 429}}),
    TransportResponse(status_code=200, body={"error": {"code": 503}}),
    TransportTimeout(), TransportFailure(), status(504)])
def test_a_call_that_may_have_been_processed_is_charged_at_the_worst_case(ambiguous) -> None:
    turn = ladder([ambiguous, completion_response(GOOD)]).generate(make_request(runtime_config=CFG))
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (1_100, 620)
    assert turn.model_calls == 2


def test_only_the_real_http_status_sets_the_free_flag() -> None:
    for body_code in (429, 503):
        with pytest.raises(GatewayError) as caught:
            make_gateway(FakeTransport([TransportResponse(status_code=200, body={"error": {"code": body_code}})]),
                         config=CFG, prices=TABLE).generate(make_request(runtime_config=CFG))
        assert caught.value.rejected_before_processing is False
        assert caught.value.usage is not None and caught.value.usage.input_tokens == 1_000
    with pytest.raises(ProviderUnavailableError) as caught:
        make_gateway(FakeTransport([status(503)]), config=CFG, prices=TABLE).generate(
            make_request(runtime_config=CFG))
    assert caught.value.rejected_before_processing is False and caught.value.usage is not None


def test_a_200_without_usage_is_charged_at_the_worst_case_on_the_error() -> None:
    from app.gateway import UsageMismatchError

    no_usage = completion_response(GOOD, prompt_tokens=None)
    with pytest.raises(UsageMismatchError) as caught:
        make_gateway(FakeTransport([no_usage]), config=CFG, prices=TABLE).generate(
            make_request(runtime_config=CFG, allow_repair=False))
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (1_000, 600)  # type: ignore[union-attr]


def test_a_429_reports_no_usage_but_one_call() -> None:
    with pytest.raises(ProviderRateLimitError) as caught:
        make_gateway(FakeTransport([status(429)]), config=CFG, prices=TABLE).generate(
            make_request(runtime_config=CFG))
    assert caught.value.usage is None and caught.value.calls == 1
    assert caught.value.rejected_before_processing is True


def test_a_rejected_call_does_not_eat_the_token_budget_needed_for_the_retry() -> None:
    # Only 1,150 input tokens remain. Charging the 429 at the worst case (1,000) would leave
    # 150 and block the retry; a pre-processing rejection must leave the tokens untouched.
    budget = evolve(RunBudget.from_ceilings(CFG.token_ceilings), input_tokens_remaining=1_150)
    turn = ladder([status(429), completion_response(GOOD)]).generate(
        make_request(runtime_config=CFG, budget=budget))
    assert turn.model_calls == 2


def test_a_rejection_after_a_reported_attempt_adds_only_the_reported_usage() -> None:
    # malformed (100/20 reported), then the repair is rejected with 429: total is 100/20.
    with pytest.raises(ProviderRateLimitError) as caught:
        make_gateway(FakeTransport([completion_response("bad"), status(429)]),
                     config=CFG, prices=TABLE).generate(make_request(runtime_config=CFG))
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (100, 20)  # type: ignore[union-attr]
    assert caught.value.calls == 2


def test_rejected_calls_still_exhaust_the_call_budget() -> None:
    budget = RunBudget.from_ceilings(CFG.token_ceilings).consume(calls=4)  # two calls left
    transport = FakeTransport([status(429), status(429), completion_response(GOOD)])
    gateway = make_gateway(transport, config=CFG, prices=TABLE, clock=Clock())
    from app.gateway import ResilientGateway

    clock = Clock()
    guarded = ResilientGateway(gateway, ResiliencePolicy(), price_table=TABLE, clock=clock,
                               sleep=lambda s: None)
    with pytest.raises(ProviderRateLimitError):
        guarded.generate(make_request(runtime_config=CFG, budget=budget))
    assert len(transport.requests) == 2
