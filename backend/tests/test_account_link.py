"""Item 10: one-time link intents, same-UID completion, collisions and the quota race.

Every test runs over the in-memory stores and the Firestore adapters on the in-process fake.
"""
from __future__ import annotations

import threading

import pytest
from fs_backend import MEMORY_FAKE_EMULATOR
from account_support import H, Env, guest, registered

from app import telemetry as t


@pytest.fixture(params=MEMORY_FAKE_EMULATOR)
def env(request: pytest.FixtureRequest) -> Env:
    e = Env(request.param)
    e.seed("g1", turns=3)
    e.seed("g2", turns=1)
    e.seed("r1", turns=7)
    return e


# --- intent --------------------------------------------------------------------------------------
def test_intent_is_random_bound_and_expires_in_fifteen_minutes(env: Env) -> None:
    a, b = env.start_intent(), env.start_intent()
    assert a["id"] != b["id"] and a["nonce"] != b["nonce"]
    assert len(a["nonce"]) >= 16 and a["provider"] == "GOOGLE"
    from datetime import datetime
    created = datetime.fromisoformat(a["created_at"])
    assert datetime.fromisoformat(a["expires_at"]) - created == __import__("datetime").timedelta(
        minutes=15)
    stored = env.service.store.get_intent(a["id"])  # type: ignore[union-attr]
    assert stored.owner_uid == "g1"  # bound to the verified token's uid
    assert a["nonce"] not in env.dump()  # only a digest of the nonce is stored


def test_intent_is_idempotent_by_key_and_rotates_the_nonce(env: Env) -> None:
    first = env.start_intent(**{"Idempotency-Key": "key-0001"})
    second = env.start_intent(**{"Idempotency-Key": "key-0001"})
    other = env.start_intent(**{"Idempotency-Key": "key-0002"})
    assert first["id"] == second["id"] != other["id"]
    assert first["nonce"] != second["nonce"]
    assert env.complete(first).status_code == 404  # the lost-response nonce is dead
    env.admin.link("g1")
    assert env.complete(second).status_code == 200


def test_a_key_of_another_user_is_a_different_intent(env: Env) -> None:
    a = env.start_intent("g1", **{"Idempotency-Key": "shared-key"})
    b = env.start_intent("g2", **{"Idempotency-Key": "shared-key"})
    assert a["id"] != b["id"]
    assert env.service.store.get_intent(b["id"]).owner_uid == "g2"  # type: ignore[union-attr]


def test_a_registered_account_cannot_start_a_link(env: Env) -> None:
    response = env.http.post("/v1/account/link-intents", json={"provider": "GOOGLE"}, headers=H["r1"])
    assert response.status_code == 409 and response.json()["message"] == "the account is already registered"


def test_identity_is_never_read_from_the_body(env: Env) -> None:
    body = {"provider": "GOOGLE", "uid": "g2", "owner_id": "g2", "tier": "registered"}
    assert env.http.post("/v1/account/link-intents", json=body, headers=H["g1"]).status_code == 422
    intent = env.start_intent()
    response = env.http.post(f"/v1/account/link-intents/{intent['id']}:complete",
                             json={"nonce": intent["nonce"], "uid": "g1"}, headers=H["g1-linked"])
    assert response.status_code == 422


# --- completion ----------------------------------------------------------------------------------
def test_complete_changes_the_tier_and_preserves_usage_and_workspaces(env: Env) -> None:
    intent = env.start_intent()
    env.admin.link("g1")
    before_usage = env.quotas.usage_for("g1")
    response = env.complete(intent)
    assert response.status_code == 200, response.text
    assert response.json() == {"schema_version": 1, "kind": "account_link_result", "linked": True,
                               "tier": "registered"}
    account = env.service.store.get_account("g1")  # type: ignore[union-attr]
    assert account is not None and account.provider.value == "GOOGLE"
    after = env.quotas.usage_for("g1")
    assert (after.successful_turns, after.activated_trips) == (3, 0) == (
        before_usage.successful_turns, before_usage.activated_trips)
    assert env.stores.workspaces.get("g1", "default", env.clock()) is not None
    usage = env.http.get("/v1/me/usage", headers=H["g1-linked"]).json()
    assert usage["user_turns_used"] == 3 and usage["user_turns_limit"] == 100  # registered numbers
    assert usage["resets_at"] is not None  # the rolling window started


def test_complete_is_one_time_a_second_use_is_rejected(env: Env) -> None:
    intent = env.start_intent()
    env.admin.link("g1")
    assert env.complete(intent).status_code == 200
    again = env.complete(intent)
    assert again.status_code == 409 and again.json().get("reason") == "LINK_INTENT_CONSUMED"


def test_wrong_nonce_unknown_id_and_another_tenants_intent_look_the_same(env: Env) -> None:
    intent = env.start_intent()
    env.admin.link("g1")
    wrong = env.complete(intent, nonce="x" * 24)
    unknown = env.complete(intent, intent_id="link-does-not-exist")
    cross = env.http.post(f"/v1/account/link-intents/{intent['id']}:complete",
                          json={"nonce": "y" * 24}, headers=H["g2"])
    for response in (wrong, unknown, cross):
        assert response.status_code == 404 and response.json()["message"] in ("unknown link intent", "unknown migration")
    assert env.service.store.get_account("g1") is None  # nothing changed


def test_the_same_intent_presented_by_a_different_anonymous_uid_is_rejected(env: Env) -> None:
    intent = env.start_intent("g1")
    response = env.http.post(f"/v1/account/link-intents/{intent['id']}:complete",
                             json={"nonce": intent["nonce"]}, headers=H["g2"])
    assert response.status_code == 403 and response.json().get("reason") == "WRONG_UID"
    assert env.service.store.get_account("g1") is None and env.service.store.get_account("g2") is None


def test_an_anonymous_token_cannot_complete(env: Env) -> None:
    intent = env.start_intent()
    env.admin.link("g1")
    stale = env.http.post(f"/v1/account/link-intents/{intent['id']}:complete",
                          json={"nonce": intent["nonce"]}, headers=H["g1"])
    assert stale.status_code == 403 and stale.json().get("reason") == "ANONYMOUS_PROVIDER"
    assert env.service.store.get_account("g1") is None


def test_the_admin_must_confirm_a_permanent_provider_even_if_the_token_claims_one(env: Env) -> None:
    intent = env.start_intent()  # admin still shows g1 with no provider
    response = env.complete(intent)
    assert response.status_code == 403 and response.json().get("reason") == "ANONYMOUS_PROVIDER"
    env.admin.link("g1", "password")  # a provider that does not match the GOOGLE intent
    mismatch = env.complete(intent)
    assert mismatch.status_code == 403 and mismatch.json()["message"] == "the provider differs from the intent"
    assert env.service.store.get_account("g1") is None


def test_an_expired_intent_is_rejected(env: Env) -> None:
    intent = env.start_intent()
    env.admin.link("g1")
    env.clock.advance(minutes=15, seconds=1)
    response = env.complete(intent)
    assert response.status_code == 409 and response.json().get("reason") == "LINK_INTENT_EXPIRED"
    assert env.service.store.get_account("g1") is None


def test_without_a_firebase_admin_completion_fails_closed() -> None:
    e = Env("memory", admin=False)
    e.seed("g1")
    intent = e.start_intent()
    response = e.complete(intent)
    assert response.status_code == 503 and response.json()["reason"] == "SERVER_NOT_CONFIGURED"


def test_a_credential_that_owns_an_account_is_a_typed_collision(env: Env) -> None:
    intent = env.start_intent("g1")
    response = env.complete(intent, who="r1")  # r1 already exists and presents g1's intent
    assert response.status_code == 409
    assert response.json().get("reason") == "ACCOUNT_COLLISION" and response.json()["code"] == "CONFLICT"
    assert env.service.store.get_account("g1") is None and env.service.store.get_account("r1") is None
    assert env.stores.workspaces.get("g1", "default", env.clock()) is not None  # both preserved
    assert env.quotas.usage_for("g1").successful_turns == 3
    assert env.quotas.usage_for("r1").successful_turns == 7
    stored = env.service.store.get_intent(intent["id"])  # type: ignore[union-attr]
    assert stored.state.value == "COLLISION"


def test_reconcile_records_a_link_whose_completion_was_lost(env: Env) -> None:
    env.admin.link("g1")
    assert env.service.reconcile(registered("g1")) is True  # type: ignore[union-attr]
    assert env.service.reconcile(registered("g1")) is False  # idempotent
    assert env.service.store.get_account("g1") is not None  # type: ignore[union-attr]
    assert env.service.reconcile(guest("g2")) is False  # type: ignore[union-attr]


# --- routes, limiter, telemetry --------------------------------------------------------------------
def test_routes_need_auth_refuse_disabled_and_are_503_without_a_service() -> None:
    e = Env("memory", with_service=False)
    body = {"provider": "GOOGLE"}
    assert e.http.post("/v1/account/link-intents", json=body).status_code == 401
    for path, payload in (("/v1/account/link-intents", body), ("/v1/me:export", None),
                          ("/v1/me:delete", None)):
        response = e.http.post(path, json=payload, headers=H["g1"])
        assert response.status_code == 503 and response.json()["reason"] == "SERVER_NOT_CONFIGURED"
    assert e.http.post("/v1/me:export", headers=H["off"]).status_code == 403
    e2 = Env("memory")
    assert e2.http.post("/v1/me:export", headers=H["off"]).status_code == 403
    caps = e.http.get("/v1/me/capabilities", headers=H["g1"]).json()
    assert caps["can_export"] is False and caps["can_delete"] is False
    caps = e2.http.get("/v1/me/capabilities", headers=H["g1"]).json()
    assert caps["can_export"] is True and caps["can_delete"] is True


def test_link_operations_have_their_own_limiter(env: Env) -> None:
    statuses = [env.http.post("/v1/account/link-intents", json={"provider": "GOOGLE"},
                              headers=H["g1"]).status_code for _ in range(11)]
    assert statuses[:10] == [200] * 10 and statuses[10] == 429
    last = env.http.post("/v1/account/link-intents", json={"provider": "GOOGLE"}, headers=H["g1"])
    assert last.json()["reason"] == "RATE_LIMITED" and "retry-after" in last.headers
    # another principal, and the general access limiter, are unaffected
    assert env.http.post("/v1/account/link-intents", json={"provider": "GOOGLE"},
                         headers=H["g2"]).status_code == 200
    assert env.http.get("/v1/me/usage", headers=H["g1"]).status_code == 200


def test_telemetry_carries_enums_only(env: Env) -> None:
    intent = env.start_intent()
    env.admin.link("g1")
    env.complete(intent, nonce="z" * 24)
    env.complete(intent)
    started = [e for e in env.sink.events if isinstance(e, t.AccountLinkStarted)]
    done = [e for e in env.sink.events if isinstance(e, t.AccountLinkCompleted)]
    assert [e.provider.value for e in started] == ["GOOGLE"]
    assert [e.outcome for e in done] == [t.LinkOutcome.REJECTED, t.LinkOutcome.LINKED]
    blob = " ".join(e.model_dump_json() for e in env.sink.events)
    assert intent["nonce"] not in blob and intent["id"] not in blob and "g1" not in blob


# --- the quota race ----------------------------------------------------------------------------------
def run_threads(count: int, target) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(count)

    def wrapped(i: int) -> None:
        try:
            barrier.wait()
            target(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return errors


@pytest.mark.hot_contention
def test_racing_completions_link_once_and_never_change_the_allowance(env: Env) -> None:
    intent = env.start_intent()
    env.admin.link("g1")
    results: list[int] = []

    def go(_: int) -> None:
        results.append(env.complete(intent).status_code)

    assert run_threads(8, go) == []
    assert results.count(200) == 1 and set(results) <= {200, 409}  # exactly one LINKED
    usage = env.quotas.usage_for("g1")
    assert (usage.successful_turns, usage.activated_trips) == (3, 0)  # no allowance was added


@pytest.mark.hot_contention
def test_duplicate_allowance_increments_during_link_are_zero(env: Env) -> None:
    """Gate: duplicate allowance increments < 0.1%. Here the rate must be exactly zero: the same
    idempotency keys committed from many threads while the account is being linked count once."""
    intent = env.start_intent()
    env.admin.link("g1")
    keys = [f"turn-{i}" for i in range(10)]

    def go(i: int) -> None:
        if i == 0:
            env.complete(intent)
            return
        principal = registered("g1") if i % 2 else guest("g1")
        for key in keys:  # every thread commits every key: each key may count only once
            env.quotas.commit_turn(principal, key)

    assert run_threads(9, go) == []
    usage = env.quotas.usage_for("g1")
    assert usage.successful_turns == 3 + len(keys)
    assert usage.committed_keys >= set(keys)
