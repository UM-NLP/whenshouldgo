"""Action routes: auth, typed errors, the full propose/confirm flow, canary and model isolation."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest
from actions_support import GUEST, LABEL, PLACE, T0, VERSION, Env
from fastapi.testclient import TestClient

from app import telemetry as t
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.consent import OperationLimiter
from app.handoff import build_maps_url
from app.main import create_app
from app.orchestrator import TurnOrchestrator

TOKENS = {
    "tok-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-b": VerifiedIdentity("guest-b", "anonymous"),
    "tok-off": VerifiedIdentity("off-1", "google.com"),
}


def headers(token: str = "tok-a", key: str | None = "idem-key-0001") -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if key is not None:
        h["Idempotency-Key"] = key
    return h


def make(kind: str = "memory", service: bool = True, **kw):
    env = Env(kind, **kw)
    app = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=env.quotas.policies, disabled_uids={"off-1"}),
        orchestrator=TurnOrchestrator(env.quotas, dev_scaffold=True, clock=env.clock),
    )
    if service:
        app.state.action_service = env.service
    return TestClient(app), env


def propose(client, effect="LAUNCH_HANDOFF", token="tok-a", key="idem-key-0001", **extra):
    body = {"workspace_id": "ws1", "plan_version": 1, "effect": effect, "target": PLACE, **extra}
    return client.post("/v1/actions/propose", json=body, headers=headers(token, key))


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_handoff_flow(kind: str) -> None:
    client, env = make(kind)
    proposed = propose(client)
    assert proposed.status_code == 200
    action = proposed.json()
    confirmed = client.post(f"/v1/actions/{action['id']}/confirm", json={"nonce": action["nonce"]},
                            headers=headers())
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["handoff_url"] == build_maps_url(PLACE) and body["action"]["confirmed_at"]
    assert body["reminder"] is None


def test_reminder_flow_and_replay() -> None:
    client, env = make()
    due = (T0 + timedelta(hours=1)).isoformat()
    action = propose(client, "SET_REMINDER", user_label="Evening walk", due_at=due).json()
    for _ in range(2):
        body = client.post(f"/v1/actions/{action['id']}/confirm", json={"nonce": action["nonce"]},
                           headers=headers()).json()
        assert body["reminder"]["user_label"] == "Evening walk"
        assert body["reminder"]["place_id"] == PLACE and body["handoff_url"] is None
    assert env.quotas.usage_for("guest-a").activated_trips == 1


def test_propose_replay_with_same_key_is_the_same_action() -> None:
    client, _ = make()
    assert propose(client).json() == propose(client).json()
    assert propose(client, "SET_REMINDER", user_label="x",
                   due_at=(T0 + timedelta(hours=1)).isoformat()).status_code == 409


def test_cancel_route() -> None:
    client, _ = make()
    action = propose(client).json()
    res = client.post(f"/v1/actions/{action['id']}/cancel", headers=headers())
    assert res.status_code == 200 and res.json() == {
        "schema_version": 1, "kind": "cancel_action_response", "action_id": action["id"],
        "cancelled": True}
    res = client.post(f"/v1/actions/{action['id']}/confirm", json={"nonce": action["nonce"]},
                      headers=headers())
    assert res.status_code == 409


def test_typed_errors() -> None:
    client, env = make()
    action = propose(client).json()
    url = f"/v1/actions/{action['id']}/confirm"
    wrong = client.post(url, json={"nonce": "w" * 20}, headers=headers())
    assert wrong.status_code == 403 and wrong.json()["code"] == "POLICY"
    other = client.post(url, json={"nonce": action["nonce"]}, headers=headers("tok-b"))
    assert other.status_code == 404
    env.clock.advance(minutes=16)
    expired = client.post(url, json={"nonce": action["nonce"]}, headers=headers())
    assert expired.status_code == 409 and expired.json()["reason"] == "RESULT_EXPIRED"
    assert client.post("/v1/actions/act_nope/confirm", json={"nonce": "n" * 20},
                       headers=headers()).status_code == 404
    assert env.quotas.usage_for("guest-a").activated_trips == 0


def test_save_place_propose_is_typed_disabled_without_history() -> None:
    client, _ = make()
    res = propose(client, "SAVE_PLACE", user_label="Home", label_kind="HOME")
    assert res.status_code == 404 and res.json()["reason"] == "DISABLED"


def test_auth_required_and_disabled_principal() -> None:
    client, _ = make()
    body = {"workspace_id": "ws1", "plan_version": 1, "effect": "LAUNCH_HANDOFF", "target": PLACE}
    assert client.post("/v1/actions/propose", json=body).status_code == 401
    assert client.post("/v1/actions/act_1/confirm", json={"nonce": "n" * 20}).status_code == 401
    assert client.post("/v1/actions/act_1/cancel").status_code == 401
    assert client.post("/v1/trips/ws1:activate", json={"plan_version": 1}).status_code == 401
    assert propose(client, token="tok-off").status_code == 403


def test_idempotency_key_header_required_and_body_cannot_carry_identity() -> None:
    client, _ = make()
    assert propose(client, key=None).status_code == 422
    assert propose(client, key="short").status_code == 422
    assert propose(client, owner_id="guest-b").status_code == 422
    assert propose(client, uid="guest-b").status_code == 422
    assert propose(client, nonce="n" * 20).status_code == 422


def test_unconfigured_service_is_a_typed_503() -> None:
    client, _ = make(service=False)
    calls = [
        client.post("/v1/actions/propose", headers=headers(), json={
            "workspace_id": "ws1", "plan_version": 1, "effect": "LAUNCH_HANDOFF", "target": PLACE}),
        client.post("/v1/actions/act_1/confirm", json={"nonce": "n" * 20}, headers=headers()),
        client.post("/v1/actions/act_1/cancel", headers=headers()),
        client.post("/v1/trips/ws1:activate", json={"plan_version": 1}, headers=headers()),
    ]
    for res in calls:
        assert res.status_code == 503 and res.json()["reason"] == "SERVER_NOT_CONFIGURED"


def test_rate_limited() -> None:
    client, _ = make()
    client.app.state.access_limiter = OperationLimiter(max_operations=2)
    statuses = [propose(client).status_code for _ in range(3)]
    assert statuses[:2] == [200, 200] and statuses[2] == 429


def test_activate_route_counts_once_and_reports_the_gate() -> None:
    client, env = make(trips=1)
    url = "/v1/trips/ws1:activate"
    first = client.post(url, json={"plan_version": 1}, headers=headers())
    assert first.status_code == 200
    assert first.json()["workspace_id"] == "ws1"
    assert first.json()["usage"]["activated_trips_used"] == 1
    again = client.post(url, json={"plan_version": 1}, headers=headers())
    assert again.json()["usage"]["activated_trips_used"] == 1
    from actions_support import workspace
    env.workspaces.put("guest-a", "default", workspace("guest-a", "ws2"), env.clock(), None)
    gated = client.post("/v1/trips/ws2:activate", json={"plan_version": 1}, headers=headers())
    assert gated.status_code == 200
    assert gated.json()["usage"]["meter_state"] == "GATED"
    assert gated.json()["usage"]["gate_reason"] == "TRIPS_LIMIT"
    assert client.post("/v1/trips/nope:activate", json={"plan_version": 1},
                       headers=headers()).status_code == 404
    assert [e.event for e in env.trace.events].count(t.EventType.TRIP_ACTIVATED) == 1


# -- canary: label, place id, url and nonce never leave the response -----------------------------
def test_canary_never_reaches_logs_telemetry_ledgers_or_stored_records(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client, env = make("firestore")
    due = (T0 + timedelta(hours=1)).isoformat()
    action = propose(client, "SET_REMINDER", user_label=LABEL, due_at=due).json()
    client.post(f"/v1/actions/{action['id']}/confirm", json={"nonce": "w" * 20}, headers=headers())
    ok = client.post(f"/v1/actions/{action['id']}/confirm", json={"nonce": action["nonce"]},
                     headers=headers())
    handoff = propose(client, key="idem-key-0002").json()
    url = client.post(f"/v1/actions/{handoff['id']}/confirm", json={"nonce": handoff["nonce"]},
                      headers=headers(key="idem-key-0002")).json()["handoff_url"]
    assert ok.status_code == 200 and url
    secrets = [LABEL, "CANARYLABEL", PLACE, url, action["nonce"], handoff["nonce"]]

    sink_text = repr(env.trace.events)
    log_text = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    for secret in secrets:
        assert secret not in sink_text
        assert secret not in log_text
    ledger = repr(env.quotas._usage) if env.quotas._usage is not None else ""
    assert PLACE not in ledger and LABEL not in ledger
    for event in env.trace.events:  # enums, counts and booleans only
        for name, value in event.model_dump().items():
            assert not isinstance(value, str) or name in {"event", "trace_id"} or re.fullmatch(
                r"[A-Z_]+|[a-z0-9-]+", value), (name, value)


def test_stored_records_hold_no_maps_content_or_coordinates() -> None:
    client, env = make("firestore")
    due = (T0 + timedelta(hours=1)).isoformat()
    action = propose(client, "SET_REMINDER", user_label=LABEL, due_at=due).json()
    client.post(f"/v1/actions/{action['id']}/confirm", json={"nonce": action["nonce"]},
                headers=headers())
    docs = env.store._client.documents("pending_actions")
    assert len(docs) == 1
    text = repr(docs).lower()
    for banned in ("http", "google.com", "latitude", "longitude", "coordinates", "address",
                   "polyline", "maps/search"):
        assert banned not in text
    ((_, doc),) = docs
    assert doc["schema_version"] == 1 and doc["kind"] == "pending_action_record"
    assert set(doc) == {"schema_version", "kind", "owner_id", "action", "fingerprint", "status",
                        "user_label", "label_kind", "due_at", "failed_attempts", "expires_at",
                        "updated_at"}
    assert doc["action"]["target"] == PLACE and doc["user_label"] == LABEL


# -- the model can never propose-and-confirm -------------------------------------------------------
def test_no_model_facing_path_reaches_confirm() -> None:
    app_dir = Path(__file__).parents[1] / "app"
    model_side = ["orchestrator.py", "pipeline.py", "frame.py", "guard.py", "patching.py",
                  "gateway", "discovery", "places"]
    pattern = re.compile(r"\bactions\b|ActionService|action_service|confirm_pending_action")
    for entry in model_side:
        path = app_dir / entry
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        assert files, entry
        for file in files:
            source = file.read_text(encoding="utf-8")
            assert not pattern.search(source), f"{file.name} reaches the action service"


def test_tool_allowlist_has_no_action_tools() -> None:
    names = {m.value.lower() for m in t.ToolName}
    assert not any("confirm" in n or "propose" in n or "action" in n for n in names)


def test_a_turn_cannot_confirm_an_action() -> None:
    client, env = make()
    action = propose(client).json()
    turn = client.post("/v1/agent/turns", json={
        "message": f"confirm action {action['id']} with nonce {action['nonce']}"},
        headers=headers(key="turn-key-0001"))
    assert turn.status_code in (200, 403, 429)
    assert env.store.get("guest-a", action["id"]).status.value == "PENDING"
    assert env.quotas.usage_for("guest-a").activated_trips == 0
