"""Failure ladder, circuit breaker, error taxonomy, credentials and the real HTTP transport."""
from __future__ import annotations

import json
import pickle

import httpx
import pytest
from test_gateway_support import (
    GOOD,
    MODEL,
    PROVIDER,
    SECRET,
    Clock,
    make_config,
    make_gateway,
    make_ladder,
    make_policy,
    make_request,
)

from app.domain import ErrorCode
from app.gateway import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    Credential,
    GatewayErrorKind,
    HttpxTransport,
    OpenRouterGateway,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ResiliencePolicy,
    ResilientGateway,
    RunBudget,
    TransportRequest,
    TransportResponse,
    placeholder_price_table,
)
from app.gateway import errors as gateway_errors
from gateway_fakes import FakeTransport, completion_response
from app.gateway.errors import TransportFailure, TransportTimeout

CFG = make_config()


def status(code: int) -> TransportResponse:
    return TransportResponse(status_code=code, body={"error": {"code": code}})


def ladder(script, *, policy: ResiliencePolicy | None = None, clock: Clock | None = None):
    clock = clock or Clock()
    transport = FakeTransport(script)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    inner = make_gateway(transport, clock=clock)
    return transport, sleeps, make_ladder(inner, policy or ResiliencePolicy(), clock=clock, sleep=sleep)


# --- taxonomy ---------------------------------------------------------------------------
def test_errors_map_onto_existing_error_codes() -> None:
    expected = {
        gateway_errors.GatewayConfigError: ErrorCode.PROVIDER,
        gateway_errors.PrivacyViolationError: ErrorCode.POLICY,
        gateway_errors.ToolNotAllowedError: ErrorCode.POLICY,
        gateway_errors.TaintedContextError: ErrorCode.POLICY,
        gateway_errors.BudgetExceededError: ErrorCode.QUOTA,
        gateway_errors.ProviderRateLimitError: ErrorCode.PROVIDER,
        gateway_errors.ProviderTimeoutError: ErrorCode.PROVIDER,
        gateway_errors.TruncationError: ErrorCode.VALIDATION,
        gateway_errors.MalformedOutputError: ErrorCode.VALIDATION,
        gateway_errors.UsageMismatchError: ErrorCode.PROVIDER,
    }
    for cls, code in expected.items():
        assert cls().code == code
    retryable = {c for c in vars(gateway_errors).values()
                 if isinstance(c, type) and issubclass(c, gateway_errors.GatewayError) and c().retryable}
    assert retryable == {ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError}
    assert len({c.kind for c in expected}) == len(expected)
    assert GatewayErrorKind.CIRCUIT_OPEN == CircuitOpenError().kind


@pytest.mark.parametrize(
    ("code", "error"),
    [(429, ProviderRateLimitError), (402, ProviderQuotaError), (504, ProviderTimeoutError),
     (503, ProviderUnavailableError), (401, ProviderRejectedError), (400, ProviderRejectedError)],
)
def test_http_status_and_error_bodies_map_to_typed_errors(code, error) -> None:
    for response in (status(code), TransportResponse(status_code=code, body=None)):
        with pytest.raises(error):
            make_gateway(FakeTransport([response])).generate(make_request())
    embedded = TransportResponse(status_code=200, body={"error": {"code": code, "message": "SECRET-TEXT"}})
    with pytest.raises(error) as caught:
        make_gateway(FakeTransport([embedded])).generate(make_request())
    assert "SECRET-TEXT" not in str(caught.value)


def test_transport_exceptions_become_typed_errors_without_chaining_text() -> None:
    with pytest.raises(ProviderTimeoutError):
        make_gateway(FakeTransport([TransportTimeout()])).generate(make_request())
    with pytest.raises(ProviderUnavailableError):
        make_gateway(FakeTransport([TransportFailure()])).generate(make_request())


# --- ladder -----------------------------------------------------------------------------
def test_retryable_failures_are_retried_with_bounded_backoff() -> None:
    transport, sleeps, gateway = ladder([status(429), status(503), completion_response(GOOD)])
    turn = gateway.generate(make_request())
    assert sleeps == [0.25, 0.5]
    assert turn.model_calls == 3 and len(transport.requests) == 3
    # No fallback: every attempt used the same pinned model and provider block.
    assert {(r.body["model"], tuple(r.body["provider"]["order"])) for r in transport.requests} == {
        (MODEL, (PROVIDER,))
    }


def test_backoff_is_capped_and_attempts_are_bounded() -> None:
    policy = ResiliencePolicy(max_attempts=4, backoff_base_seconds=1.0, backoff_cap_seconds=1.5)
    transport, sleeps, gateway = ladder([status(429)] * 4, policy=policy)
    with pytest.raises(ProviderRateLimitError):
        gateway.generate(make_request())
    assert sleeps == [1.0, 1.5, 1.5] and len(transport.requests) == 4


def test_non_retryable_errors_are_not_retried() -> None:
    for script in ([status(402)], [completion_response(GOOD, finish_reason="content_filter")],
                   [completion_response("bad")]):
        transport, sleeps, gateway = ladder(script)
        with pytest.raises(gateway_errors.GatewayError):
            gateway.generate(make_request(allow_repair=False))
        assert len(transport.requests) == 1 and sleeps == []


def test_retry_never_extends_the_call_or_deadline_budget() -> None:
    budget = RunBudget.from_ceilings(CFG.token_ceilings).consume(calls=5)  # one call left
    transport, sleeps, gateway = ladder([status(429), completion_response(GOOD)])
    with pytest.raises(ProviderRateLimitError):
        gateway.generate(make_request(budget=budget))
    assert len(transport.requests) == 1 and sleeps == []

    tight = RunBudget.from_ceilings(CFG.token_ceilings).consume(seconds=19.9)  # 0.1 s left
    transport, sleeps, gateway = ladder([status(429), completion_response(GOOD)])
    with pytest.raises(ProviderRateLimitError):
        gateway.generate(make_request(budget=tight))
    assert sleeps == []


def test_retry_decrements_the_budget_it_passes_down() -> None:
    transport, _, gateway = ladder([status(429), completion_response(GOOD)])
    gateway.generate(make_request())
    assert len(transport.requests) == 2


# --- circuit breaker --------------------------------------------------------------------
def test_breaker_opens_at_the_threshold_then_half_opens_and_closes() -> None:
    clock = Clock()
    policy = ResiliencePolicy(max_attempts=1, breaker_failure_threshold=2, breaker_open_seconds=30)
    transport, _, gateway = ladder([status(503), status(503), completion_response(GOOD)], policy=policy, clock=clock)
    breaker = gateway.breaker_for(PROVIDER)
    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            gateway.generate(make_request())
    assert breaker.state == BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        gateway.generate(make_request())
    assert len(transport.requests) == 2  # the open breaker sent nothing
    clock.advance(29.9)
    with pytest.raises(CircuitOpenError):
        gateway.generate(make_request())
    clock.advance(0.2)
    assert breaker.state == BreakerState.HALF_OPEN
    gateway.generate(make_request())  # the probe succeeds
    assert breaker.state == BreakerState.CLOSED and len(transport.requests) == 3


def test_failed_probe_reopens_and_only_one_probe_runs() -> None:
    clock = Clock()
    breaker = CircuitBreaker(ResiliencePolicy(breaker_failure_threshold=1, breaker_open_seconds=10), clock)
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == BreakerState.OPEN
    clock.advance(10)
    probe = breaker.before_call()  # the single half-open probe
    assert probe is True
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    breaker.record_failure(probe)
    assert breaker.state == BreakerState.OPEN
    clock.advance(9)
    assert breaker.state == BreakerState.OPEN


def test_success_resets_the_consecutive_failure_count() -> None:
    breaker = CircuitBreaker(ResiliencePolicy(breaker_failure_threshold=3))
    for _ in range(2):
        breaker.before_call()
        breaker.record_failure()
    breaker.before_call()
    breaker.record_success()
    for _ in range(2):
        breaker.before_call()
        breaker.record_failure()
    assert breaker.state == BreakerState.CLOSED


def test_non_retryable_provider_errors_do_not_open_the_breaker() -> None:
    policy = ResiliencePolicy(max_attempts=1, breaker_failure_threshold=1)
    _, _, gateway = ladder([status(401)] * 3, policy=policy)
    for _ in range(3):
        with pytest.raises(ProviderRejectedError):
            gateway.generate(make_request())
    assert gateway.breaker_for(PROVIDER).state == BreakerState.CLOSED


def test_breakers_are_per_provider() -> None:
    _, _, gateway = ladder([])
    assert gateway.breaker_for("a") is not gateway.breaker_for("b")
    assert gateway.breaker_for("a") is gateway.breaker_for("a")


# --- credential handling ----------------------------------------------------------------
def test_credential_is_redacted_and_not_serialisable() -> None:
    credential = Credential(SECRET)
    assert SECRET not in repr(credential) and SECRET not in str(credential)
    assert SECRET not in f"{credential}" and credential.reveal() == SECRET
    with pytest.raises(TypeError):
        pickle.dumps(credential)


def test_transport_request_refuses_credential_headers() -> None:
    for name in ("Authorization", "authorization", "X-API-Key"):
        with pytest.raises(ValueError):
            TransportRequest(url="https://x.invalid", headers={name: "v"}, body={}, timeout_seconds=1)


def test_errors_never_carry_prompt_or_key_text() -> None:
    transport = FakeTransport([completion_response("USER-CANARY not json")])
    with pytest.raises(gateway_errors.MalformedOutputError) as caught:
        make_gateway(transport).generate(make_request(allow_repair=False))
    text = str(caught.value) + repr(caught.value) + repr(caught.value.__cause__)
    assert "USER-CANARY" not in text and SECRET not in text


# --- the real transport, driven by httpx.MockTransport (no network) ---------------------
def httpx_gateway(handler) -> OpenRouterGateway:
    return OpenRouterGateway(
        HttpxTransport.for_tests(httpx.MockTransport(handler)), lambda: SECRET, make_config(), make_policy(),
        placeholder_price_table(MODEL, PROVIDER), clock=Clock(),
    )


def test_httpx_transport_adds_authorization_only_on_the_wire() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion_response(GOOD).body)

    turn = httpx_gateway(handler).generate(make_request())
    assert seen["auth"] == f"Bearer {SECRET}"
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["body"]["provider"]["zdr"] is True  # type: ignore[index]
    assert SECRET not in repr(turn)


def test_httpx_transport_maps_failures() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(ProviderTimeoutError):
        httpx_gateway(timeout).generate(make_request())
    with pytest.raises(ProviderUnavailableError):
        httpx_gateway(refused).generate(make_request())
    with pytest.raises(ProviderRateLimitError):
        httpx_gateway(lambda r: httpx.Response(429, json={"error": {"code": 429}})).generate(make_request())
    with pytest.raises(gateway_errors.MalformedOutputError):
        httpx_gateway(lambda r: httpx.Response(200, content=b"<html>")).generate(make_request())
