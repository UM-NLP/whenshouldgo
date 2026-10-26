"""Contract suite for the durable-store protocols (ACCESS-02 / #35, decision 26).

Every test runs against BOTH the in-memory stores and the Firestore adapters over the in-process
fake (``app/stores/fake_firestore.py``). The fake does NOT prove real Firestore behaviour
(indexes, TTL policy timing, contention); see docs/backlog.md for the real-emulator TODO.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from maps_simulator import NOW
from maps_simulator import wrap_unvalidated as W
from test_discovery_contracts import make_answer

from app.access import usage_view
from app.consent import InMemoryConsentStore
from app.discovery import CacheError
from app.domain import ConsentPurpose, Principal, PrincipalKind, TripWorkspace
from app.entitlements import (
    Allowance,
    EntitlementPolicy,
    Period,
    PolicyRegistry,
)
from app.quota import QuotaService, ReservationConflict
from app.stores import (
    CostEntryDelta,
    SpendBasis,
    KillSwitchReason,
    SpendBudget,
    StateStores,
    StoreKillSwitch,
    firestore_stores,
    memory_stores,
)
from app.stores.fake_firestore import FakeFirestoreClient, fake_transactional

T0 = NOW  # the synthetic answers are retrieved at this instant
POLICY_VERSION = "contract-test-1"


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def registry(turns: int = 5, trips: int = 3, reg_turns: int = 5, reg_trips: int = 3) -> PolicyRegistry:
    policy = EntitlementPolicy(
        version=POLICY_VERSION, effective_at=T0, grace_rule="unknown-version-uses-current",
        allowances={
            PrincipalKind.GUEST: Allowance(turns, trips, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(reg_turns, reg_trips, Period.ROLLING_30_DAYS),
        })
    return PolicyRegistry((policy,), POLICY_VERSION)


def guest(uid: str = "g1") -> Principal:
    return Principal(uid, PrincipalKind.GUEST, POLICY_VERSION)


def registered(uid: str = "r1") -> Principal:
    return Principal(uid, PrincipalKind.REGISTERED_FREE, POLICY_VERSION)


class Env:
    def __init__(self, kind: str, **cache: int) -> None:
        self.kind = kind
        self.clock = Clock()
        self.client: FakeFirestoreClient | None = None
        if kind == "memory":
            self.stores: StateStores = memory_stores(self.clock, **cache)
        else:
            self.client = FakeFirestoreClient(self.clock)
            self.stores = firestore_stores(
                self.client, self.clock, transactional=fake_transactional, **cache)

    def quotas(self, **limits: int) -> QuotaService:
        return QuotaService(registry(**limits), store=self.stores.quota, clock=self.clock)


@pytest.fixture(params=["memory", "fake-firestore"])
def env(request: pytest.FixtureRequest) -> Env:
    return Env(request.param)


def run_threads(count: int, target) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(count)

    def wrapped(index: int) -> None:
        try:
            barrier.wait()
            target(index)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted on
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


# --- quota: reserve / commit / release ----------------------------------------------------------
def test_reserve_holds_allowance_and_a_denial_changes_nothing(env: Env) -> None:
    quotas = env.quotas(turns=2)
    p = guest()
    a, b = quotas.reserve(p, "a"), quotas.reserve(p, "b")
    with pytest.raises(PermissionError):
        quotas.reserve(p, "c")
    used = quotas.usage_for(p.uid)
    assert used.reserved_keys == {"a", "b"} and used.successful_turns == 0
    quotas.release(b)
    assert quotas.reserve(p, "c").idempotency_key == "c"
    quotas.commit(a)
    used = quotas.usage_for(p.uid)
    assert used.successful_turns == 1 and used.committed_keys == {"a"} and used.reserved_keys == {"c"}


def test_in_flight_and_committed_keys_are_distinguished(env: Env) -> None:
    quotas = env.quotas()
    p = guest()
    held = quotas.reserve(p, "k")
    with pytest.raises(ReservationConflict):
        quotas.reserve(p, "k")
    quotas.commit(held)
    quotas.commit(held)  # idempotent: counted once
    again = quotas.reserve(p, "k")
    assert again.duplicate is True
    assert quotas.usage_for(p.uid).successful_turns == 1


def test_commit_without_a_reservation_is_refused(env: Env) -> None:
    quotas = env.quotas()
    from app.quota import TurnReservation
    with pytest.raises(RuntimeError):
        quotas.commit(TurnReservation("g1", "never-reserved"))


def test_commit_turn_checks_and_counts_atomically(env: Env) -> None:
    quotas = env.quotas(turns=1)
    p = guest()
    quotas.commit_turn(p, "a")
    quotas.commit_turn(p, "a")  # replay counts once
    with pytest.raises(PermissionError):
        quotas.commit_turn(p, "b")
    assert quotas.usage_for(p.uid).successful_turns == 1


def test_an_abandoned_reservation_expires_instead_of_holding_allowance_forever(env: Env) -> None:
    quotas = env.quotas(turns=1)
    p = guest()
    quotas.reserve(p, "crashed")  # the process dies here: no commit, no release
    with pytest.raises(PermissionError):
        quotas.reserve(p, "next")
    env.clock.advance(minutes=10)
    assert quotas.usage_for(p.uid).reserved_keys == set()
    assert quotas.reserve(p, "next").duplicate is False


def test_tenants_never_share_a_ledger(env: Env) -> None:
    quotas = env.quotas(turns=1)
    quotas.commit_turn(guest("g1"), "k")
    assert quotas.allowed(guest("g2")) is True
    assert quotas.usage_for("g2").successful_turns == 0
    quotas.commit_turn(guest("g2"), "k")  # same key, other tenant: counted separately
    assert quotas.usage_for("g2").successful_turns == 1


@pytest.mark.parametrize("round_", range(3))
def test_racing_reservations_never_overspend(env: Env, round_: int) -> None:
    quotas = env.quotas(turns=10)
    p = guest()
    outcomes: list[str] = []
    lock = threading.Lock()

    def work(i: int) -> None:
        try:
            res = quotas.reserve(p, f"k{i}")
            quotas.commit(res)
            result = "ok"
        except PermissionError:
            result = "denied"
        with lock:
            outcomes.append(result)

    assert run_threads(32, work) == []
    used = quotas.usage_for(p.uid)
    assert outcomes.count("ok") == 10 and outcomes.count("denied") == 22
    assert used.successful_turns == 10 and len(used.committed_keys) == 10  # no duplicate increment


def test_the_same_key_committed_by_many_threads_counts_once(env: Env) -> None:
    quotas = env.quotas(turns=50)
    p = guest()
    held = quotas.reserve(p, "same")
    assert run_threads(20, lambda _i: quotas.commit(held)) == []
    assert quotas.usage_for(p.uid).successful_turns == 1


def test_the_same_key_reserved_by_many_threads_holds_once(env: Env) -> None:
    quotas = env.quotas(turns=50)
    p = guest()
    results: list[str] = []
    lock = threading.Lock()

    def work(_i: int) -> None:
        try:
            quotas.reserve(p, "same")
            outcome = "reserved"
        except ReservationConflict:
            outcome = "conflict"
        with lock:
            results.append(outcome)

    assert run_threads(16, work) == []
    assert results.count("reserved") == 1 and results.count("conflict") == 15


# --- activate_trip ------------------------------------------------------------------------------
def test_activate_trip_counts_once_per_workspace(env: Env) -> None:
    quotas = env.quotas(trips=2)
    p = guest()
    assert quotas.activate_trip(p, "ws-1") is True
    assert quotas.activate_trip(p, "ws-1") is False  # idempotent
    assert quotas.usage_for(p.uid).activated_trips == 1
    assert quotas.activate_trip(p, "ws-2") is True
    with pytest.raises(PermissionError):
        quotas.activate_trip(p, "ws-3")
    assert quotas.activate_trip(p, "ws-1") is False  # an already counted trip never errors
    assert quotas.usage_for(p.uid).activated_trips == 2


def test_racing_activations_of_one_workspace_count_once(env: Env) -> None:
    quotas = env.quotas(trips=5)
    p = guest()
    firsts: list[bool] = []
    lock = threading.Lock()

    def work(_i: int) -> None:
        counted = quotas.activate_trip(p, "ws-1")
        with lock:
            firsts.append(counted)

    assert run_threads(16, work) == []
    assert firsts.count(True) == 1 and quotas.usage_for(p.uid).activated_trips == 1


def test_a_trip_limit_stops_new_turns_as_before(env: Env) -> None:
    quotas = env.quotas(turns=9, trips=1)
    p = guest()
    quotas.activate_trip(p, "ws-1")
    assert quotas.allowed(p) is False
    with pytest.raises(PermissionError):
        quotas.reserve(p, "k")


# --- registered rolling 30-day window ----------------------------------------------------------
def test_the_registered_window_rolls_after_thirty_days_and_guests_never_roll(env: Env) -> None:
    quotas = env.quotas(turns=2, reg_turns=2)
    r, g = registered(), guest()
    for p in (r, g):
        quotas.commit_turn(p, "a")
        quotas.commit_turn(p, "b")
    env.clock.advance(days=29, hours=23)
    assert quotas.allowed(r) is False and quotas.current_usage(r).successful_turns == 2
    env.clock.advance(hours=1)  # exactly 30 days after the window began
    assert quotas.allowed(r) is True
    assert quotas.current_usage(r).successful_turns == 0
    quotas.commit_turn(r, "c")
    assert quotas.current_usage(r).successful_turns == 1
    env.clock.advance(days=400)
    assert quotas.allowed(g) is False and quotas.usage_for(g.uid).successful_turns == 2


def test_resets_at_is_real_for_registered_and_null_for_guests(env: Env) -> None:
    quotas = env.quotas()
    r, g = registered(), guest()
    view = usage_view(quotas, r)
    assert view.resets_at == T0 + timedelta(days=30)
    assert usage_view(quotas, g).resets_at is None
    env.clock.advance(days=31)
    assert usage_view(quotas, r).resets_at == T0 + timedelta(days=61)  # a fresh window


def test_the_window_roll_returns_trips_too(env: Env) -> None:
    quotas = env.quotas(reg_trips=1)
    r = registered()
    quotas.activate_trip(r, "ws-1")
    assert quotas.allowed(r) is False
    env.clock.advance(days=30)
    assert quotas.allowed(r) is True
    assert quotas.activate_trip(r, "ws-1") is True  # a new window counts the trip again


# --- idempotency / replay store -----------------------------------------------------------------
def test_pending_then_commit_then_rollback(env: Env) -> None:
    store, now = env.stores.idempotency, env.clock.now
    store.begin("u1", "k", "fp", _record("hello"), now)
    pending = store.get("u1", "k", now)
    assert pending is not None and pending.pending is True and pending.fingerprint == "fp"
    store.commit("u1", "k", now)
    done = store.get("u1", "k", now)
    assert done is not None and done.pending is False and done.record.result.reply == "hello"
    store.begin("u1", "k2", "fp2", _record("x"), now)
    store.rollback("u1", "k2")
    assert store.get("u1", "k2", now) is None


def test_idempotency_records_are_tenant_scoped(env: Env) -> None:
    store, now = env.stores.idempotency, env.clock.now
    store.begin("u1", "k", "fp", _record("mine"), now)
    store.commit("u1", "k", now)
    assert store.get("u2", "k", now) is None


def test_a_crashed_pending_record_expires_but_a_committed_one_stays_until_retention(env: Env) -> None:
    store = env.stores.idempotency
    now = env.clock.now
    store.begin("u1", "crashed", "fp", _record("x"), now)
    store.begin("u1", "done", "fp", _record("y"), now)
    store.commit("u1", "done", now)
    env.clock.advance(minutes=10)
    assert store.get("u1", "crashed", env.clock.now) is None
    assert store.get("u1", "done", env.clock.now) is not None
    env.clock.advance(days=30)
    assert store.get("u1", "done", env.clock.now) is None


def _record(reply: str):
    from app.orchestrator import ReplayRecord, TurnResult
    return ReplayRecord(TurnResult(reply, None, (), None))


# --- workspace store ----------------------------------------------------------------------------
def _workspace(owner: str, goal: str = "g") -> TripWorkspace:
    return TripWorkspace(id=f"ws-{owner}", owner_id=owner, goal=goal)


def test_workspace_round_trip_and_tenant_isolation(env: Env) -> None:
    store, now = env.stores.workspaces, env.clock.now
    store.put("u1", "default", _workspace("u1", "mine"), now, None)
    store.put("u2", "default", _workspace("u2", "theirs"), now, None)
    assert store.get("u1", "default", now).goal == "mine"
    assert store.get("u2", "default", now).goal == "theirs"
    assert store.get("u3", "default", now) is None
    store.delete("u1", "default")
    assert store.get("u1", "default", now) is None and store.get("u2", "default", now) is not None


def test_a_guest_workspace_expires_after_seven_inactive_days_and_a_write_refreshes_it(env: Env) -> None:
    store = env.stores.workspaces
    ttl = timedelta(days=7)
    store.put("u1", "default", _workspace("u1"), env.clock.now, ttl)
    env.clock.advance(days=6)
    assert store.get("u1", "default", env.clock.now) is not None
    store.put("u1", "default", _workspace("u1", "again"), env.clock.now, ttl)  # activity
    env.clock.advance(days=6)
    assert store.get("u1", "default", env.clock.now).goal == "again"
    env.clock.advance(days=1)  # seven days since the last write
    assert store.get("u1", "default", env.clock.now) is None


def test_a_workspace_without_a_ttl_does_not_expire(env: Env) -> None:
    store = env.stores.workspaces
    store.put("u1", "default", _workspace("u1"), env.clock.now, None)
    env.clock.advance(days=900)
    assert store.get("u1", "default", env.clock.now) is not None


# --- consent ledger -----------------------------------------------------------------------------
def test_consent_ledger_is_append_only(env: Env) -> None:
    consents = InMemoryConsentStore(env.clock, ledger=env.stores.consents)
    first = consents.grant("u1", ConsentPurpose.LOCATION_FOREGROUND, "location-notice-1")
    assert consents.grant("u1", ConsentPurpose.LOCATION_FOREGROUND, "location-notice-1") == first
    before = consents.ledger("u1")
    env.clock.advance(minutes=1)
    assert consents.withdraw("u1", ConsentPurpose.LOCATION_FOREGROUND) is True
    after = consents.ledger("u1")
    assert after[: len(before)] == before and len(after) == 2  # earlier entries never mutate
    assert after[1].id == first.id and after[1].withdrawn_at is not None
    assert consents.has_active("u1", ConsentPurpose.LOCATION_FOREGROUND) is False
    again = consents.grant("u1", ConsentPurpose.LOCATION_FOREGROUND, "location-notice-1")
    assert again.id != first.id and len(consents.ledger("u1")) == 3


def test_consent_is_tenant_scoped(env: Env) -> None:
    consents = InMemoryConsentStore(env.clock, ledger=env.stores.consents)
    consents.grant("u1", ConsentPurpose.LOCATION_FOREGROUND, "location-notice-1")
    assert consents.has_active("u2", ConsentPurpose.LOCATION_FOREGROUND) is False
    assert consents.ledger("u2") == ()


def test_racing_grants_make_one_active_consent(env: Env) -> None:
    consents = InMemoryConsentStore(env.clock, ledger=env.stores.consents)
    ids: list[str] = []
    lock = threading.Lock()

    def work(_i: int) -> None:
        record = consents.grant("u1", ConsentPurpose.LOCATION_FOREGROUND, "location-notice-1")
        with lock:
            ids.append(record.id)

    assert run_threads(12, work) == []
    assert len(set(ids)) == 1 and len(consents.ledger("u1")) == 1


# --- cost store: whole-pilot spend counter and kill switch --------------------------------------
def test_spend_totals_separate_provider_attributed_spend(env: Env) -> None:
    store = env.stores.costs
    store.add_spend(CostEntryDelta(100, 10, 5_000, provider_attributed=False), None, env.clock.now)
    totals = store.add_spend(CostEntryDelta(200, 20, 7_000, provider_attributed=True), None,
                             env.clock.now)
    assert (totals.provider_input_tokens, totals.provider_output_tokens) == (200, 20)
    assert (totals.total_input_tokens, totals.total_output_tokens) == (300, 30)
    assert totals.total_usd_micros == 12_000 and totals.provider_usd_micros == 7_000


def test_racing_spend_sums_exactly(env: Env) -> None:
    store = env.stores.costs
    delta = CostEntryDelta(10, 1, 100, provider_attributed=True)
    assert run_threads(24, lambda _i: store.add_spend(delta, None, env.clock.now)) == []
    totals = store.totals()
    assert totals.provider_input_tokens == 240 and totals.provider_output_tokens == 24
    assert totals.provider_usd_micros == 2_400


def test_the_spend_cap_trips_the_kill_switch_atomically(env: Env) -> None:
    store = env.stores.costs
    budget = SpendBudget(max_tokens=1_000, basis=SpendBasis.PROVIDER_ATTRIBUTED)
    first = store.add_spend(CostEntryDelta(400, 50, 0, provider_attributed=True), budget,
                            env.clock.now)
    assert first.over_budget is False and store.kill_switch().tripped is False
    second = store.add_spend(CostEntryDelta(500, 60, 0, provider_attributed=True), budget,
                             env.clock.now)
    assert second.over_budget is True
    state = store.kill_switch()
    assert state.tripped is True and state.reason == KillSwitchReason.SPEND_CAP


def test_the_usd_cap_also_trips(env: Env) -> None:
    store = env.stores.costs
    budget = SpendBudget(max_usd=Decimal("0.01"), basis=SpendBasis.PROVIDER_ATTRIBUTED)
    totals = store.add_spend(CostEntryDelta(1, 1, 10_000, provider_attributed=True), budget,
                             env.clock.now)
    assert totals.over_budget is True


def test_provider_basis_ignores_user_attributed_spend_but_the_default_counts_everything(env: Env) -> None:
    store = env.stores.costs
    user = CostEntryDelta(10_000, 0, 0, provider_attributed=False)
    provider_only = SpendBudget(max_tokens=100, basis=SpendBasis.PROVIDER_ATTRIBUTED)
    totals = store.add_spend(user, provider_only, env.clock.now)
    assert totals.over_budget is False and store.kill_switch().tripped is False
    assert SpendBudget(max_tokens=100).basis == SpendBasis.ALL  # a healthy-traffic cap must trip
    assert store.add_spend(user, SpendBudget(max_tokens=100), env.clock.now).over_budget is True


def test_the_spend_cap_holds_under_a_race(env: Env) -> None:
    store = env.stores.costs
    budget = SpendBudget(max_tokens=500, basis=SpendBasis.PROVIDER_ATTRIBUTED)
    delta = CostEntryDelta(30, 0, 0, provider_attributed=True)
    assert run_threads(24, lambda _i: store.add_spend(delta, budget, env.clock.now)) == []
    assert store.totals().provider_input_tokens == 24 * 30  # nothing lost
    assert store.kill_switch().tripped is True


def test_the_kill_switch_is_shared_and_only_an_explicit_reset_clears_it(env: Env) -> None:
    switch = StoreKillSwitch(env.stores.costs, env.clock)
    other = StoreKillSwitch(env.stores.costs, env.clock)  # another instance, same shared state
    assert switch.tripped is False
    switch.trip()
    assert other.tripped is True
    switch.trip()  # idempotent
    other.reset()
    assert switch.tripped is False


def test_the_kill_switch_fails_closed_when_the_store_is_unreadable() -> None:
    class Broken:
        def kill_switch(self):
            raise OSError("unavailable")

    assert StoreKillSwitch(Broken(), Clock()).tripped is True  # type: ignore[arg-type]


# --- display cache ------------------------------------------------------------------------------
@pytest.fixture(params=["memory", "fake-firestore"])
def cache_env(request: pytest.FixtureRequest) -> Env:
    return Env(request.param, cache_max_per_owner=3)


def test_cache_round_trip_expiry_and_hard_cap(cache_env: Env) -> None:
    cache, clock = cache_env.stores.display_cache, cache_env.clock
    answer = make_answer()
    cache.put("u1", "k1", W(answer))
    got = cache.get("u1", "k1")
    assert got is not None and got.answer == answer and got.principal_id == W(answer).principal_id
    assert cache.get("u2", "k1") is None  # owner scoped
    clock.now = answer.cache_expires_at
    assert cache.get("u1", "k1") is None
    from test_discovery_validator import forged
    long_lived = forged(answer, cache_expires_at=answer.retrieved_at + timedelta(days=90))
    clock.now = T0
    with pytest.raises(CacheError):
        cache.put("u1", "k2", W(long_lived))


def test_cache_per_owner_cap_evicts_the_owners_own_oldest(cache_env: Env) -> None:
    cache, clock = cache_env.stores.display_cache, cache_env.clock
    for i in range(5):
        cache.put("flooder", f"k{i}", W(make_answer()))
        clock.advance(seconds=1)
    cache.put("quiet", "q", W(make_answer()))
    assert cache.get("quiet", "q") is not None
    survivors = [i for i in range(5) if cache.get("flooder", f"k{i}") is not None]
    assert survivors == [2, 3, 4]


def test_cache_purge_and_delete_owner(cache_env: Env) -> None:
    cache, clock = cache_env.stores.display_cache, cache_env.clock
    answer = make_answer()
    cache.put("u1", "a", W(answer))
    cache.put("u1", "b", W(answer))
    cache.put("u2", "a", W(answer))
    assert cache.delete_owner("u1") == 2
    assert cache.get("u1", "a") is None and cache.get("u2", "a") is not None
    clock.now = answer.cache_expires_at + timedelta(seconds=1)
    assert cache.purge() == 1


def test_a_tampered_stored_answer_is_never_served() -> None:
    env = Env("fake-firestore")
    cache = env.stores.display_cache
    cache.put("u1", "k", W(make_answer()))
    assert env.client is not None
    for _path, doc in env.client.documents("display_cache"):
        doc["answer"]["text"] = doc["answer"]["text"] + " (edited)"
    assert cache.get("u1", "k") is None


def test_the_bundle_exposes_every_store(env: Env) -> None:
    s = env.stores
    for name in ("quota", "consents", "workspaces", "idempotency", "display_cache", "costs"):
        assert getattr(s, name) is not None
