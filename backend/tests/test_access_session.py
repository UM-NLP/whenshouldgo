"""Session bootstrap, usage and capabilities (docs/access-tiers.md): the server computes the meter."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService, Usage

TOKENS = {
    "tok-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-b": VerifiedIdentity("guest-b", "anonymous"),
    "tok-reg": VerifiedIdentity("reg-1", "google.com"),
    "tok-off": VerifiedIdentity("off-1", "google.com"),
}
FORBIDDEN = ("cost", "token", "price", "usd", "ceiling", "spend", "provider")
TRAVEL = {"message": "Plan a trip to a restaurant"}


def make():
    quotas = QuotaService()
    app = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=quotas.policies, disabled_uids={"off-1"}),
        orchestrator=TurnOrchestrator(quotas, dev_scaffold=True),
    )
    return TestClient(app), quotas


def auth(token="tok-a"):
    return {"Authorization": f"Bearer {token}"}


def set_usage(quotas, uid, turns=0, trips=0):
    quotas._usage[uid] = Usage(successful_turns=turns, activated_trips=trips)


def usage(client, token="tok-a"):
    response = client.get("/v1/me/usage", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "turns,state",
    [(0, "NONE"), (34, "NONE"), (35, "QUIET_70"), (44, "QUIET_70"), (45, "NOTICE_90"),
     (49, "NOTICE_90"), (50, "GATED")],
)
def test_turn_thresholds(turns, state) -> None:
    client, quotas = make()
    set_usage(quotas, "guest-a", turns=turns)
    body = usage(client)
    assert body["meter_state"] == state
    assert (body["user_turns_used"], body["user_turns_limit"]) == (turns, 50)
    assert body["kind"] == "usage" and body["schema_version"] == 1
    assert (body.get("gate_reason") is not None) == (state == "GATED")


@pytest.mark.parametrize(
    "trips,state", [(6, "NONE"), (7, "QUIET_70"), (8, "QUIET_70"), (9, "NOTICE_90"), (10, "GATED")]
)
def test_trip_thresholds(trips, state) -> None:
    client, quotas = make()
    set_usage(quotas, "guest-a", trips=trips)
    body = usage(client)
    assert body["meter_state"] == state
    assert (body["activated_trips_used"], body["activated_trips_limit"]) == (trips, 10)


def test_larger_ratio_wins_and_gate_reason_names_it() -> None:
    client, quotas = make()
    set_usage(quotas, "guest-a", turns=10, trips=10)
    body = usage(client)
    assert body["meter_state"] == "GATED" and body["gate_reason"] == "TRIPS_LIMIT"
    set_usage(quotas, "guest-a", turns=36, trips=2)
    assert usage(client)["meter_state"] == "QUIET_70"
    set_usage(quotas, "guest-a", turns=36, trips=9)
    assert usage(client)["meter_state"] == "NOTICE_90"
    set_usage(quotas, "guest-a", turns=50, trips=0)
    assert usage(client)["gate_reason"] == "TURNS_LIMIT"


def test_guest_period_and_reset() -> None:
    client, _ = make()
    body = usage(client)
    assert body["period"] == "LIFETIME" and body["resets_at"] is None
    reg = usage(client, "tok-reg")
    assert (reg["user_turns_limit"], reg["period"]) == (100, "ROLLING_30_DAYS")


def test_tenant_isolation() -> None:
    client, quotas = make()
    set_usage(quotas, "guest-a", turns=47)
    set_usage(quotas, "guest-b", turns=3)
    assert usage(client, "tok-a")["user_turns_used"] == 47
    assert usage(client, "tok-b")["user_turns_used"] == 3
    b = client.post("/v1/session:bootstrap", headers=auth("tok-b")).json()
    assert b["usage"]["user_turns_used"] == 3


def test_free_turns_do_not_count() -> None:
    client, _ = make()
    before = usage(client)["user_turns_used"]
    response = client.post("/v1/agent/turns", json={"message": "write me a poem about tax law"},
                           headers={**auth(), "Idempotency-Key": "free-1"})
    assert response.status_code == 200
    assert usage(client)["user_turns_used"] == before
    assert response.json()["usage"]["user_turns_used"] == before


def test_turn_response_carries_usage_and_matches_usage_endpoint() -> None:
    client, _ = make()
    response = client.post("/v1/agent/turns", json=TRAVEL, headers={**auth(), "Idempotency-Key": "t-1"})
    assert response.status_code == 200
    assert response.json()["usage"] == usage(client)
    assert response.json()["usage"]["user_turns_used"] == 1
    replay = client.post("/v1/agent/turns", json=TRAVEL, headers={**auth(), "Idempotency-Key": "t-1"})
    assert replay.json()["usage"]["user_turns_used"] == 1


def test_gated_turn_response_keeps_flag_and_usage() -> None:
    client, quotas = make()
    set_usage(quotas, "guest-a", turns=50)
    response = client.post("/v1/agent/turns", json=TRAVEL, headers={**auth(), "Idempotency-Key": "t-2"})
    body = response.json()
    assert body["allowance_exhausted"] is True and body["usage"]["meter_state"] == "GATED"


def test_bootstrap_is_idempotent_and_typed() -> None:
    client, quotas = make()
    first = client.post("/v1/session:bootstrap", headers=auth())
    second = client.post("/v1/session:bootstrap", headers=auth())
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["kind"] == "session_bootstrap" and a["tier"] == "guest"
    assert a["policy_version"] == quotas.policies.current().version
    strip = lambda d: {k: v for k, v in d.items() if k != "server_time"}  # noqa: E731
    assert strip(a) == strip(b)
    datetime.fromisoformat(a["server_time"].replace("Z", "+00:00"))
    assert a["usage"]["user_turns_used"] == 0
    assert a["capabilities"]["can_start_turn"] is True
    assert client.post("/v1/session:bootstrap", headers=auth("tok-reg")).json()["tier"] == "registered"
    assert quotas.usage_for("guest-a").successful_turns == 0


def test_bootstrap_takes_no_body_identity() -> None:
    client, _ = make()
    response = client.post("/v1/session:bootstrap", headers=auth(), json={"uid": "guest-b"})
    assert response.status_code == 200  # body is not part of the contract and is never read
    assert "guest-b" not in response.text


def test_capabilities_by_state() -> None:
    client, quotas = make()
    caps = client.get("/v1/me/capabilities", headers=auth()).json()
    assert caps["kind"] == "capabilities" and caps["can_start_turn"] is True
    assert caps["can_use_location"] and caps["can_read"]
    assert caps["can_export"] is False and caps["can_delete"] is False
    set_usage(quotas, "guest-a", turns=50)
    gated = client.get("/v1/me/capabilities", headers=auth()).json()
    assert gated["can_start_turn"] is False
    assert gated["can_read"] and not gated["can_export"] and not gated["can_delete"]


@pytest.mark.parametrize("path,method", [("/v1/me/usage", "get"), ("/v1/me/capabilities", "get"),
                                          ("/v1/session:bootstrap", "post")])
def test_unauthenticated_and_disabled(path, method) -> None:
    client, _ = make()
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    off = getattr(client, method)(path, headers=auth("tok-off"))
    assert off.status_code == 403 and off.json()["code"] == "POLICY"


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_no_cost_or_token_fields_anywhere() -> None:
    client, quotas = make()
    set_usage(quotas, "guest-a", turns=46)
    responses = [
        client.get("/v1/me/usage", headers=auth()).json(),
        client.get("/v1/me/capabilities", headers=auth()).json(),
        client.post("/v1/session:bootstrap", headers=auth()).json(),
        client.post("/v1/agent/turns", json=TRAVEL, headers={**auth(), "Idempotency-Key": "t-3"}).json(),
    ]
    for body in responses:
        for key in _keys(body):
            assert not any(word in key.lower() for word in FORBIDDEN), key
    schemas = client.app.openapi()["components"]["schemas"]
    for name in ("UsageView", "Capabilities", "SessionBootstrap"):
        assert schemas[name]["additionalProperties"] is False
        assert not any(w in json.dumps(list(schemas[name]["properties"])).lower() for w in FORBIDDEN)


def test_rate_limited_bootstrap_declares_retry_after() -> None:
    client, _ = make()
    last = None
    for _ in range(40):
        last = client.post("/v1/session:bootstrap", headers=auth())
        if last.status_code == 429:
            break
    assert last.status_code == 429 and int(last.headers["Retry-After"]) >= 1
    assert last.json()["reason"] == "RATE_LIMITED"
    assert client.post("/v1/session:bootstrap", headers=auth("tok-b")).status_code == 200


def test_openapi_declares_headers_and_responses() -> None:
    client, _ = make()
    schema = client.app.openapi()
    for path, method in (("/v1/session:bootstrap", "post"), ("/v1/me/usage", "get"),
                         ("/v1/me/capabilities", "get"), ("/v1/agent/turns", "post")):
        op = schema["paths"][path][method]
        assert "Retry-After" in op["responses"]["429"]["headers"], path
        auth_param = [p for p in op["parameters"] if p["name"] == "authorization"]
        assert auth_param and auth_param[0]["required"] is True, path
    turn_params = {p["name"]: p for p in schema["paths"]["/v1/agent/turns"]["post"]["parameters"]}
    assert turn_params["idempotency-key"]["required"] is True
    assert "usage" in schema["components"]["schemas"]["TurnResponse"]["required"]
