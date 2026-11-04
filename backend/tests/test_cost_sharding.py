"""The whole-pilot spend counter is sharded across sub-documents (reads sum them and the legacy
single document). Bounded eventual consistency: the cap may overshoot by the in-flight turns."""
from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from fs_backend import MEMORY_FAKE_EMULATOR, client_for, txn_for

from app.stores import CostEntryDelta, SpendBasis, SpendBudget, firestore_stores, memory_stores
from app.stores.firestore import COST_TOTALS, COST_SHARDS

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(params=MEMORY_FAKE_EMULATOR)
def costs_env(request: pytest.FixtureRequest):
    clock = lambda: T0  # noqa: E731
    if request.param == "memory":
        return memory_stores(clock).costs, None
    client = client_for(request.param, clock)
    return firestore_stores(client, clock, transactional=txn_for(client)).costs, client


D = CostEntryDelta(10, 1, 100, provider_attributed=True)


def test_totals_sum_across_shards(costs_env) -> None:
    store, _client = costs_env
    for i in range(40):
        store.add_spend(D, None, T0, shard_key=f"run-{i}")
    totals = store.totals()
    assert totals.provider_input_tokens == 400 and totals.total_output_tokens == 40
    assert totals.provider_usd_micros == 4_000


def test_spend_spreads_over_bounded_shard_documents(costs_env) -> None:
    store, client = costs_env
    if client is None:
        pytest.skip("memory has one counter")
    for i in range(80):
        store.add_spend(D, None, T0, shard_key=f"run-{i}")
    docs = client.documents(COST_TOTALS)
    assert 2 <= len(docs) <= COST_SHARDS
    assert not any(path.endswith("/pilot") for path, _ in docs)  # new writes never use the legacy doc


def test_the_same_key_always_hits_the_same_shard(costs_env) -> None:
    store, client = costs_env
    if client is None:
        pytest.skip("memory has one counter")
    for _ in range(5):
        store.add_spend(D, None, T0, shard_key="one-run")
    assert len(client.documents(COST_TOTALS)) == 1


def test_the_legacy_single_document_is_still_read_and_counted(costs_env) -> None:
    store, client = costs_env
    if client is None:
        pytest.skip("memory has no legacy document")
    client.collection(COST_TOTALS).document("pilot").set({
        "schema_version": 1, "kind": "cost_totals", "total_input_tokens": 1000,
        "total_output_tokens": 0, "total_usd_micros": 0, "provider_input_tokens": 1000,
        "provider_output_tokens": 0, "provider_usd_micros": 0, "updated_at": T0})
    assert store.totals().provider_input_tokens == 1000
    after = store.add_spend(D, None, T0, shard_key="x")
    assert after.provider_input_tokens == 1010
    budget = SpendBudget(max_tokens=1_005, basis=SpendBasis.PROVIDER_ATTRIBUTED)
    assert store.add_spend(D, budget, T0, shard_key="y").over_budget is True


def test_the_cap_check_reads_the_sum_of_all_shards(costs_env) -> None:
    store, _client = costs_env
    budget = SpendBudget(max_tokens=500, basis=SpendBasis.PROVIDER_ATTRIBUTED)
    results = [store.add_spend(CostEntryDelta(100, 0, 0, provider_attributed=True), budget, T0,
                               shard_key=f"k{i}") for i in range(6)]
    assert [r.over_budget for r in results] == [False] * 4 + [True, True]
    assert store.kill_switch().tripped is True


def test_concurrent_spend_loses_nothing_and_trips_once(costs_env) -> None:
    store, client = costs_env
    budget = SpendBudget(max_tokens=300, basis=SpendBasis.PROVIDER_ATTRIBUTED)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            store.add_spend(CostEntryDelta(30, 0, 0, provider_attributed=True), budget, T0,
                            shard_key=f"t{i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6 if client is not None and
               type(client).__name__ == "EmulatorClient" else 16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []
    assert store.totals().provider_input_tokens == len(threads) * 30
    assert store.kill_switch().tripped is True
