"""The composed app end to end over the fakes, and the entry points that must not change."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from composition_support import CANARY, ENV, HEADERS, MESSAGE, TOKEN, build
from fastapi.testclient import TestClient

from app import main, server
from app.composition import ConfigurationError


def test_no_token_is_401_and_a_wrong_token_is_401() -> None:
    client = TestClient(build().app)
    assert client.post("/v1/agent/turns", json=MESSAGE,
                       headers={"Idempotency-Key": "k"}).status_code == 401
    bad = {"Authorization": "Bearer nope", "Idempotency-Key": "k"}
    assert client.post("/v1/agent/turns", json=MESSAGE, headers=bad).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_a_turn_runs_frame_then_discovery_then_the_verbatim_block() -> None:
    runtime = build()
    response = TestClient(runtime.app).post("/v1/agent/turns", json=MESSAGE, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    block = body["discovery"]
    assert CANARY in block["text"]  # Google's text, verbatim and unedited
    assert len(block["places"]) == 3 and block["render_mode"] == "text"
    assert body["warnings"] == []
    entries = runtime.costs.entries()
    gateway_entries = [e for e in entries if e.kind == "gateway"]
    assert len(gateway_entries) == 1
    assert float(gateway_entries[0].estimated_cost_usd) == pytest.approx(
        250 * 0.20e-6 + 60 * 0.40e-6)
    assert sum(e.tool_calls for e in entries if e.kind == "tool") == 1
    assert runtime.kill_switch.tripped is False and runtime.quota_latch.tripped is False
    assert runtime.quotas.usage_for("guest-comp").successful_turns == 1


def test_identity_is_never_accepted_from_the_body() -> None:
    client = TestClient(build().app)
    body = {**MESSAGE, "uid": "someone-else", "kind": "REGISTERED_FREE"}
    assert client.post("/v1/agent/turns", json=body, headers=HEADERS).status_code == 422
    assert TOKEN not in str(client.app.openapi())


def test_module_level_main_app_is_unchanged() -> None:
    orchestrator = main.app.state.orchestrator
    assert main.app.state.verifier is None
    assert orchestrator._pipeline is None and orchestrator._dev_scaffold is False  # noqa: SLF001
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not modules & {"composition", "server"}


def test_the_production_entry_point_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match="verifier"):
        server.create_production_app()
