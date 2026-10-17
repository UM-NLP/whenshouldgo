"""M1: ResilientGateway keeps charging across retries. When the retries run out, the error it
raises carries the ACCUMULATED usage and the TOTAL calls sent, so the orchestrator's cost ledger
sees every paid attempt, not just the last. Real ResilientGateway + OpenRouterGateway + fake
transport; no scripted gateway."""
from __future__ import annotations

import pytest
from gateway_fakes import completion_response
from wiring_support import real_stack

from app.gateway import TransportResponse
from app.gateway.errors import TransportTimeout

SERVER_ERROR = TransportResponse(status_code=500, body=None)


def one_attempt_charge() -> tuple[int, int]:
    env, transport, _ = real_stack([SERVER_ERROR], attempts=1)
    assert env.post().status_code == 503
    (entry,) = env.costs.entries()
    return entry.input_tokens, entry.output_tokens


@pytest.mark.parametrize("failure", [SERVER_ERROR, TransportTimeout()],
                         ids=["5xx", "timeout"])
def test_three_failed_attempts_are_charged_as_three(failure) -> None:
    single_in, single_out = one_attempt_charge()
    env, transport, _ = real_stack([failure, failure, failure], attempts=3)
    assert env.post().status_code in (503, 504)
    assert len(transport.requests) == 3
    (entry,) = env.costs.entries()
    assert entry.model_calls == 3, "the ledger counted only the last attempt's calls"
    assert (entry.input_tokens, entry.output_tokens) == (3 * single_in, 3 * single_out)


def test_a_non_retryable_failure_after_retries_still_carries_the_earlier_attempts() -> None:
    single_in, _ = one_attempt_charge()
    env, transport, _ = real_stack(
        [SERVER_ERROR, completion_response("not json"), completion_response("not json")],
        attempts=3)
    response = env.post()
    assert response.status_code == 502
    (entry,) = env.costs.entries()
    assert entry.model_calls >= 2  # no repair for FRAME: the 5xx attempt and the malformed reply
    assert entry.input_tokens >= single_in, "the first (5xx) attempt was dropped from the charge"


def test_a_retry_that_recovers_is_still_one_turn_with_all_usage() -> None:
    env, transport, _ = real_stack([SERVER_ERROR], attempts=3)
    assert env.post().status_code == 200
    (entry,) = env.costs.entries()
    assert entry.model_calls == 2 and len(transport.requests) == 2
