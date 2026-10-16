"""L7 (a TurnFailure raised from the discovery path carries no Maps text in its traceback frames)
and L8 (one public validation-stage reason, OUTPUT_REJECTED; telemetry keeps the detail)."""
from __future__ import annotations

import pytest
from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RaisingClient,
    RecordingClient,
    ScriptedGateway,
    canary_payload,
    deep_text,
    fact,
    frame_turn,
)

from app import telemetry as t
from app.discovery import DiscoveryUnavailable, ToolId
from app.discovery._mint import AUTHORITY, mint
from app.discovery.client import ToolResult, payload_ledger_info
from app.domain import ErrorReason, Principal, PrincipalKind
from app.failures import TurnFailure

FORGED = "FORGED-FRAME-CANARY-9d1"


class ForgingClient:
    """Records the ledger for an honest payload and returns a forged one (the validator rejects)."""

    tripped = False

    def __init__(self, clock) -> None:
        self.clock = clock

    def call_tool(self, run, tool, arguments, timeout_seconds=None):
        call_id = run.begin(ToolId.SEARCH_PLACES, authority=AUTHORITY).call_id
        run.record(call_id, payload_ledger_info(ToolId.SEARCH_PLACES, canary_payload()),
                   authority=AUTHORITY)
        return ToolResult(
            tool=ToolId.SEARCH_PLACES, tool_call_id=call_id, run_id=run.run_id,
            retrieved_at=self.clock(), payload=canary_payload(canary=FORGED), size_bytes=10,
            billable_units=1, issued=mint(call_id))


def frames_text(error: BaseException) -> str:
    """Every local of every frame the exception travelled through (the orchestrator, which holds
    the display cache by design, is left out)."""
    parts = []
    tb = error.__traceback__
    while tb is not None:
        for name, value in tb.tb_frame.f_locals.items():
            if name != "self":
                parts.append(deep_text(value))
        tb = tb.tb_next
    return "\n".join(parts)


def run_failure(env: Env, key: str = "k") -> TurnFailure:
    guest = Principal("guest-a", PrincipalKind.GUEST, env.quotas.policies.current().version)
    with pytest.raises(TurnFailure) as caught:
        env.orchestrator.run_turn(guest, "Plan a quiet Toronto restaurant dinner tonight", key)
    return caught.value


def test_a_validator_rejection_leaves_no_maps_text_in_the_traceback_frames() -> None:
    config = make_config()
    env = Env(gateway=ScriptedGateway([frame_turn(config)], config), config=config,
              discovery=lambda e: ForgingClient(e.clock))
    failure = run_failure(env)
    assert failure.reason is not None and failure.status == 502
    assert failure.__cause__ is None and failure.__context__ is None
    text = frames_text(failure)
    assert FORGED not in text and "Invented" not in text, "Maps text survives in a frame local"


def test_a_failed_parse_leaves_no_maps_text_in_the_traceback_frames() -> None:
    config = make_config()
    payload = canary_payload(canary="PARSE-CANARY-77")
    payload["places"][0]["id"] = "not a valid id!"  # fails the documented shape
    maps = RecordingClient(SyntheticMapsClient(payload=payload))
    env = Env(gateway=ScriptedGateway([frame_turn(config)], config), config=config,
              discovery=maps)
    maps.inner._now = env.clock  # noqa: SLF001
    failure = run_failure(env)
    assert "PARSE-CANARY-77" not in frames_text(failure)


def test_a_tool_failure_leaves_nothing_either() -> None:
    config = make_config()
    env = Env(gateway=ScriptedGateway([frame_turn(config)], config), config=config,
              discovery=RaisingClient(DiscoveryUnavailable(calls=1)))
    assert "Invented" not in frames_text(run_failure(env))


# --- L8 ---------------------------------------------------------------------------------------
def test_validation_stage_failures_share_one_public_reason() -> None:
    config = make_config()
    # a patch rejection
    env = Env(gateway=ScriptedGateway([frame_turn(config, facts=[fact("b", "x", hard=True)])],
                                      config),
              discovery=RecordingClient(SyntheticMapsClient()), config=config)
    patch_rejected = env.post().json()
    # a tool the server did not grant
    env2 = Env(gateway=ScriptedGateway([frame_turn(config, tool=ToolId.RESOLVE_NAMES)], config),
               discovery=RecordingClient(SyntheticMapsClient()), config=config)
    tool_refused = env2.post().json()
    # a forged tool result
    env3 = Env(gateway=ScriptedGateway([frame_turn(config)], config), config=config,
               discovery=lambda e: ForgingClient(e.clock))
    forged = env3.post().json()
    # discovery not configured
    env4 = Env(gateway=ScriptedGateway([frame_turn(config)], config), discovery=None, config=config)
    unconfigured = env4.post().json()
    for body in (patch_rejected, tool_refused, forged, unconfigured):
        assert body["reason"] == "OUTPUT_REJECTED", body
    # the detail stays in telemetry
    assert [e.reason for e in env.events(t.ValidationRejection)] == [
        t.ValidationReason.HARD_CONSTRAINT]
    assert [e.reason for e in env2.events(t.ValidationRejection)] == [
        t.ValidationReason.TOOL_NOT_ALLOWED]
    assert env3.events(t.ValidationRejection), "the validator's own rejection event is kept"


def test_the_public_enum_lost_the_fine_validation_reasons() -> None:
    members = set(ErrorReason.__members__)
    assert {"OUTPUT_REJECTED", "RATE_LIMITED", "OUTPUT_TRUNCATED", "OVER_BUDGET", "RESULT_EXPIRED",
            "TURN_IN_PROGRESS"} <= members
    assert not members & {"PATCH_REJECTED", "OUTPUT_MALFORMED", "TOOL_NOT_ALLOWED",
                          "TOOL_RESULT_INVALID", "DISCOVERY_NOT_CONFIGURED"}


def test_openapi_lists_the_collapsed_reasons() -> None:
    env = Env()
    enum = env.client.get("/openapi.json").json()["components"]["schemas"]["ErrorReason"]["enum"]
    assert "OUTPUT_REJECTED" in enum and "RATE_LIMITED" in enum
    assert "PATCH_REJECTED" not in enum and "TOOL_RESULT_INVALID" not in enum
