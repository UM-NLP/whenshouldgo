"""Identity, tenant and quota boundary tests (evaluation role).

Deterministic and offline: synthetic tokens and uids, no network, no credentials.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import Principal, PrincipalKind
from app.entitlements import Allowance, EntitlementPolicy, Period, PolicyRegistry
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.guard import OUT_OF_SCOPE_REPLY, SAFETY_REPLY
from app.quota import QuotaService

TOKENS = {
    "tok-guest-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-guest-b": VerifiedIdentity("guest-b", "anonymous"),
    "tok-reg": VerifiedIdentity("reg-1", "google.com"),
}
TRAVEL = {"message": "Plan a trip to a Toronto restaurant"}


def registry(guest_turns: int = 2, registered_turns: int = 3) -> PolicyRegistry:
    policy = EntitlementPolicy(
        version="test-boundary",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        grace_rule="unknown-version-uses-current",
        allowances={
            PrincipalKind.GUEST: Allowance(guest_turns, 10, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(registered_turns, 10, Period.ROLLING_30_DAYS),
        },
    )
    return PolicyRegistry((policy,), policy.version)


def make(reg: PolicyRegistry | None = None) -> tuple[TestClient, TurnOrchestrator]:
    orchestrator = TurnOrchestrator(QuotaService(reg or registry()), dev_scaffold=True)
    application = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=orchestrator.quotas.policies),
        orchestrator=orchestrator,
    )
    return TestClient(application), orchestrator


def post(client, token="tok-guest-a", key="k1", body=TRAVEL, headers=None, params=None):
    sent = {"Idempotency-Key": key, "Authorization": f"Bearer {token}", **(headers or {})}
    return client.post("/v1/agent/turns", json=body, headers=sent, params=params)


def turns(orchestrator: TurnOrchestrator, uid: str) -> int:
    return orchestrator.quotas.usage_for(uid).successful_turns


# --- Body / query / header supplied identity -------------------------------------

@pytest.mark.parametrize(
    "extra",
    [
        {"uid": "reg-1"},
        {"user_id": "reg-1"},
        {"kind": "REGISTERED_FREE"},
        {"tier": "REGISTERED_FREE"},
        {"policy_version": "unlimited"},
        {"principal": {"uid": "reg-1", "kind": "REGISTERED_FREE"}},
    ],
)
def test_any_identity_like_body_field_is_rejected_without_commit(extra) -> None:
    client, orch = make()
    response = post(client, body={**TRAVEL, **extra})
    assert response.status_code == 422
    assert orch.quotas._usage == {}
    assert orch.workspaces == {}


def test_identity_in_query_or_headers_is_ignored() -> None:
    client, orch = make(registry(guest_turns=1, registered_turns=5))
    spoof_headers = {"X-Principal-Id": "reg-1", "X-Principal-Kind": "REGISTERED_FREE"}
    spoof_query = {"principal_id": "reg-1", "principal_kind": "REGISTERED_FREE"}
    first = post(client, key="a1", headers=spoof_headers, params=spoof_query)
    second = post(client, key="a2", headers=spoof_headers, params=spoof_query)
    assert first.status_code == second.status_code == 200
    assert first.json()["allowance_exhausted"] is False
    # Guest limit (1) applies, not the registered limit (5).
    assert second.json()["allowance_exhausted"] is True
    assert turns(orch, "guest-a") == 1
    assert turns(orch, "reg-1") == 0


# --- Missing / invalid token -----------------------------------------------------

@pytest.mark.parametrize(
    "authorization",
    [
        "tok-guest-a",  # no scheme
        "Token tok-guest-a",  # wrong scheme
        "Bearer",  # no credential
        "Bearer    ",
        "Bearer tok-guest-a-forged",  # near-miss of a known token
        "Bearer TOK-GUEST-A",  # case altered
        "Basic dG9rLWd1ZXN0LWE6",
    ],
)
def test_malformed_or_unknown_authorization_is_401_without_side_effects(authorization) -> None:
    client, orch = make()
    response = client.post(
        "/v1/agent/turns",
        json=TRAVEL,
        headers={"Idempotency-Key": "k1", "Authorization": authorization},
    )
    assert response.status_code == 401
    assert response.json() == {
        "schema_version": 1,
        "kind": "error",
        "code": "POLICY",
        "message": "authentication required",
    }
    assert "tok-guest-a" not in response.text.lower()
    assert orch.quotas._usage == {}
    assert orch.workspaces == {}


def test_rejected_token_is_not_logged(caplog) -> None:
    client, _ = make()
    with caplog.at_level(logging.DEBUG):
        post(client, token="canary-token-7f3a")
    assert "canary-token-7f3a" not in caplog.text


def test_missing_idempotency_key_commits_nothing() -> None:
    client, orch = make()
    response = client.post(
        "/v1/agent/turns", json=TRAVEL, headers={"Authorization": "Bearer tok-guest-a"}
    )
    assert response.status_code == 422
    assert orch.quotas._usage == {}


# --- Cross-tenant isolation ------------------------------------------------------

def test_workspace_is_per_principal_and_reused_only_by_owner() -> None:
    client, orch = make(registry(guest_turns=5))
    a1 = post(client, "tok-guest-a", "a1").json()
    b1 = post(client, "tok-guest-b", "b1").json()
    a2 = post(client, "tok-guest-a", "a2", body={"message": "Find an outing in the GTA"}).json()
    assert a1["workspace_id"] == a2["workspace_id"]
    assert a1["workspace_id"] != b1["workspace_id"]
    owners = {ws.id: ws.owner_id for ws in orch.workspaces.values()}
    assert owners[a1["workspace_id"]] == "guest-a"
    assert owners[b1["workspace_id"]] == "guest-b"
    # guest-a's later message does not leak into guest-b's workspace.
    assert orch.workspaces[("guest-b", "default")].goal == TRAVEL["message"]


def test_exhausted_tenant_does_not_affect_other_tiers_or_tenants() -> None:
    client, orch = make(registry(guest_turns=1, registered_turns=1))
    post(client, "tok-guest-a", "x")
    assert post(client, "tok-guest-a", "y").json()["allowance_exhausted"] is True
    assert post(client, "tok-guest-b", "x").json()["allowance_exhausted"] is False
    assert post(client, "tok-reg", "x").json()["allowance_exhausted"] is False
    assert (turns(orch, "guest-a"), turns(orch, "guest-b"), turns(orch, "reg-1")) == (1, 1, 1)


# --- Allowances read from policy -------------------------------------------------

@pytest.mark.parametrize("limit", [1, 3, 7])
def test_api_enforces_whatever_limit_the_policy_holds(limit) -> None:
    client, orch = make(registry(guest_turns=limit))
    results = [post(client, key=f"k{i}").json()["allowance_exhausted"] for i in range(limit + 1)]
    assert results == [False] * limit + [True]
    assert turns(orch, "guest-a") == limit


def test_unknown_principal_policy_version_uses_current_limits() -> None:
    quotas = QuotaService(registry(guest_turns=1))
    principal = Principal("g", PrincipalKind.GUEST, "client-claimed-unlimited")
    quotas.commit_turn(principal, "a")
    with pytest.raises(PermissionError):
        quotas.commit_turn(principal, "b")


def test_response_policy_version_is_server_registry_version() -> None:
    client, _ = make()
    assert post(client).json()["policy_version"] == "test-boundary"


# --- Duplicate idempotency keys --------------------------------------------------

def test_duplicate_key_counts_once_and_keeps_same_workspace() -> None:
    client, orch = make(registry(guest_turns=5))
    first = post(client, key="dup").json()
    second = post(client, key="dup").json()
    assert turns(orch, "guest-a") == 1
    assert first["workspace_id"] == second["workspace_id"]


def test_duplicate_key_replays_the_stored_response_without_rerunning() -> None:
    client, orch = make(registry(guest_turns=5))
    first = post(client, key="dup").json()
    workspace = orch.workspaces[("guest-a", "default")]
    second = post(client, key="dup").json()
    assert second == first
    assert orch.workspaces[("guest-a", "default")] is workspace
    assert workspace.version == 1


def test_duplicate_key_replays_even_after_allowance_is_exhausted() -> None:
    client, orch = make(registry(guest_turns=1))
    first = post(client, key="only").json()
    assert post(client, key="later").json()["allowance_exhausted"] is True
    assert post(client, key="only").json() == first
    assert turns(orch, "guest-a") == 1


def test_duplicate_key_with_different_body_is_rejected_without_side_effects() -> None:
    client, orch = make(registry(guest_turns=5))
    post(client, key="dup")
    workspace = orch.workspaces[("guest-a", "default")]
    response = post(client, key="dup", body={"message": "Plan a different Toronto trip"})
    assert response.status_code == 409  # was 422; now a typed CONFLICT
    assert orch.workspaces[("guest-a", "default")] is workspace
    assert turns(orch, "guest-a") == 1


def test_over_length_message_is_not_labelled_out_of_scope() -> None:
    client, _ = make()
    payload = post(client, body={"message": "trip " * 2_100}).json()
    assert payload["domain"] is None and payload["workspace_id"] is None


def test_duplicate_key_after_exhaustion_does_not_raise_or_increment() -> None:
    quotas = QuotaService(registry(guest_turns=1))
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")
    quotas.commit_turn(guest, "only")
    assert quotas.commit_turn(guest, "only").successful_turns == 1


# --- Quota race ------------------------------------------------------------------

def _run_parallel(count: int, target) -> None:
    barrier = threading.Barrier(count)

    def worker(index: int) -> None:
        barrier.wait()
        target(index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)


def test_concurrent_distinct_keys_never_exceed_policy_limit() -> None:
    quotas = QuotaService(registry(guest_turns=5))
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        try:
            quotas.commit_turn(guest, f"key-{index}")
            result = "ok"
        except PermissionError:
            result = "denied"
        with lock:
            outcomes.append(result)

    _run_parallel(40, attempt)
    assert outcomes.count("ok") == 5
    assert outcomes.count("denied") == 35
    assert quotas.usage_for("g").successful_turns == 5


def test_concurrent_same_key_counts_once() -> None:
    quotas = QuotaService(registry(guest_turns=5))
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")
    _run_parallel(25, lambda _: quotas.commit_turn(guest, "same"))
    assert quotas.usage_for("g").successful_turns == 1


def test_check_then_commit_race_through_orchestrator_does_not_overspend() -> None:
    """Both requests pass allowed() before either commits; only one may commit."""
    quotas = QuotaService(registry(guest_turns=1))
    orch = TurnOrchestrator(quotas, dev_scaffold=True)
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")
    barrier = threading.Barrier(2)
    original_allowed = quotas.allowed
    local = threading.local()

    def synchronized_allowed(principal):
        result = original_allowed(principal)
        if not getattr(local, "prechecked", False):
            # Only the orchestrator's pre-check waits; commit_turn's own check does not.
            local.prechecked = True
            barrier.wait(timeout=5)
        return result

    quotas.allowed = synchronized_allowed  # type: ignore[method-assign]
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        turn = orch.run_turn(guest, f"Plan a Toronto outing number {index}", f"k{index}")
        result = "denied" if turn.allowance_exhausted else f"ok{index}"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(outcomes) == 2 and outcomes.count("denied") == 1
    winner = next(o for o in outcomes if o.startswith("ok"))[2:]
    assert quotas.usage_for("g").successful_turns == 1
    assert quotas.usage_for("g").reserved_keys == set()
    # The denied request must not have written its message into the workspace.
    assert list(orch.workspaces) == [("g", "default")]
    assert orch.workspaces[("g", "default")].goal == f"Plan a Toronto outing number {winner}"


def test_denied_reservation_leaves_existing_workspace_unchanged() -> None:
    quotas = QuotaService(registry(guest_turns=1))
    orch = TurnOrchestrator(quotas, dev_scaffold=True)
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")
    orch.run_turn(guest, "Plan a Toronto outing", "first")
    workspace = orch.workspaces[("g", "default")]
    snapshot = (workspace.goal, workspace.status, list(workspace.assumptions))
    quotas.allowed = lambda principal: True  # type: ignore[method-assign]  # stale pre-check
    denied = orch.run_turn(guest, "Plan a different Toronto outing", "second")
    assert denied.allowance_exhausted and denied.workspace is None and denied.tools == ()
    after = orch.workspaces[("g", "default")]
    assert after is workspace
    assert (after.goal, after.status, list(after.assumptions)) == snapshot


def test_failed_commit_releases_reservation_and_keeps_workspace() -> None:
    quotas = QuotaService(registry(guest_turns=1))
    orch = TurnOrchestrator(quotas, dev_scaffold=True)
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")

    def failing_commit(reservation):
        raise RuntimeError("store unavailable")

    original_commit = quotas.commit
    quotas.commit = failing_commit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        orch.run_turn(guest, "Plan a Toronto outing", "k")
    assert orch.workspaces == {}
    assert quotas.usage_for("g").reserved_keys == set()
    assert quotas.usage_for("g").successful_turns == 0
    quotas.commit = original_commit  # type: ignore[method-assign]
    assert orch.run_turn(guest, "Plan a Toronto outing", "k").workspace is not None
    assert quotas.usage_for("g").successful_turns == 1


def test_concurrent_first_turns_share_one_workspace() -> None:
    quotas = QuotaService(registry(guest_turns=50))
    orch = TurnOrchestrator(quotas, dev_scaffold=True)
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")
    ids: list[str] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        result = orch.run_turn(guest, "Plan a Toronto outing", f"k{index}")
        with lock:
            ids.append(result.workspace.id)

    _run_parallel(20, attempt)
    assert len(ids) == 20 and len(set(ids)) == 1
    assert quotas.usage_for("g").successful_turns == 20


def test_in_flight_duplicate_key_conflicts_without_counting() -> None:
    from app.quota import ReservationConflict

    quotas = QuotaService(registry(guest_turns=5))
    guest = Principal("g", PrincipalKind.GUEST, "test-boundary")
    held = quotas.reserve(guest, "same")
    with pytest.raises(ReservationConflict):
        quotas.reserve(guest, "same")
    quotas.release(held)
    assert quotas.usage_for("g").successful_turns == 0
    assert quotas.reserve(guest, "same").duplicate is False


# --- Rejected turns do not consume quota -----------------------------------------

@pytest.mark.parametrize(
    ("message", "expected_reply"),
    [
        ("Write Python code", OUT_OF_SCOPE_REPLY),
        ("Tell me a joke about cats", OUT_OF_SCOPE_REPLY),
        ("This is an emergency", SAFETY_REPLY),
        ("trip " * 2_100, None),  # over the 2,000-token message budget
    ],
)
def test_rejected_turns_consume_no_quota_and_create_no_workspace(message, expected_reply) -> None:
    client, orch = make(registry(guest_turns=1))
    response = post(client, body={"message": message})
    assert response.status_code == 200
    payload = response.json()
    if expected_reply is not None:
        assert payload["reply"] == expected_reply
    assert payload["allowed_tools"] == []
    assert payload["workspace_id"] is None
    assert orch.quotas._usage.get("guest-a") is None or turns(orch, "guest-a") == 0
    # The single policy turn is still available for a real travel request.
    assert post(client, key="after").json()["allowance_exhausted"] is False


def test_exhausted_travel_turn_gets_no_tools_and_no_workspace_change() -> None:
    client, orch = make(registry(guest_turns=1))
    post(client, key="first")
    before = orch.workspaces[("guest-a", "default")].goal
    response = post(client, key="second", body={"message": "Now plan a different Toronto trip"})
    payload = response.json()
    assert payload["allowance_exhausted"] is True
    assert payload["allowed_tools"] == []
    assert payload["workspace_id"] is None
    assert orch.workspaces[("guest-a", "default")].goal == before
    assert turns(orch, "guest-a") == 1
