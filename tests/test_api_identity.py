from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import PrincipalKind
from app.entitlements import Allowance, EntitlementPolicy, Period, PolicyRegistry
from app.main import app as default_app
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.policy import OUT_OF_SCOPE_REPLY
from app.quota import QuotaService

TOKENS = {
    "tok-guest-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-guest-b": VerifiedIdentity("guest-b", "anonymous"),
    "tok-reg": VerifiedIdentity("reg-1", "google.com"),
    "tok-disabled": VerifiedIdentity("off-1", "google.com"),
}
TRAVEL = {"message": "Plan a trip to a Toronto restaurant"}


def small_registry() -> PolicyRegistry:
    policy = EntitlementPolicy(
        version="test-small",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        grace_rule="unknown-version-uses-current",
        allowances={
            PrincipalKind.GUEST: Allowance(2, 10, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(3, 10, Period.ROLLING_30_DAYS),
        },
    )
    return PolicyRegistry((policy,), policy.version)


def make(registry: PolicyRegistry | None = None) -> tuple[TestClient, QuotaService]:
    quotas = QuotaService(registry)
    application = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=quotas.policies, disabled_uids={"off-1"}),
        orchestrator=TurnOrchestrator(quotas),
    )
    return TestClient(application), quotas


def post(client, token=None, key="k1", body=TRAVEL, auth=None):
    headers = {"Idempotency-Key": key}
    if auth is not None:
        headers["Authorization"] = auth
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/v1/agent/turns", json=body, headers=headers)


@pytest.mark.parametrize("auth", [None, "Basic xyz", "Bearer ", "Bearer secret-unknown"])
def test_unauthenticated_requests_get_401(auth) -> None:
    client, quotas = make()
    response = post(client, auth=auth)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "secret-unknown" not in response.text and "xyz" not in response.text
    assert quotas._usage == {}


def test_default_app_fails_closed() -> None:
    assert post(TestClient(default_app), token="tok-guest-a").status_code == 401


@pytest.mark.parametrize(
    "extra", [{"principal_id": "reg-1"}, {"principal_kind": "REGISTERED_FREE"}]
)
def test_identity_in_body_is_rejected(extra) -> None:
    client, quotas = make()
    response = post(client, "tok-guest-a", body={**TRAVEL, **extra})
    assert response.status_code == 422
    assert quotas.usage_for("guest-a").successful_turns == 0


def test_tenant_isolation_and_server_derived_tier() -> None:
    client, _ = make(small_registry())
    first = post(client, "tok-guest-a", "a1")
    post(client, "tok-guest-a", "a2")
    assert post(client, "tok-guest-a", "a3").json()["allowance_exhausted"] is True
    other = post(client, "tok-guest-b", "b1")
    assert other.status_code == 200 and other.json()["allowance_exhausted"] is False
    assert other.json()["workspace_id"] != first.json()["workspace_id"]
    for index in range(3):
        assert post(client, "tok-reg", f"r{index}").json()["allowance_exhausted"] is False
    assert post(client, "tok-reg", "r4").json()["allowance_exhausted"] is True


def test_replayed_idempotency_key_counts_once_per_principal() -> None:
    client, quotas = make()
    post(client, "tok-guest-a", "same")
    post(client, "tok-guest-a", "same")
    post(client, "tok-guest-b", "same")
    assert quotas.usage_for("guest-a").successful_turns == 1
    assert quotas.usage_for("guest-b").successful_turns == 1


def test_disabled_principal_gets_403_without_tools_or_workspace() -> None:
    client, quotas = make()
    response = post(client, "tok-disabled")
    assert response.status_code == 403
    assert "allowed_tools" not in response.json() and "workspace_id" not in response.json()
    assert quotas.usage_for("off-1").successful_turns == 0


def test_out_of_scope_turn_has_no_tools_and_no_commit() -> None:
    client, quotas = make()
    response = post(client, "tok-guest-a", body={"message": "Write Python code"})
    assert response.status_code == 200
    assert response.json()["reply"] == OUT_OF_SCOPE_REPLY
    assert response.json()["allowed_tools"] == []
    assert quotas.usage_for("guest-a").successful_turns == 0
