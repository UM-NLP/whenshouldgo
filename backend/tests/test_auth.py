import pytest

from app.auth import InvalidToken, PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import PrincipalKind
from app.entitlements import default_registry

VERIFIER = StaticTokenVerifier(
    {
        "tok-guest": VerifiedIdentity("guest-a", "anonymous"),
        "REGISTERED_FREE": VerifiedIdentity("guest-x", "anonymous"),
    }
)


def test_static_verifier_known_and_unknown_tokens() -> None:
    assert VERIFIER.verify("tok-guest").uid == "guest-a"
    for token in ("", "   ", "nope"):
        with pytest.raises(InvalidToken):
            VERIFIER.verify(token)


def test_resolver_maps_provider_to_tier_on_server() -> None:
    resolver = PrincipalResolver(disabled_uids={"off"})
    version = default_registry().current().version
    assert resolver.resolve(VerifiedIdentity("g", "anonymous")).kind == PrincipalKind.GUEST
    assert resolver.resolve(VerifiedIdentity("r", "google.com")).kind == PrincipalKind.REGISTERED_FREE
    assert resolver.resolve(VerifiedIdentity("p", "password")).kind == PrincipalKind.REGISTERED_FREE
    disabled = resolver.resolve(VerifiedIdentity("off", "google.com"))
    assert disabled.kind == PrincipalKind.DISABLED
    assert disabled.policy_version == version


def test_tier_cannot_come_from_token_text() -> None:
    principal = PrincipalResolver().resolve(VERIFIER.verify("REGISTERED_FREE"))
    assert principal.kind == PrincipalKind.GUEST
