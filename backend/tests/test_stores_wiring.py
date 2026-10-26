"""The in-process Firestore fake itself, the composition wiring of the stores, durability across
instances, and what the stored documents may contain (canary scan).

The fake is NOT real Firestore: it proves our adapters' logic and transaction usage, not indexes,
TTL-policy timing or contention (docs/backlog.md lists the real-emulator test as a TODO)."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

import pytest
from composition_support import CANARY, ENV, HEADERS, MESSAGE, ProviderModel, build
from fastapi.testclient import TestClient
from location_support import CANARIES, LOCATION
from maps_simulator import FakeMcpTransport, mcp_ok
from wiring_support import TITLE_CANARY, canary_payload

from app.composition import ConfigurationError
from app.domain import Principal, PrincipalKind
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService
from app.stores import SpendBudget, firestore_stores, memory_stores
from app.stores.fake_firestore import (
    SERVER_TIMESTAMP,
    AbortedError,
    AlreadyExistsError,
    FakeFirestoreClient,
    NotFoundError,
    fake_transactional,
)

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


# --- the fake's own contract --------------------------------------------------------------------
def test_a_conflicting_write_aborts_the_transaction_and_it_is_retried() -> None:
    client = FakeFirestoreClient(Clock())
    ref = client.collection("c").document("d")
    ref.set({"n": 1})
    calls: list[int] = []

    @fake_transactional
    def bump(txn):
        snap = ref.get(transaction=txn)
        calls.append(1)
        if len(calls) == 1:
            ref.set({"n": 100})  # a concurrent writer commits between our read and commit
        txn.set(ref, {"n": snap.to_dict()["n"] + 1})

    bump(client.transaction())
    assert ref.get().to_dict() == {"n": 101} and len(calls) == 2


def test_a_transaction_that_always_conflicts_gives_up() -> None:
    client = FakeFirestoreClient(Clock())
    ref = client.collection("c").document("d")
    ref.set({"n": 0})
    tries: list[int] = []

    @fake_transactional
    def never(txn):
        ref.get(transaction=txn)
        tries.append(1)
        ref.set({"n": len(tries)})
        txn.set(ref, {"n": -1})

    with pytest.raises(AbortedError):
        never(client.transaction(max_attempts=3))
    assert len(tries) == 3


def test_reads_after_writes_in_a_transaction_are_refused_like_the_real_service() -> None:
    client = FakeFirestoreClient(Clock())
    ref = client.collection("c").document("d")

    @fake_transactional
    def bad(txn):
        txn.set(ref, {"a": 1})
        ref.get(transaction=txn)

    with pytest.raises(RuntimeError):
        bad(client.transaction())


class _Color(StrEnum):
    RED = "red"


@pytest.mark.parametrize("value", [Decimal("1"), (1, 2), {1, 2}, math.nan, _Color.RED, object(),
                                   {"nested": (1,)}, datetime(2026, 1, 1)])
def test_values_real_firestore_would_not_take_are_rejected(value: object) -> None:
    ref = FakeFirestoreClient(Clock()).collection("c").document("d")
    with pytest.raises(TypeError):
        ref.set({"v": value})


def test_server_timestamp_and_ttl_sweep() -> None:
    clock = Clock()
    client = FakeFirestoreClient(clock)
    col = client.collection("c")
    col.document("a").set({"at": SERVER_TIMESTAMP, "expires_at": T0 + timedelta(hours=1)})
    col.document("b").set({"expires_at": T0 + timedelta(days=2)})
    assert col.document("a").get().to_dict()["at"] == T0
    assert client.run_ttl_sweep("expires_at") == 0
    clock.now = T0 + timedelta(hours=2)
    assert client.run_ttl_sweep("expires_at") == 1
    assert not col.document("a").get().exists and col.document("b").get().exists


def test_update_create_delete_and_ids() -> None:
    col = FakeFirestoreClient(Clock()).collection("c")
    with pytest.raises(NotFoundError):
        col.document("x").update({"a": 1})
    col.document("x").create({"a": 1})
    with pytest.raises(AlreadyExistsError):
        col.document("x").create({"a": 2})
    col.document("x").update({"b": 2})
    assert col.document("x").get().to_dict() == {"a": 1, "b": 2}
    col.document("x").delete()
    assert not col.document("x").get().exists
    for bad in (".", "..", "__reserved__", "a/b", ""):
        with pytest.raises(ValueError):
            col.document(bad)


def test_queries_filter_on_equality_and_inequality() -> None:
    col = FakeFirestoreClient(Clock()).collection("c")
    col.document("1").set({"o": "u1", "n": 1})
    col.document("2").set({"o": "u1", "n": 5})
    col.document("3").set({"o": "u2", "n": 9})
    assert sorted(s.id for s in col.where("o", "==", "u1").stream()) == ["1", "2"]
    assert [s.id for s in col.where("n", "<=", 1).stream()] == ["1"]


# --- orchestrator on the stores -----------------------------------------------------------------
TRAVEL = "Plan a trip to a quiet Toronto restaurant this evening"


def _orchestrator(stores, clock, kind=PrincipalKind.GUEST):
    quotas = QuotaService(store=stores.quota, clock=clock)
    orchestrator = TurnOrchestrator(
        quotas, clock=clock, dev_scaffold=True,
        workspace_store=stores.workspaces, idempotency_store=stores.idempotency)
    return orchestrator, Principal("u1", kind, quotas.policies.current().version)


@pytest.mark.parametrize("backend", ["memory", "fake-firestore"])
def test_a_guest_workspace_expires_after_seven_inactive_days_through_the_orchestrator(backend) -> None:
    clock = Clock()
    stores = (memory_stores(clock) if backend == "memory" else
              firestore_stores(FakeFirestoreClient(clock), clock, transactional=fake_transactional))
    orchestrator, guest = _orchestrator(stores, clock)
    first = orchestrator.run_turn(guest, TRAVEL, "k1").workspace
    clock.now += timedelta(days=6)
    second = orchestrator.run_turn(guest, TRAVEL, "k2").workspace
    assert second.id == first.id and second.version == 2
    clock.now += timedelta(days=8)
    third = orchestrator.run_turn(guest, TRAVEL, "k3").workspace
    assert third.id != first.id and third.version == 1  # the inactive workspace is gone


def test_a_registered_workspace_does_not_expire() -> None:
    clock = Clock()
    stores = memory_stores(clock)
    orchestrator, registered = _orchestrator(stores, clock, PrincipalKind.REGISTERED_FREE)
    first = orchestrator.run_turn(registered, TRAVEL, "k1").workspace
    clock.now += timedelta(days=60)
    assert orchestrator.run_turn(registered, TRAVEL, "k2").workspace.id == first.id


def test_replay_and_quota_survive_a_new_orchestrator_on_the_same_firestore_state() -> None:
    clock = Clock()
    client = FakeFirestoreClient(clock)
    stores = firestore_stores(client, clock, transactional=fake_transactional)
    one, guest = _orchestrator(stores, clock)
    result = one.run_turn(guest, TRAVEL, "k1")
    two, _ = _orchestrator(firestore_stores(client, clock, transactional=fake_transactional), clock)
    replay = two.run_turn(guest, TRAVEL, "k1")
    assert replay.workspace == result.workspace and replay.reply == result.reply
    assert two.quotas.usage_for("u1").successful_turns == 1


# --- composition --------------------------------------------------------------------------------
BUDGET = SpendBudget(max_tokens=10_000_000)


def test_production_fails_closed_without_valid_state_or_a_budget(monkeypatch) -> None:
    with pytest.raises(ConfigurationError, match="shared state not implemented"):
        build(allow_in_memory_state=False)
    client = FakeFirestoreClient(Clock())
    with pytest.raises(ConfigurationError, match="spend budget"):
        build(allow_in_memory_state=False, firestore_client=client)  # durable state needs a cap
    build(allow_in_memory_state=False, firestore_client=client, spend_budget=BUDGET)  # accepted
    with pytest.raises(ConfigurationError):
        build(allow_in_memory_state=False, env={**ENV, "FIRESTORE_PROJECT_ID": "Not A Project!"},
              spend_budget=BUDGET)
    import app.stores.firestore as firestore_module

    def missing(_project: str):
        raise ImportError("google-cloud-firestore is not installed")

    monkeypatch.setattr(firestore_module, "make_client", missing)
    with pytest.raises(ConfigurationError, match="firestore"):
        build(allow_in_memory_state=False, env={**ENV, "FIRESTORE_PROJECT_ID": "demo-project-1"},
              spend_budget=BUDGET)
    monkeypatch.setattr(firestore_module, "make_client", lambda _p: client)
    runtime = build(allow_in_memory_state=False, env={**ENV, "FIRESTORE_PROJECT_ID": "demo-project-1"},
                    spend_budget=BUDGET)
    assert runtime.stores.kind == "firestore"


def test_memory_state_still_works_in_dev_and_is_labelled() -> None:
    assert build().stores.kind == "memory"


def test_optional_hooks_for_identity_and_places_are_passed_through() -> None:
    marker = object()
    runtime = build(places_service=marker, app_check_verifier=marker)
    assert runtime.app.state.places_service is marker
    assert runtime.app.state.app_check_verifier is marker
    assert build().app.state.places_service is None


def _turn(runtime, key: str, **extra):
    return TestClient(runtime.app).post(
        "/v1/agent/turns", json={**MESSAGE, **extra}, headers={**HEADERS, "Idempotency-Key": key})


def test_a_turn_replays_after_a_restart_with_no_new_provider_call() -> None:
    client = FakeFirestoreClient(Clock())
    first_model = ProviderModel()
    one = build(firestore_client=client, spend_budget=BUDGET, model_transport=first_model)
    original = _turn(one, "k-1")
    assert original.status_code == 200, original.text
    second_model = ProviderModel()
    two = build(firestore_client=client, spend_budget=BUDGET, model_transport=second_model,
                discovery_transport=FakeMcpTransport([]))
    replay = _turn(two, "k-1")
    assert replay.status_code == 200 and replay.json() == original.json()
    assert second_model.requests == []
    assert two.quotas.usage_for("guest-comp").successful_turns == 1


def test_the_whole_pilot_spend_cap_stops_new_paid_work_and_trips_the_shared_kill_switch() -> None:
    client = FakeFirestoreClient(Clock())
    model = ProviderModel()
    runtime = build(firestore_client=client, spend_budget=SpendBudget(max_tokens=100),
                    model_transport=model)
    assert _turn(runtime, "k-1").status_code == 200  # 310 tokens spent: now over the cap
    assert runtime.kill_switch.tripped is True
    assert runtime.costs.totals().total_input_tokens == 250
    blocked = _turn(runtime, "k-2")
    assert blocked.status_code == 503 and blocked.json()["reason"] == "PROVIDER_UNAVAILABLE"
    assert len(model.requests) == 1  # no second provider call
    assert runtime.quotas.usage_for("guest-comp").successful_turns == 1  # nothing consumed


# --- what the stored documents may contain ------------------------------------------------------
def _everything_stored(client: FakeFirestoreClient) -> list[tuple[str, dict]]:
    return list(client.documents())


def test_no_maps_content_or_precise_location_in_any_stored_document_except_the_display_cache() -> None:
    client = FakeFirestoreClient(Clock())
    runtime = build(firestore_client=client, spend_budget=BUDGET,
                    discovery_transport=FakeMcpTransport([mcp_ok(canary_payload())]))
    consent = {"purpose": "LOCATION_FOREGROUND", "notice_version": "location-notice-1"}
    http = TestClient(runtime.app)
    auth = {"Authorization": HEADERS["Authorization"]}
    assert http.post("/v1/me/consents", json=consent, headers=auth).status_code in (200, 201)
    response = _turn(runtime, "k-1", location=LOCATION)
    assert response.status_code == 200, response.text
    assert CANARY in response.json()["discovery"]["text"]
    docs = _everything_stored(client)
    assert docs, "nothing was stored"
    cache_docs = [d for path, d in docs if path.startswith("display_cache/")]
    assert cache_docs and any(CANARY in repr(d) for d in cache_docs)  # redisplay copy, <= 30 days
    for path, doc in docs:
        text = repr(doc)
        for secret in CANARIES:
            assert secret not in text, f"precise location in {path}"
        if not path.startswith("display_cache/"):
            assert CANARY not in text and TITLE_CANARY not in text, f"Maps content in {path}"
    for path, doc in docs:
        assert isinstance(doc.get("schema_version"), int) and doc["schema_version"] >= 1, path
        assert isinstance(doc.get("kind"), str) and doc["kind"], path


def test_every_collection_the_adapters_write_is_known() -> None:
    client = FakeFirestoreClient(Clock())
    runtime = build(firestore_client=client, spend_budget=BUDGET)
    assert _turn(runtime, "k-1").status_code == 200
    TestClient(runtime.app).post(
        "/v1/me/consents", json={"purpose": "LOCATION_FOREGROUND",
                                 "notice_version": "location-notice-1"},
        headers={"Authorization": HEADERS["Authorization"]})
    collections = {path.split("/")[0] for path, _ in client.documents()}
    assert collections <= {"quota_usage", "workspaces", "idempotency", "display_cache",
                           "consent_ledgers", "cost_totals", "kill_switch"}
    assert {"quota_usage", "workspaces", "idempotency", "display_cache", "cost_totals",
            "consent_ledgers"} <= collections


def test_document_ids_do_not_contain_the_raw_principal_id() -> None:
    client = FakeFirestoreClient(Clock())
    runtime = build(firestore_client=client, spend_budget=BUDGET)
    assert _turn(runtime, "k-1").status_code == 200
    assert all("guest-comp" not in path for path, _ in client.documents())



@pytest.mark.parametrize("name,value", [("PILOT_SPEND_MAX_TOKENS", "abc"),
                                        ("PILOT_SPEND_MAX_TOKENS", "0"),
                                        ("PILOT_SPEND_MAX_USD", "-1"),
                                        ("PILOT_SPEND_MAX_USD", "lots")])
def test_a_malformed_spend_budget_is_refused_not_ignored(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match="spend budget"):
        build(env={**ENV, name: value}, firestore_client=FakeFirestoreClient(Clock()))


def test_the_budget_can_come_from_env() -> None:
    runtime = build(env={**ENV, "PILOT_SPEND_MAX_USD": "5.00"},
                    firestore_client=FakeFirestoreClient(Clock()))
    assert runtime.stores.kind == "firestore"
