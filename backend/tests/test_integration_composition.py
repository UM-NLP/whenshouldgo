"""The composition root wires every service: identity, App Check, places (off), actions, workspace,
account, the shared workspace lock, the deleters and export contributors, and the deleted-account
guard for in-flight writers. Synthetic only: no network, no keys, no Firebase."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest
from composition_support import ENV, build
from fastapi.testclient import TestClient

from app import composition
from app.account import AdminAccount
from app.actions import ActionError
from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.composition import ConfigurationError, firebase_identity_from_env
from app.domain import (
    FactEdit,
    Principal,
    PrincipalKind,
    ProposeActionRequest,
    WorkspaceEditRequest,
)
from app.failures import TurnFailure
from app.stores.fake_firestore import FakeFirestoreClient
from app.workspaces import WorkspaceUnavailable

PLACE = "ChIJplaceA_-1"
BUDGET = composition.SpendBudget(max_tokens=10_000_000)
TOKENS = {
    "tok-g": VerifiedIdentity("guest-w", "anonymous"),
    "tok-r": VerifiedIdentity("reg-w", "google.com"),
}


def headers(token: str = "tok-g", **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def runtime_for(kind: str, **overrides):
    clock = {"now": datetime(2026, 10, 26, 12, 0, tzinfo=UTC)}
    kwargs = dict(identity_verifier=StaticTokenVerifier(TOKENS), clock=lambda: clock["now"])
    if kind == "firestore":
        kwargs.update(firestore_client=FakeFirestoreClient(), spend_budget=BUDGET,
                      allow_in_memory_state=False)
    kwargs.update(overrides)
    runtime = build(**kwargs)
    return runtime, TestClient(runtime.app), clock


# -- identity and fail-closed production ----------------------------------------------------------
def test_firebase_identity_needs_a_project_id() -> None:
    with pytest.raises(ConfigurationError, match="FIREBASE_PROJECT_ID"):
        firebase_identity_from_env({})
    with pytest.raises(ConfigurationError, match="FIREBASE_PROJECT_ID"):
        firebase_identity_from_env({"FIREBASE_PROJECT_ID": "  "})


def test_firebase_identity_shares_the_disabled_set_with_the_resolver() -> None:
    verifier, disabled = firebase_identity_from_env({"FIREBASE_PROJECT_ID": "demo-project-1"})
    assert verifier._disabled is disabled  # noqa: SLF001 - the same set object
    runtime = build(identity_verifier=verifier, disabled_uids=disabled)
    assert runtime.app.state.resolver.disabled_uids is disabled


def test_a_missing_auth_extra_fails_closed_with_static_text(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "app.auth_firebase", None)  # makes the import raise
    with pytest.raises(ConfigurationError, match="auth extra"):
        firebase_identity_from_env({"FIREBASE_PROJECT_ID": "demo-project-1"})


def test_production_needs_state_even_with_an_identity(monkeypatch) -> None:
    from app import server

    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", "demo-project-1")
    monkeypatch.delenv("FIRESTORE_PROJECT_ID", raising=False)
    with pytest.raises(ConfigurationError, match="shared state"):
        server.create_production_app()


# -- App Check ------------------------------------------------------------------------------------
def test_app_check_is_off_by_default_and_refuses_a_bad_setting() -> None:
    assert build().app.state.app_check_policy is None
    with pytest.raises(ConfigurationError, match="APP_CHECK_ENFORCE"):
        build(env={**ENV, "APP_CHECK_ENFORCE": "yes"})
    with pytest.raises(ConfigurationError, match="FIREBASE_PROJECT_NUMBER"):
        build(env={**ENV, "APP_CHECK_ENFORCE": "true"})  # enforcing with no verifier


class StubAppCheckVerifier:
    def verify(self, token: str):
        from app.appcheck import AppCheckError, AppCheckResult

        if token != "good-app-check-token":
            raise AppCheckError("signature")
        return AppCheckResult(app_id="app")


def test_enforced_app_check_gates_every_route_and_monitor_mode_never_blocks() -> None:
    enforced = build(env={**ENV, "APP_CHECK_ENFORCE": "true"},
                     identity_verifier=StaticTokenVerifier(TOKENS),
                     app_check_verifier=StubAppCheckVerifier())
    http = TestClient(enforced.app)
    assert http.get("/v1/me/usage", headers=headers()).status_code == 401  # no App Check header
    bad = http.get("/v1/me/usage", headers=headers(**{"X-Firebase-AppCheck": "nope"}))
    assert bad.status_code == 401
    good = http.get("/v1/me/usage", headers=headers(**{"X-Firebase-AppCheck": "good-app-check-token"}))
    assert good.status_code == 200
    assert http.get("/healthz").status_code == 200  # the health check is outside the gate

    monitor = TestClient(build(identity_verifier=StaticTokenVerifier(TOKENS),
                               app_check_verifier=StubAppCheckVerifier()).app)
    assert monitor.get("/v1/me/usage", headers=headers()).status_code == 200  # recorded, not blocked


# -- the wired services ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_every_service_is_attached_and_the_routes_are_live(kind: str) -> None:
    runtime, http, _ = runtime_for(kind)
    state = runtime.app.state
    assert state.workspace_service is runtime.workspace_service is not None
    assert state.action_service is runtime.action_service is not None
    assert state.account_service is runtime.account_service is not None
    assert http.get("/v1/workspace", headers=headers()).status_code == 200
    assert http.post("/v1/me:export", headers=headers()).status_code == 200
    capabilities = http.get("/v1/me/capabilities", headers=headers()).json()
    assert capabilities["can_export"] is True and capabilities["can_delete"] is True
    assert capabilities["history_enabled"] is False  # dark by default


def test_places_is_built_but_off_while_the_price_table_is_unreviewed() -> None:
    runtime, http, _ = runtime_for("memory")
    assert runtime.places_service.enabled is False
    response = http.post("/v1/places/details", headers=headers(), json={"place_ids": [PLACE]})
    assert response.status_code in (503, 404) and response.json()["code"] in ("PROVIDER", "VALIDATION")


def test_the_workspace_service_uses_the_orchestrators_lock() -> None:
    runtime, _, _ = runtime_for("memory")
    assert runtime.workspace_service._lock == runtime.orchestrator.workspace_lock  # noqa: SLF001


def test_bootstrap_reconciles_a_registered_token_with_no_account_record() -> None:
    class Admin:
        def get_account(self, uid: str):
            return AdminAccount(uid, frozenset({"google.com"}))

        def delete_user(self, uid: str) -> None:
            return None

    runtime, http, _ = runtime_for("memory", firebase_admin=Admin())
    assert runtime.account_service.store.get_account("reg-w") is None
    assert http.post("/v1/session:bootstrap", headers=headers("tok-r")).status_code == 200
    assert runtime.account_service.store.get_account("reg-w") is not None


def test_purge_expired_runs_both_sweeps() -> None:
    runtime, _, _ = runtime_for("memory")
    assert runtime.purge_expired() == {"pending_actions": 0, "display_cache": 0, "history": 0}


# -- delete and export cascade across every store -------------------------------------------------
def seed(http: TestClient, clock: dict) -> str:
    edit = {"base_version": 0, "upsert_facts": [
        {"key": "selected_place_id", "value": PLACE}, {"key": "selected_place_label", "value": "Cafe"}]}
    done = http.patch("/v1/workspace", json=edit,
                      headers=headers(**{"Idempotency-Key": "edit-key-0001"}))
    assert done.status_code == 200 and done.json()["plan_versions"] == [1]
    workspace_id = done.json()["id"]
    body = {"workspace_id": workspace_id, "plan_version": 1, "effect": "LAUNCH_HANDOFF",
            "target": PLACE}
    action = http.post("/v1/actions/propose", json=body,
                       headers=headers(**{"Idempotency-Key": "act-key-00001"})).json()
    ok = http.post(f"/v1/actions/{action['id']}/confirm", json={"nonce": action["nonce"]},
                   headers=headers())
    assert ok.status_code == 200 and ok.json()["handoff_url"]
    return workspace_id


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_export_carries_plan_versions_and_delete_cascades_to_every_store(kind: str) -> None:
    runtime, http, clock = runtime_for(kind)
    workspace_id = seed(http, clock)
    exported = http.post("/v1/me:export", headers=headers()).json()
    assert [p["version"] for p in exported["plan_versions"]] == [1]
    assert exported["workspaces"][0]["id"] == workspace_id and exported["tier"] == "guest"
    assert PLACE not in str(exported["consents"])

    receipt = http.post("/v1/me:delete", headers=headers()).json()
    assert receipt["completed"] is True
    now = clock["now"]
    uid = "guest-w"
    assert runtime.stores.workspaces.get(uid, "default", now) is None
    assert runtime.plan_stores.plans.history(uid, workspace_id, now) == ()
    assert runtime.plan_stores.receipts.get(uid, "edit-key-0001", now) is None
    assert runtime.plan_stores.outcomes.entries(uid, now) == ()
    assert runtime.action_service._store.get(uid, _first_action(runtime, uid)) is None  # noqa: SLF001
    if kind == "firestore":
        client = runtime.stores.client
        leftovers = [path for path, doc in client.documents()
                     if doc.get("owner_id") == uid and not path.startswith("account_tombstones")]
        assert leftovers == []
    # the tombstone refuses the uid at the route
    assert http.get("/v1/workspace", headers=headers()).status_code == 403


def _first_action(runtime, uid: str) -> str:
    from app.actions import _action_id

    return _action_id(uid, "act-key-00001")


# -- in-flight writers after a delete -------------------------------------------------------------
@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_late_writers_write_nothing_for_a_deleted_account(kind: str) -> None:
    runtime, http, clock = runtime_for(kind)
    seed(http, clock)
    assert http.post("/v1/me:delete", headers=headers()).json()["completed"] is True
    now = clock["now"]
    principal = Principal("guest-w", PrincipalKind.GUEST, runtime.quotas.policies.current().version)

    with pytest.raises(TurnFailure) as failure:  # a turn that was in flight when the delete ran
        runtime.orchestrator.run_turn(principal, "plan a picnic", "late-turn-key-1")
    assert failure.value.status == 403
    with pytest.raises(WorkspaceUnavailable):
        runtime.workspace_service.edit(
            principal, WorkspaceEditRequest(
                base_version=0, upsert_facts=(FactEdit(key="late", value="x"),)), "late-edit-key-1")
    with pytest.raises(ActionError):
        runtime.action_service.propose(
            principal, ProposeActionRequest(
                workspace_id="w", plan_version=1, effect="LAUNCH_HANDOFF", target=PLACE),
            "late-act-key-1")
    assert runtime.stores.workspaces.get("guest-w", "default", now) is None
    assert runtime.stores.idempotency.get("guest-w", "late-turn-key-1", now) is None


def test_a_deletion_that_lands_mid_turn_takes_the_turns_writes_back() -> None:
    runtime, http, clock = runtime_for("memory")
    http.get("/v1/workspace", headers=headers())  # the account exists
    principal = Principal("guest-w", PrincipalKind.GUEST, runtime.quotas.policies.current().version)
    deleted = {"now": False}
    original = runtime.stores.workspaces.put_if_version

    def put_then_delete(*args, **kwargs):
        wrote = original(*args, **kwargs)  # the turn's write lands ...
        deleted["now"] = True  # ... and the account is deleted right after it
        return wrote

    runtime.stores.workspaces.put_if_version = put_then_delete  # type: ignore[method-assign]
    runtime.orchestrator._account_guard = (  # noqa: SLF001
        lambda uid: (_ for _ in ()).throw(PermissionError()) if deleted["now"] else None)
    with pytest.raises(TurnFailure) as failure:
        runtime.orchestrator.run_turn(principal, "plan a picnic", "mid-turn-key-1")
    assert failure.value.status == 403
    now = clock["now"] + timedelta(seconds=1)
    assert runtime.stores.workspaces.get("guest-w", "default", now) is None
    assert runtime.stores.idempotency.get("guest-w", "mid-turn-key-1", now) is None
