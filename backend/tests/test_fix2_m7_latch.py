"""M7: ``latched`` is part of the ModelGateway protocol and a gateway without it is treated as
latched (fail closed). A refusal before anything was sent (the latch) is neither charged nor
counted as a paid failure. Real ResilientGateway over the real OpenRouterGateway."""
from __future__ import annotations

from datetime import timedelta

from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, real_stack

from app.gateway import ModelGateway


class NoLatchGateway:
    """A gateway object that predates the protocol member."""

    def __init__(self) -> None:
        self.requests: list = []

    def generate(self, request):
        self.requests.append(request)
        raise AssertionError("must not be called")


def test_the_protocol_names_latched() -> None:
    assert "latched" in dir(ModelGateway)
    assert ScriptedGateway([]).latched is False


def test_a_gateway_without_latched_fails_closed() -> None:
    config = make_config()
    gateway = NoLatchGateway()
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    response = env.post()
    assert gateway.requests == [], "a gateway of unknown latch state was called"
    assert response.status_code == 503 and env.turns() == 0 and env.reserved() == set()


def test_a_latch_tripped_between_the_check_and_the_call_is_free(monkeypatch) -> None:
    env, transport, inner = real_stack(attempts=3, config=make_config(max_paid_failed_turns_per_hour=1))
    pipeline = env.orchestrator._pipeline  # noqa: SLF001
    monkeypatch.setattr(pipeline, "_check_available", lambda allowed: None)  # the race window
    inner.quota_latch.trip()
    response = env.post()
    assert response.status_code == 503, response.text
    assert transport.requests == []
    assert env.costs.entries() == (), "a refusal before sending was charged"
    assert env.costs.failed_paid_turns("guest-a", window=timedelta(hours=1)) == 0
    assert env.turns() == 0 and env.reserved() == set()
    again = env.post(key="again")  # still not rate limited: it never counted as a paid failure
    assert again.status_code == 503 and "RATE_LIMITED" not in again.text


def test_the_real_latch_is_noticed_before_the_call() -> None:
    env, transport, inner = real_stack(attempts=3)
    inner.quota_latch.trip()
    response = env.post()
    assert response.status_code == 503 and transport.requests == []
    assert env.costs.entries() == ()
