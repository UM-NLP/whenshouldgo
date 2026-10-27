"""Behaviour only the REAL Firestore emulator can show. Skipped unless FIRESTORE_EMULATOR_HOST is set.

The emulator does NOT enforce TTL policies and serves queries without composite indexes, so these
tests prove what it can: the adapters' queries and transactions run on the real client library,
the retention sweep deletes what its query selects, and contention on one document is serialised.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fs_backend import SKIP_REASON, client_for, emulator_enabled, txn_for

from app.retention import sweep_firestore
from app.stores import firestore_stores
from app.stores.protocols import QuotaLimits, ReserveOutcome

pytestmark = pytest.mark.skipif(not emulator_enabled(), reason=SKIP_REASON)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def put(client, collection: str, doc_id: str, expires: datetime) -> None:
    client.collection(collection).document(doc_id).set(
        {"owner_id": "u1", "expires_at": expires, "kind": "x"})


def test_emulator_does_not_enforce_ttl_but_the_sweep_deletes_expired_documents() -> None:
    client = client_for("emulator")
    put(client, "workspaces", "old", NOW - timedelta(days=3))
    put(client, "workspaces", "live", NOW + timedelta(days=3))
    assert len(client.documents("workspaces")) == 2  # the emulator never deletes by TTL
    assert sweep_firestore(client, NOW)["workspaces"] == 1
    assert [p for p, _ in client.documents("workspaces")] == ["workspaces/live"]


def test_bounded_sweep_query_with_limit_runs_without_a_composite_index() -> None:
    client = client_for("emulator")
    for i in range(12):
        put(client, "history_records", f"h{i}", NOW - timedelta(days=1))
    assert sum(sweep_firestore(client, NOW, 5).values()) == 5
    assert len(client.documents("history_records")) == 7
    assert sum(sweep_firestore(client, NOW, 100).values()) == 7


@pytest.mark.parametrize("writers", [2, 3])
def test_a_few_concurrent_reservations_never_overspend(writers: int) -> None:
    """Small scale on purpose: on the real emulator many concurrent read-modify-write transactions
    on ONE document stall until lock timeouts (8 writers took over a minute and lost the race for
    the document), which the fake hides. No overspend is the invariant that must always hold."""
    client = client_for("emulator")
    stores = firestore_stores(client, lambda: NOW, transactional=txn_for(client))
    limits = QuotaLimits(turn_limit=1, trip_limit=1, window=None)
    results: list[object] = []
    lock = threading.Lock()

    def reserve(i: int) -> None:
        try:
            outcome = stores.quota.reserve("u1", f"key-{i}", limits, NOW)
        except Exception as error:  # noqa: BLE001 - recorded; the assertions inspect it
            outcome = error
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    usage = stores.quota.snapshot("u1", NOW, None)
    assert len(usage.reserved_keys) == 1
    assert results.count(ReserveOutcome.RESERVED) == 1
    assert results.count(ReserveOutcome.EXHAUSTED) == writers - 1
