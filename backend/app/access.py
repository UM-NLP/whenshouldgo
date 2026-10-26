"""Server-side derivation of the usage meter and capabilities (docs/access-tiers.md).

Everything is computed from the verified principal's tier, the versioned entitlement policy and
the quota ledger. Free turns never count (decision 23) because only committed turns are counted.
"""
from __future__ import annotations

from datetime import datetime
from fractions import Fraction

from .domain import (
    Capabilities,
    GateReason,
    MeterState,
    Principal,
    PrincipalKind,
    RuntimeConfig,
    SessionBootstrap,
    Tier,
    UsagePeriod,
    UsageView,
    history_active,
)
from .entitlements import Allowance, EntitlementPolicy
from .quota import QuotaService


class AccountUnavailable(Exception):
    """Disabled principal or a tier with no allowance: no access view is produced."""


def tier_of(principal: Principal) -> Tier:
    if principal.kind == PrincipalKind.GUEST:
        return Tier.GUEST
    if principal.kind == PrincipalKind.REGISTERED_FREE:
        return Tier.REGISTERED
    raise AccountUnavailable()


def _ratio(value: float) -> Fraction:
    return Fraction(str(value))  # exact: 0.7 means 7/10, so 35 of 50 is on the threshold


def meter_state(policy: EntitlementPolicy, turns: int, trips: int, allowance: Allowance) -> tuple[
    MeterState, GateReason | None
]:
    turn_limit, trip_limit = allowance.user_turn_limit, allowance.activated_trip_limit
    if turns >= turn_limit or trips >= trip_limit:
        # Whichever allowance is exhausted names the reason; a tie favours turns.
        reason = GateReason.TURNS_LIMIT if turns >= turn_limit else GateReason.TRIPS_LIMIT
        return MeterState.GATED, reason
    ratio = max(Fraction(turns, turn_limit), Fraction(trips, trip_limit))
    if ratio >= _ratio(policy.notice_benefit_ratio):
        return MeterState.NOTICE_90, None
    if ratio >= _ratio(policy.notice_quiet_meter_ratio):
        return MeterState.QUIET_70, None
    return MeterState.NONE, None


def usage_view(quotas: QuotaService, principal: Principal) -> UsageView:
    tier_of(principal)
    policy = quotas.policies.get(principal.policy_version)
    allowance = policy.for_kind(principal.kind)
    if allowance is None:
        raise AccountUnavailable()
    quotas.ensure_record(principal.uid)
    used = quotas.usage_for(principal.uid)
    state, reason = meter_state(policy, used.successful_turns, used.activated_trips, allowance)
    registered = policy.for_kind(PrincipalKind.REGISTERED_FREE)
    guest = principal.kind == PrincipalKind.GUEST
    return UsageView(
        period=UsagePeriod(allowance.period.value),
        user_turns_used=used.successful_turns,
        user_turns_limit=allowance.user_turn_limit,
        activated_trips_used=used.activated_trips,
        activated_trips_limit=allowance.activated_trip_limit,
        resets_at=None,
        meter_state=state,
        gate_reason=reason,
        registered_turn_limit=registered.user_turn_limit if guest and registered else None,
        registered_trip_limit=registered.activated_trip_limit if guest and registered else None,
    )


def capabilities_for(
    principal: Principal, usage: UsageView, config: RuntimeConfig | None = None
) -> Capabilities:
    tier = tier_of(principal)
    return Capabilities(
        can_start_turn=usage.meter_state != MeterState.GATED,
        can_use_location=True,
        can_read=True,
        # False until POST /v1/me:export and /v1/me:delete exist (docs/access-tiers.md); clients must
        # not depend on them.
        can_export=False,
        can_delete=False,
        can_link_account=tier == Tier.GUEST,
        # Server-owned (decision 25): false unless history_enabled and an approved notice exist.
        history_enabled=history_active(config),
    )


def session_bootstrap(
    quotas: QuotaService, principal: Principal, now: datetime, config: RuntimeConfig | None = None
) -> SessionBootstrap:
    usage = usage_view(quotas, principal)
    return SessionBootstrap(
        tier=tier_of(principal),
        policy_version=quotas.policies.get(principal.policy_version).version,
        capabilities=capabilities_for(principal, usage, config),
        usage=usage,
        server_time=now,
    )
