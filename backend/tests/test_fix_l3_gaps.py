"""L3: tests for gaps the first pass left: patch ordering and statuses, scope abuse, the
duplicate-reservation fall-through, and the pipeline through the REAL OpenRouter adapter."""
from __future__ import annotations

import json

import pytest
from gateway_fakes import FakeTransport, completion_response
from maps_simulator import SyntheticMapsClient
from pydantic import ValidationError
from test_gateway_support import make_config, make_gateway
from wiring_support import (
    CANARY,
    Env,
    RecordingClient,
    ScriptedGateway,
    canary_payload,
    fact,
    frame_turn,
)

from app import telemetry as t
from app.domain import Fact, NextAction, TripWorkspace, WorkspacePatch, WorkspaceStatus
from app.frame import FrameIntent
from app.patching import PatchRejected, apply_patch
from app.telemetry import ValidationReason


def workspace(status: WorkspaceStatus) -> TripWorkspace:
    return TripWorkspace(id="ws-1", owner_id="u1", version=3, status=status)


def patch(**kw) -> WorkspacePatch:
    fields = dict(workspace_id="ws-1", base_version=3, next_action=NextAction.SEARCH)
    fields.update(kw)
    return WorkspacePatch(**fields)


def reason_of(base, proposal):
    try:
        apply_patch(base, proposal)
    except PatchRejected as rejected:
        return rejected.reason
    return None


# --- patch statuses and ordering ----------------------------------------------------------------
@pytest.mark.parametrize("status", [WorkspaceStatus.COMPLETED, WorkspaceStatus.CANCELLED,
                                    WorkspaceStatus.EXPIRED, WorkspaceStatus.UNSUPPORTED])
def test_a_terminal_workspace_accepts_no_patch_at_all(status) -> None:
    assert reason_of(workspace(status), patch()) == ValidationReason.UNKNOWN_FIELD
    assert reason_of(workspace(status), patch(upsert_facts=[fact("party", "two")])) is not None


@pytest.mark.parametrize("status", [WorkspaceStatus.PROPOSED, WorkspaceStatus.CONFIRMED,
                                    WorkspaceStatus.ARMED, WorkspaceStatus.ACTIVE,
                                    WorkspaceStatus.ADAPTING])
def test_a_later_workspace_keeps_its_status_and_refuses_status_changes(status) -> None:
    base = workspace(status)
    assert apply_patch(base, patch(upsert_facts=[fact("party", "two")])).status == status
    assert reason_of(base, patch(status=WorkspaceStatus.FRAMING)) == ValidationReason.UNKNOWN_FIELD


def test_the_header_is_checked_before_next_action_status_and_facts() -> None:
    bad_everything = patch(workspace_id="other", next_action=NextAction.CONFIRM,
                           status=WorkspaceStatus.CONFIRMED,
                           upsert_facts=[fact("party", "x", hard=True)])
    assert reason_of(workspace(WorkspaceStatus.EXPLORING), bad_everything) == ValidationReason.OTHER


def test_next_action_is_checked_before_the_facts() -> None:
    both = patch(next_action=NextAction.PROPOSE_ACTION, upsert_facts=[fact("party", "x", hard=True)])
    assert reason_of(workspace(WorkspaceStatus.EXPLORING), both) == ValidationReason.UNKNOWN_FIELD


def test_scope_cannot_be_oversize_or_empty() -> None:
    for scope in ("", "s" * 65):
        with pytest.raises(ValidationError):
            Fact(id="a", key="a", value="v", source="INFERENCE", confirmation="UNCONFIRMED",
                 hard=False, scope=scope)


# --- the duplicate-reservation fall-through -----------------------------------------------------
def test_a_committed_key_without_a_replay_record_never_reruns_the_paid_pipeline() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    from app.domain import Principal, PrincipalKind

    guest = Principal("guest-a", PrincipalKind.GUEST, env.quotas.policies.current().version)
    env.quotas.commit_turn(guest, "lost")  # counted, but its replay record was lost
    response = env.post(key="lost")
    assert response.status_code == 409 and response.json().get("reason") == "RESULT_EXPIRED"
    assert gateway.requests == [], "the paid pipeline re-ran under a committed key"
    assert env.turns() == 1


# --- the pipeline through the REAL OpenRouter adapter ---------------------------------------------
GOOD_FRAME = json.dumps({
    "patch": {"workspace_id": "WS", "base_version": 0, "next_action": "ASK"},
    "intent": "OTHER",
})


def real_env(*responses, **kw) -> tuple[Env, FakeTransport]:
    transport = FakeTransport(list(responses))
    config = make_config()
    gateway = make_gateway(transport, config=config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient(
        payload=canary_payload())), config=config, **kw)
    return env, transport


def test_real_adapter_malformed_json_is_a_typed_502_with_cost_and_quota_released() -> None:
    env, transport = real_env(completion_response("not json at all"),
                              completion_response("still not json"))
    response = env.post()
    assert response.status_code == 502 and response.json()["reason"] == "OUTPUT_REJECTED"
    assert env.turns() == 0 and env.reserved() == set() and env.orchestrator.workspaces == {}
    (entry,) = env.costs.entries()
    assert entry.input_tokens > 0 and entry.model_calls >= 1
    assert len(transport.requests) == 2  # one bounded repair, then give up


def test_real_adapter_truncated_output_is_a_typed_502() -> None:
    cut = completion_response('{"patch":', finish_reason="length")
    env, transport = real_env(cut, cut)  # a truncated answer gets one bounded repair, also cut
    response = env.post()
    assert response.status_code == 502 and response.json()["reason"] == "OUTPUT_TRUNCATED"
    assert env.turns() == 0 and env.reserved() == set() and len(transport.requests) == 2
    (entry,) = env.costs.entries()
    assert entry.input_tokens > 0 and entry.model_calls == 2


def test_real_adapter_wrong_schema_is_a_typed_502() -> None:
    wrong = json.dumps({"answer": "hello", "count": 1})
    env, transport = real_env(completion_response(wrong), completion_response(wrong))
    response = env.post()
    assert response.status_code == 502 and response.json()["reason"] == "OUTPUT_REJECTED"
    assert env.turns() == 0 and len(transport.requests) == 2 and env.costs.entries()


def test_real_adapter_missing_usage_is_charged_at_the_worst_case() -> None:
    env, _ = real_env(completion_response(GOOD_FRAME, prompt_tokens=None, completion_tokens=None))
    response = env.post()
    assert response.status_code == 503
    (entry,) = env.costs.entries()
    assert entry.input_tokens > 0 and entry.output_tokens == (
        env.config.token_ceilings.planner_output_tokens)
    assert env.turns() == 0 and env.reserved() == set()
    assert env.events(t.GatewayCall)[0].output_tokens == entry.output_tokens


def test_real_adapter_success_path_runs_through_to_the_block() -> None:
    config = make_config()

    class EchoingTransport(FakeTransport):
        """Answers with a FRAME result that echoes the workspace id found in the prompt."""

        def send(self, request, credential):
            state = next(m["content"] for m in request.body["messages"]
                         if m["content"].startswith("WORKSPACE_STATE_JSON:"))
            ids = json.loads(state.split(":", 1)[1])
            answer = {
                "patch": {"workspace_id": ids["workspace_id"], "base_version": ids["base_version"],
                          "next_action": "SEARCH"},
                "intent": "DISCOVERY", "tool": "search_places",
                "search": {"text_query": "quiet restaurant"},
            }
            self._script = [completion_response(json.dumps(answer))]
            return super().send(request, credential)

    transport = EchoingTransport([])
    gateway = make_gateway(transport, config=config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config)
    maps.inner._now = env.clock  # noqa: SLF001
    response = env.post()
    assert response.status_code == 200 and CANARY in response.text
    sent = json.dumps([r.body for r in transport.requests])
    assert CANARY not in sent and "google.com/maps" not in sent
    assert env.turns() == 1
