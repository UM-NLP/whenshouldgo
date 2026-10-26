"""Firebase ID-token verifier: signature, claims, tier-from-claims, key rotation, secrecy."""
from __future__ import annotations

import logging

import pytest

from app.auth import InvalidToken, PrincipalResolver, VerifiedIdentity
from app.auth_firebase import (
    FirebaseConfigError,
    FirebaseIdentityVerifier,
    FirebaseTokenError,
)
from app.domain import PrincipalKind
from tests.firebase_support import (
    NOW,
    PROJECT,
    Clock,
    FakeFetcher,
    KeyPair,
    firebase_claims,
    mint,
    mint_alg_none,
    mint_hs256_with_public_key,
)

PAIR = KeyPair("kid-1")
PAIR2 = KeyPair("kid-2")


def make(fetcher=None, **kw):
    fetcher = fetcher or FakeFetcher({PAIR.kid: PAIR.x509_pem()})
    clock = kw.pop("clock", Clock())
    return FirebaseIdentityVerifier(PROJECT, fetcher, clock=clock, **kw), fetcher, clock


def provider(name):
    return {"sign_in_provider": name}


def test_valid_anonymous_token_is_guest():
    v, _, _ = make()
    ident = v.verify(mint(PAIR, firebase_claims()))
    assert (ident.uid, ident.sign_in_provider) == ("uid-1", "anonymous")
    assert PrincipalResolver().resolve(ident).kind is PrincipalKind.GUEST


@pytest.mark.parametrize("prov", ["google.com", "password", "emailLink"])
def test_registered_providers_are_registered_free(prov):
    v, _, _ = make()
    ident = v.verify(mint(PAIR, firebase_claims(firebase=provider(prov))))
    assert PrincipalResolver().resolve(ident).kind is PrincipalKind.REGISTERED_FREE


@pytest.mark.parametrize("prov", ["custom", "phone", "facebook.com", "", None, 5])
def test_unknown_provider_rejected(prov):
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(firebase={"sign_in_provider": prov})))


def test_missing_firebase_claim_rejected():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(firebase=None)))


def test_tier_comes_from_claims_only_not_extra_claims():
    v, _, _ = make()
    tok = mint(
        PAIR,
        firebase_claims(tier="REGISTERED_FREE", kind="REGISTERED_FREE", admin=True),
    )
    ident = v.verify(tok)
    assert PrincipalResolver().resolve(ident).kind is PrincipalKind.GUEST


def test_wrong_audience_rejected():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(aud="other-project")))


def test_wrong_issuer_rejected():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(iss="https://securetoken.google.com/other")))
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(iss="https://accounts.google.com")))


def test_expired_rejected_and_skew_tolerated():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(exp=NOW - 3600)))
    v.verify(mint(PAIR, firebase_claims(exp=NOW - 5)))  # inside the small skew


def test_not_yet_valid_rejected():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(iat=NOW + 3600)))
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(auth_time=NOW + 3600)))


@pytest.mark.parametrize("claim", ["exp", "iat", "auth_time", "aud", "iss"])
def test_required_claims_missing_rejected(claim):
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims(**{claim: None})))


@pytest.mark.parametrize("sub", [None, "", "   ", 7])
def test_missing_or_empty_sub_rejected(sub):
    v, _, _ = make()
    claims = firebase_claims()
    if sub is None:
        del claims["sub"]
    else:
        claims["sub"] = sub
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, claims))


def test_forged_alg_none_rejected():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint_alg_none(firebase_claims(), PAIR.kid))


def test_hs256_signed_with_public_key_rejected():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint_hs256_with_public_key(PAIR, firebase_claims()))


def test_signature_from_other_key_rejected():
    v, _, _ = make()
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR2, firebase_claims(), kid=PAIR.kid))


@pytest.mark.parametrize("tok", ["", "  ", "abc", "a.b.c", "a." * 3, "x" * 20000])
def test_garbage_rejected(tok):
    v, _, _ = make()
    with pytest.raises(InvalidToken):
        v.verify(tok)


def test_unknown_kid_one_refresh_then_rotation_success():
    fetcher = FakeFetcher({PAIR.kid: PAIR.x509_pem()})
    v, _, _ = make(fetcher, min_refresh_interval=0)
    v.verify(mint(PAIR, firebase_claims()))
    assert fetcher.calls == 1
    fetcher.keys = {PAIR2.kid: PAIR2.x509_pem()}  # Google rotated
    v.verify(mint(PAIR2, firebase_claims()))
    assert fetcher.calls == 2


def test_unknown_kid_refreshes_once_then_rejects():
    fetcher = FakeFetcher({PAIR.kid: PAIR.x509_pem()})
    v, _, _ = make(fetcher, min_refresh_interval=0)
    v.verify(mint(PAIR, firebase_claims()))
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR2, firebase_claims()))
    assert fetcher.calls == 2


def test_forged_kid_flood_cannot_amplify_fetches():
    fetcher = FakeFetcher({PAIR.kid: PAIR.x509_pem()})
    v, _, _ = make(fetcher)  # default refresh floor
    v.verify(mint(PAIR, firebase_claims()))
    for _ in range(5):
        with pytest.raises(FirebaseTokenError):
            v.verify(mint(PAIR2, firebase_claims()))
    assert fetcher.calls == 1


def test_cache_honors_max_age():
    fetcher = FakeFetcher({PAIR.kid: PAIR.x509_pem()}, max_age=100)
    v, _, clock = make(fetcher)
    v.verify(mint(PAIR, firebase_claims()))
    clock.now += 50
    v.verify(mint(PAIR, firebase_claims()))
    assert fetcher.calls == 1
    clock.now += 100
    v.verify(mint(PAIR, firebase_claims(iat=clock.now - 10, auth_time=clock.now - 20,
                                        exp=clock.now + 3000)))
    assert fetcher.calls == 2


def test_fetch_failure_fails_closed():
    v, _, _ = make(FakeFetcher({}, fail=True))
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims()))


def test_bad_certificate_material_fails_closed():
    v, _, _ = make(FakeFetcher({PAIR.kid: "not a cert"}))
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims()))


class Revoker:
    def __init__(self, revoked=False, boom=False):
        self.revoked, self.boom, self.seen = revoked, boom, []

    def is_revoked(self, uid, issued_at):
        self.seen.append((uid, issued_at))
        if self.boom:
            raise RuntimeError("firestore down")
        return self.revoked


def test_revoked_user_becomes_disabled_principal():
    disabled: set[str] = set()
    v, _, _ = make(revocation=Revoker(revoked=True), disabled_uids=disabled)
    ident = v.verify(mint(PAIR, firebase_claims()))
    resolver = PrincipalResolver(disabled_uids=disabled)
    assert resolver.resolve(ident).kind is PrincipalKind.DISABLED


def test_not_revoked_stays_guest_and_checker_sees_verified_uid():
    rev = Revoker()
    disabled: set[str] = set()
    v, _, _ = make(revocation=rev, disabled_uids=disabled)
    ident = v.verify(mint(PAIR, firebase_claims()))
    assert rev.seen == [("uid-1", NOW - 10)]
    assert PrincipalResolver(disabled_uids=disabled).resolve(ident).kind is PrincipalKind.GUEST


def test_revocation_checker_failure_fails_closed():
    v, _, _ = make(revocation=Revoker(boom=True))
    with pytest.raises(FirebaseTokenError):
        v.verify(mint(PAIR, firebase_claims()))


@pytest.mark.parametrize("pid", ["", "  ", None])
def test_construction_without_project_id_fails_closed(pid):
    with pytest.raises(FirebaseConfigError):
        FirebaseIdentityVerifier(pid, FakeFetcher({}))


def test_from_environment_requires_project_id():
    with pytest.raises(FirebaseConfigError):
        FirebaseIdentityVerifier.from_environment({}, FakeFetcher({}))
    v = FirebaseIdentityVerifier.from_environment(
        {"FIREBASE_PROJECT_ID": PROJECT}, FakeFetcher({PAIR.kid: PAIR.x509_pem()}), clock=Clock()
    )
    v.verify(mint(PAIR, firebase_claims()))


def test_tokens_never_in_exception_text_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    v, _, _ = make(revocation=Revoker(boom=True))
    bad = [
        mint(PAIR, firebase_claims()),  # revoker boom
        mint(PAIR, firebase_claims(aud="x")),
        mint_alg_none(firebase_claims(), PAIR.kid),
        mint_hs256_with_public_key(PAIR, firebase_claims()),
        mint(PAIR, firebase_claims(sub="secret-uid-123"), kid="nope"),
    ]
    for tok in bad:
        with pytest.raises(FirebaseTokenError) as ei:
            v.verify(tok)
        text = repr(ei.value) + str(ei.value)
        for part in tok.split("."):
            if part:
                assert part not in text
        assert "secret-uid-123" not in text
        assert ei.value.__cause__ is None
    for tok in bad:
        assert tok not in caplog.text
    assert "secret-uid-123" not in caplog.text


class _Resp:
    def __init__(self, status=200, body=None, cc="public, max-age=19000, must-revalidate"):
        self.status_code, self._body = status, body if body is not None else {}
        self.headers = {"cache-control": cc}
        self.content = b"{}"

    def json(self):
        return self._body


class _Client:
    def __init__(self, resp):
        self.resp, self.urls = resp, []

    def get(self, url, timeout):
        self.urls.append(url)
        return self.resp


def test_http_fetcher_parses_max_age_and_rejects_bad_status():
    from app.auth_firebase import HttpKeyFetcher, KeyFetchError

    ok = HttpKeyFetcher("https://example.invalid/certs", client=_Client(_Resp(body={"k": "pem"})))
    got = ok.fetch()
    assert got.max_age_seconds == 19000 and dict(got.keys) == {"k": "pem"}
    with pytest.raises(KeyFetchError):
        HttpKeyFetcher("https://example.invalid/c", client=_Client(_Resp(status=500))).fetch()
    with pytest.raises(FirebaseConfigError):
        HttpKeyFetcher("http://example.invalid/c")


# -- review finding 14: revocation travels on the identity, never through shared mutable state ----
class PerTokenRevoker:
    """Revoked for tokens issued before ``cutoff`` (the user signed out everywhere then)."""

    def __init__(self, cutoff: float) -> None:
        self.cutoff = cutoff

    def is_revoked(self, uid, issued_at):
        return issued_at < self.cutoff


def test_another_tokens_success_never_re_enables_a_revoked_one():
    disabled: set[str] = set()
    v, _, _ = make(revocation=PerTokenRevoker(NOW - 5), disabled_uids=disabled)
    resolver = PrincipalResolver(disabled_uids=disabled)
    old = v.verify(mint(PAIR, firebase_claims(iat=NOW - 10, auth_time=NOW - 20)))
    fresh = v.verify(mint(PAIR, firebase_claims(iat=NOW - 1, auth_time=NOW - 2)))
    assert old.revoked is True and fresh.revoked is False
    assert resolver.resolve(old).kind is PrincipalKind.DISABLED
    assert resolver.resolve(fresh).kind is PrincipalKind.GUEST
    assert disabled == set()  # nothing was written to the shared set, so nothing can be discarded
    assert resolver.resolve(old).kind is PrincipalKind.DISABLED  # still, after the success


def test_the_resolver_contract_with_a_static_disabled_set_still_works():
    resolver = PrincipalResolver(disabled_uids={"u1"})
    assert resolver.resolve(VerifiedIdentity("u1", "google.com")).kind is PrincipalKind.DISABLED
    assert resolver.resolve(VerifiedIdentity("u2", "google.com")).kind is PrincipalKind.REGISTERED_FREE
