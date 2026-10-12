"""Versioned entitlement policy (docs/access-tiers.md, EntitlementPolicy).

Allowances are server-side data, never client input. Changing a limit means adding a
new policy version here (later: loading it from server configuration), not editing
handlers or QuotaService.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from .domain import PrincipalKind


class Period(StrEnum):
    LIFETIME = "LIFETIME"
    ROLLING_30_DAYS = "ROLLING_30_DAYS"


@dataclass(frozen=True)
class Allowance:
    user_turn_limit: int
    activated_trip_limit: int
    period: Period


@dataclass(frozen=True)
class EntitlementPolicy:
    version: str
    effective_at: datetime
    grace_rule: str
    allowances: Mapping[PrincipalKind, Allowance] = field(default_factory=dict)

    def for_kind(self, kind: PrincipalKind) -> Allowance | None:
        """Return the allowance for a tier; None means no entitlement (always denied)."""
        return self.allowances.get(kind)


PILOT_POLICY = EntitlementPolicy(
    version="2026-10-11.pilot-1",
    effective_at=datetime(2026, 10, 11, tzinfo=UTC),
    grace_rule="unknown-version-uses-current",
    allowances=MappingProxyType(
        {
            PrincipalKind.GUEST: Allowance(50, 10, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(100, 10, Period.ROLLING_30_DAYS),
            # DISABLED intentionally has no allowance.
        }
    ),
)


class PolicyRegistry:
    """Holds every known policy version and the current one.

    Grace rule (minimal, Phase 0): a principal keeps the policy version recorded for
    it; an unknown or missing version resolves to the current policy. A full
    per-session grace rule needs a persisted policyVersion from session:bootstrap.
    """

    def __init__(self, policies: tuple[EntitlementPolicy, ...], current_version: str) -> None:
        self._policies = {policy.version: policy for policy in policies}
        if current_version not in self._policies:
            raise ValueError("current policy version is not registered")
        self._current = current_version

    def current(self) -> EntitlementPolicy:
        return self._policies[self._current]

    def get(self, version: str | None) -> EntitlementPolicy:
        if version is None:
            return self.current()
        return self._policies.get(version) or self.current()

    def for_kind(self, kind: PrincipalKind, version: str | None = None) -> Allowance | None:
        return self.get(version).for_kind(kind)


def default_registry() -> PolicyRegistry:
    return PolicyRegistry((PILOT_POLICY,), PILOT_POLICY.version)
