import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain import Principal, PrincipalKind
from app.entitlements import Allowance, EntitlementPolicy, Period, PolicyRegistry, default_registry
from app.quota import QuotaService

VERSION = default_registry().current().version
GUEST = Principal("guest", PrincipalKind.GUEST, VERSION)


def small_registry(guest_turns: int) -> PolicyRegistry:
    policy = EntitlementPolicy(
        version="test-small",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        grace_rule="unknown-version-uses-current",
        allowances={PrincipalKind.GUEST: Allowance(guest_turns, 10, Period.LIFETIME)},
    )
    return PolicyRegistry((policy,), policy.version)


def test_turn_commit_is_idempotent() -> None:
    quotas = QuotaService()
    quotas.commit_turn(GUEST, "request-1")
    usage = quotas.commit_turn(GUEST, "request-1")
    assert usage.successful_turns == 1


def test_guest_turn_allowance_is_enforced_from_policy() -> None:
    quotas = QuotaService()
    limit = quotas.policies.for_kind(PrincipalKind.GUEST).user_turn_limit
    for index in range(limit):
        quotas.commit_turn(GUEST, f"request-{index}")
    with pytest.raises(PermissionError):
        quotas.commit_turn(GUEST, "request-over")


def test_registered_free_gets_policy_allowance() -> None:
    quotas = QuotaService()
    registered = Principal("reg", PrincipalKind.REGISTERED_FREE, VERSION)
    for index in range(100):
        quotas.commit_turn(registered, f"request-{index}")
    with pytest.raises(PermissionError):
        quotas.commit_turn(registered, "request-over")


def test_injected_policy_changes_enforcement() -> None:
    quotas = QuotaService(small_registry(2))
    quotas.commit_turn(GUEST, "a")
    quotas.commit_turn(GUEST, "b")
    with pytest.raises(PermissionError):
        quotas.commit_turn(GUEST, "c")


def test_disabled_and_unentitled_kinds_are_denied() -> None:
    with pytest.raises(PermissionError):
        QuotaService().commit_turn(Principal("off", PrincipalKind.DISABLED, VERSION), "a")
    guest_only = QuotaService(small_registry(5))
    assert not guest_only.allowed(Principal("r", PrincipalKind.REGISTERED_FREE, "test-small"))


def test_quota_module_has_no_allowance_literals() -> None:
    source = (Path(__file__).parents[1] / "app" / "quota.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(10|50|100)\b", source)
