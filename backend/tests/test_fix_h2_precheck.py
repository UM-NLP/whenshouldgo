"""H2: availability is checked BEFORE the FRAME model call, in a fixed order:
discovery client configured, discovery not tripped, gateway quota latch not tripped.

Rule: every turn that reaches the pipeline is TRAVEL, and a TRAVEL turn is discovery-class
whenever the server allowlist grants search_places (the guard grants it for TRAVEL). FRAME
decides the intent only AFTER it runs, so a discovery-class turn is short-circuited when
discovery is unavailable; a turn whose allowlist grants no discovery tool could proceed
without it and is not."""
from __future__ import annotations

import pytest
from maps_simulator import FakeMcpTransport, SyntheticMapsClient, mcp_ok
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RecordingClient,
    ScriptedGateway,
    canary_payload,
    frame_turn,
    real_client,
)

from app import telemetry as t
from app.discovery import GroundingLiteClient, GroundingLiteMcpClient, KillSwitch
from app.frame import FrameIntent


def reason_of(response, status: int) -> str | None:
    assert response.status_code == status, response.text
    return response.json().get("reason")


class LatchedGateway(ScriptedGateway):
    latched = True


def test_a_tripped_kill_switch_means_zero_gateway_requests_for_a_discovery_turn() -> None:
    config = make_config()
    switch = KillSwitch()
    switch.trip()
    transport = FakeMcpTransport([])
    gateway = ScriptedGateway([frame_turn(config)], config)
    env = Env(gateway=gateway, config=config,
              discovery=lambda e: real_client(config, transport, e.sink, e.clock, switch))
    response = env.post()
    assert gateway.requests == [], "FRAME was called although discovery is switched off"
    assert reason_of(response, 503) == "DISCOVERY_UNAVAILABLE"
    assert env.turns() == 0 and env.reserved() == set() and env.costs.entries() == ()
    assert transport.requests == []
    denied = [e for e in env.events(t.ToolCall) if e.outcome == t.ToolOutcome.DENIED]
    assert denied, "the tripped refusal must stay visible in telemetry"


def test_discovery_not_configured_means_zero_gateway_requests() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER)], config)
    env = Env(gateway=gateway, discovery=None, config=config)
    response = env.post()
    assert gateway.requests == [], "FRAME was called although discovery is not configured"
    assert reason_of(response, 503) == "DISCOVERY_NOT_CONFIGURED"
    assert env.turns() == 0 and env.reserved() == set()


def test_a_latched_gateway_means_zero_gateway_requests() -> None:
    config = make_config()
    gateway = LatchedGateway([frame_turn(config)], config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config)
    response = env.post()
    assert gateway.requests == [], "FRAME was called although the provider quota latch is tripped"
    assert reason_of(response, 503) == "PROVIDER_UNAVAILABLE"
    assert maps.calls == [] and env.turns() == 0 and env.reserved() == set()


def test_the_checks_run_in_the_documented_order() -> None:
    config = make_config()
    switch = KillSwitch()
    switch.trip()
    both = Env(gateway=LatchedGateway([], config), config=config,
               discovery=lambda e: real_client(config, FakeMcpTransport([]), e.sink, e.clock,
                                               switch))
    assert reason_of(both.post(), 503) == "DISCOVERY_UNAVAILABLE"  # tripped beats latched
    missing = Env(gateway=LatchedGateway([], config), discovery=None, config=config)
    assert reason_of(missing.post(), 503) == "DISCOVERY_NOT_CONFIGURED"  # not configured first


def test_a_healthy_discovery_turn_still_runs_frame_once() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config)], config)
    transport = FakeMcpTransport([mcp_ok(canary_payload())])
    env = Env(gateway=gateway, config=config,
              discovery=lambda e: real_client(config, transport, e.sink, e.clock))
    assert env.post().status_code == 200 and len(gateway.requests) == 1


def test_the_client_protocol_exposes_a_read_only_tripped_property() -> None:
    switch = KillSwitch()
    client = GroundingLiteMcpClient(FakeMcpTransport([]), lambda: "AIzaSyFAKE-0123456789",
                                    config=make_config(), kill_switch=switch)
    assert getattr(client, "tripped", None) is False
    switch.trip()
    assert client.tripped is True
    with pytest.raises(AttributeError):
        client.tripped = False  # type: ignore[misc]
    assert not hasattr(client, "reset") and "tripped" in dir(GroundingLiteClient)
    assert isinstance(SyntheticMapsClient(), GroundingLiteClient)  # test doubles follow suit
