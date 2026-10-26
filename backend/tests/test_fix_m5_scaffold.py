"""M5: the development scaffold is explicit (``dev_scaffold=True``). Without a gateway a
verifier-configured app answers turns with a typed 503 instead of silently serving it."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import (
    TOKENS,
    TRAVEL,
    Env,
    RecordingClient,
    ScriptedGateway,
    frame_turn,
)

from app.auth import StaticTokenVerifier
from app.domain import Principal, PrincipalKind
from app.frame import FrameIntent
from app.main import create_app
from app.orchestrator import TurnOrchestrator

HEADERS = {"Authorization": "Bearer tok-a", "Idempotency-Key": "k1"}


def client_for(**kw) -> TestClient:
    return TestClient(create_app(verifier=StaticTokenVerifier(TOKENS), **kw))


def test_no_gateway_and_no_scaffold_flag_is_a_typed_503() -> None:
    client = client_for()
    response = client.post("/v1/agent/turns", json=TRAVEL, headers=HEADERS)
    assert response.status_code == 503, "the scaffold was served without dev_scaffold=True"
    body = response.json()
    assert (body["code"], body.get("reason")) == ("PROVIDER", "SERVER_NOT_CONFIGURED")
    assert "workspace_id" not in body and "reply" not in body


def test_the_503_consumes_no_allowance_and_creates_no_workspace() -> None:
    app = create_app(verifier=StaticTokenVerifier(TOKENS))
    TestClient(app).post("/v1/agent/turns", json=TRAVEL, headers=HEADERS)
    orchestrator = app.state.orchestrator
    assert orchestrator.workspaces == {}
    assert orchestrator.quotas.usage_for("guest-a").successful_turns == 0
    assert orchestrator.quotas.usage_for("guest-a").reserved_keys == set()


def test_refusals_that_need_no_model_still_work_without_a_gateway() -> None:
    response = client_for().post("/v1/agent/turns", json={"message": "Write my python homework"},
                                 headers=HEADERS)
    assert response.status_code == 200 and response.json()["domain"] == "OUT_OF_SCOPE"


def test_dev_scaffold_true_serves_the_scaffold_and_stores_the_goal() -> None:
    try:
        app = create_app(verifier=StaticTokenVerifier(TOKENS), dev_scaffold=True)
    except TypeError:
        pytest.fail("create_app has no dev_scaffold keyword")
    response = TestClient(app).post("/v1/agent/turns", json=TRAVEL, headers=HEADERS)
    assert response.status_code == 200 and "What area" in response.json()["reply"]
    assert app.state.orchestrator.workspaces[("guest-a", "default")].goal == TRAVEL["message"]


def test_the_orchestrator_has_the_same_flag() -> None:
    guest = Principal("g", PrincipalKind.GUEST, "x")
    with pytest.raises(Exception) as caught:
        TurnOrchestrator().run_turn(guest, "Plan a Toronto outing", "k")
    assert getattr(caught.value, "status", None) == 503, "no typed refusal without the flag"
    assert TurnOrchestrator(dev_scaffold=True).run_turn(guest, "Plan a Toronto outing", "k").workspace


def test_a_discovery_client_without_a_gateway_is_a_configuration_error() -> None:
    with pytest.raises(ValueError):
        create_app(discovery_client=SyntheticMapsClient())
    with pytest.raises(ValueError):
        TurnOrchestrator(discovery_client=SyntheticMapsClient())


def test_the_module_level_app_stays_importable_and_fails_closed() -> None:
    from app.main import app as module_app

    response = TestClient(module_app).post("/v1/agent/turns", json=TRAVEL)
    assert response.status_code == 401


def test_the_goal_is_never_stored_on_the_gateway_path() -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config, intent=FrameIntent.OTHER)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    assert env.post().status_code == 200
    assert env.orchestrator.workspaces[("guest-a", "default")].goal is None
