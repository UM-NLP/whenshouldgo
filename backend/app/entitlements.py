"""Versioned server policy: allowances (docs/access-tiers.md) and token ceilings
(docs/voice-and-domain-controls.md, D10).

Allowances and budgets are server-side data, never client input. Changing a limit means adding a
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
class TokenPolicy:
    max_user_message_tokens: int = 2_000
    classifier_input_tokens: int = 2_048
    classifier_output_tokens: int = 64
    planner_call_input_tokens: int = 12_000
    planner_call_output_tokens: int = 1_200
    run_input_tokens: int = 40_000
    run_output_tokens: int = 4_000
    run_model_calls: int = 6
    run_tool_calls: int = 12
    run_escalations: int = 1
    visible_reply_tokens: int = 450
    spoken_reply_tokens: int = 160
    run_deadline_seconds: int = 20


@dataclass(frozen=True)
class DailyTokenCeiling:
    input_tokens: int
    output_tokens: int


DEFAULT_DAILY_TOKENS: Mapping[PrincipalKind, DailyTokenCeiling] = MappingProxyType(
    {
        PrincipalKind.GUEST: DailyTokenCeiling(60_000, 6_000),
        PrincipalKind.REGISTERED_FREE: DailyTokenCeiling(120_000, 12_000),
    }
)


@dataclass(frozen=True)
class EntitlementPolicy:
    version: str
    effective_at: datetime
    grace_rule: str
    allowances: Mapping[PrincipalKind, Allowance] = field(default_factory=dict)
    tokens: TokenPolicy = TokenPolicy()
    daily_tokens: Mapping[PrincipalKind, DailyTokenCeiling] = DEFAULT_DAILY_TOKENS
    guest_workspace_retention_days: int = 7
    notice_quiet_meter_ratio: float = 0.7
    notice_benefit_ratio: float = 0.9

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
