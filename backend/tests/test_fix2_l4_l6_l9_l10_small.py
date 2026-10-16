"""L4 (the APP_HELP responder makes no paid call), L6 (identifiers are validated before any paid
work), L9 (key comparisons are case-folded), L10 (no bare asserts guard invariants)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from maps_simulator import SyntheticMapsClient
from test_gateway_support import make_config
from wiring_support import Env, RecordingClient, ScriptedGateway, fact, frame_turn

from app.domain import (
    Confirmation,
    Fact,
    FactSource,
    NextAction,
    Principal,
    PrincipalKind,
    TripWorkspace,
    WorkspacePatch,
)
from app.failures import TurnFailure
from app.patching import PatchRejected, apply_patch

APP = Path(__file__).resolve().parents[1] / "app"


# --- L4 ---------------------------------------------------------------------------------------
def test_app_help_with_a_gateway_makes_no_paid_call_and_stores_no_goal() -> None:
    config = make_config()
    gateway = ScriptedGateway([], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    response = env.post({"message": "How does this app handle my privacy?"})
    assert response.status_code == 200 and response.json()["domain"] == "APP_HELP"
    assert gateway.requests == [] and env.costs.entries() == ()
    assert env.orchestrator.workspaces[("guest-a", "default")].goal is None


def test_the_app_help_branch_is_named_for_what_it_is() -> None:
    from app.orchestrator import TurnOrchestrator

    assert hasattr(TurnOrchestrator, "_app_help_workspace")
    assert "no paid call" in (TurnOrchestrator._app_help_workspace.__doc__ or "").lower()


# --- L6 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("uid", ["bad uid with spaces", "x" * 129, "emoji\U0001f600id", "a/b"])
def test_a_malformed_principal_id_is_refused_before_any_paid_work(uid) -> None:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config)], config)
    env = Env(gateway=gateway, discovery=RecordingClient(SyntheticMapsClient()), config=config)
    guest = Principal(uid, PrincipalKind.GUEST, env.quotas.policies.current().version)
    with pytest.raises(TurnFailure) as caught:
        env.orchestrator.run_turn(guest, "Plan a quiet Toronto restaurant dinner", "k")
    assert caught.value.status in (500, 503)
    assert gateway.requests == [], "FRAME was paid for before the identifier was checked"
    assert env.costs.entries() == () and env.quotas.usage_for(uid).reserved_keys == set()


# --- L9 ---------------------------------------------------------------------------------------
def test_key_comparisons_are_case_folded() -> None:
    user = Fact(id="user-1", key="Budget", value="under 50", source=FactSource.USER_EXPLICIT,
                confirmation=Confirmation.CONFIRMED, hard=True, scope="trip")
    base = TripWorkspace(id="ws-1", owner_id="u", version=1, facts=[user])

    def patch(*facts):
        return WorkspacePatch(workspace_id="ws-1", base_version=1, next_action=NextAction.SEARCH,
                              upsert_facts=list(facts))

    for shadow in (fact("budget", "free"), fact("BUDGET", "free")):
        with pytest.raises(PatchRejected):
            apply_patch(base, patch(shadow))
    with pytest.raises(PatchRejected):
        apply_patch(TripWorkspace(id="ws-1", owner_id="u", version=1), patch(
            fact("Planning_Area", "Paris")))
    first = fact("Party", "two")
    with pytest.raises(PatchRejected):  # two spellings of one key in one patch
        apply_patch(TripWorkspace(id="ws-1", owner_id="u", version=1),
                    patch(first, fact("party", "three")))


# --- L10 --------------------------------------------------------------------------------------
def test_pipeline_has_no_bare_assert_guarding_an_invariant() -> None:
    tree = ast.parse((APP / "pipeline.py").read_text(encoding="utf-8"))
    asserts = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    assert asserts == [], f"bare asserts at lines {asserts}"
