from app.domain import PrincipalKind
from app.entitlements import Period, default_registry


def test_pilot_policy_matches_access_tiers() -> None:
    registry = default_registry()
    assert registry.current().version
    guest = registry.for_kind(PrincipalKind.GUEST)
    registered = registry.for_kind(PrincipalKind.REGISTERED_FREE)
    assert (guest.user_turn_limit, guest.activated_trip_limit, guest.period) == (
        50, 10, Period.LIFETIME,
    )
    assert (
        registered.user_turn_limit, registered.activated_trip_limit, registered.period
    ) == (100, 10, Period.ROLLING_30_DAYS)


def test_disabled_has_no_entitlement() -> None:
    assert default_registry().for_kind(PrincipalKind.DISABLED) is None


def test_unknown_version_resolves_to_current() -> None:
    registry = default_registry()
    assert registry.get("no-such-version") is registry.current()
    assert registry.get(None) is registry.current()
