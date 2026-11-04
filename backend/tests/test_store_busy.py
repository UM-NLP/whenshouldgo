"""A store that exhausts its transaction retries is a typed, retryable 503 STORE_BUSY, never a
500: static text, no document ids, Retry-After 1-2 s, no allowance consumed, no double charge."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import ErrorReason
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService
from app.stores.busy import StoreBusy
from app.stores.fake_firestore import AbortedError, FakeFirestoreClient, fake_transactional
from app.stores.firestore import FirestoreQuotaStore
from app.stores.protocols import QuotaLimits
from app.stores.memory import MemoryQuotaStore

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
LIMITS = QuotaLimits(5, 5, None)
TOKENS = {"tok-a": VerifiedIdentity("guest-a", "anonymous")}
AUTH = {"Authorization": "Bearer tok-a"}


def _api_core(name: str):
    exceptions = pytest.importorskip("google.api_core.exceptions")
    return getattr(exceptions, name)


def _raisers():
    yield "fake-aborted", lambda: AbortedError("x")
    yield "retries", lambda: ValueError("Failed to commit transaction in 30 attempts.")
    for name in ("Aborted", "DeadlineExceeded", "ServiceUnavailable"):
        yield name, (lambda n=name: _api_core(n)("doc 12345"))


@pytest.mark.parametrize("make", [m for _n, m in _raisers()], ids=[n for n, _m in _raisers()])
def test_transaction_exhaustion_is_a_typed_store_busy(make) -> None:
    def failing(_fn):
        def run(_txn, *a, **k):
            raise make()
        return run

    store = FirestoreQuotaStore(FakeFirestoreClient(lambda: NOW), transactional=failing)
    with pytest.raises(StoreBusy) as caught:
        store.reserve("guest-a", "k1", LIMITS, NOW)
    assert str(caught.value) == "the store is busy"
    assert "12345" not in repr(caught.value) and caught.value.__cause__ is None


def test_an_unrelated_value_error_is_not_swallowed() -> None:
    def failing(_fn):
        def run(_txn, *a, **k):
            raise ValueError("something else")
        return run

    store = FirestoreQuotaStore(FakeFirestoreClient(lambda: NOW), transactional=failing)
    with pytest.raises(ValueError, match="something else"):
        store.reserve("guest-a", "k1", LIMITS, NOW)


def test_the_fake_attempt_limit_is_a_store_busy() -> None:
    client = FakeFirestoreClient(lambda: NOW)

    def always_abort(*_a, **_k):
        raise AbortedError("conflict")

    client._commit = always_abort  # type: ignore[method-assign]
    store = FirestoreQuotaStore(client, transactional=fake_transactional, max_attempts=2)
    with pytest.raises(StoreBusy):
        store.reserve("guest-a", "k1", LIMITS, NOW)


class FlakyQuota(MemoryQuotaStore):
    """Raises StoreBusy on the chosen operations while ``busy`` holds them."""

    def __init__(self, ops: set[str]) -> None:
        super().__init__(max_records=100)
        self.ops = ops

    def _maybe(self, op: str) -> None:
        if op in self.ops:
            raise StoreBusy()

    def reserve(self, *a, **k):
        self._maybe("reserve")
        return super().reserve(*a, **k)

    def commit(self, *a, **k):
        self._maybe("commit")
        return super().commit(*a, **k)

    def ensure(self, *a, **k):
        self._maybe("ensure")
        return super().ensure(*a, **k)

    def snapshot(self, *a, **k):
        self._maybe("snapshot")
        return super().snapshot(*a, **k)


def make(ops: set[str]):
    store = FlakyQuota(ops)
    quotas = QuotaService(store=store)
    app = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=quotas.policies),
        orchestrator=TurnOrchestrator(quotas, dev_scaffold=True))
    return TestClient(app), store, quotas


def assert_busy(response) -> None:
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["reason"] == ErrorReason.STORE_BUSY == "STORE_BUSY"
    assert body["code"] == "PROVIDER"
    assert body["message"] == "the service is busy; try again in a moment"
    assert 1 <= int(response.headers["Retry-After"]) <= 2


TURN = {"message": "Plan a trip to a restaurant"}


def test_busy_reserve_on_the_turn_path_is_a_503_and_consumes_nothing() -> None:
    client, store, _q = make({"reserve"})
    assert_busy(client.post("/v1/agent/turns", json=TURN, headers={**AUTH, "Idempotency-Key": "k"}))
    store.ops.clear()
    ok = client.post("/v1/agent/turns", json=TURN, headers={**AUTH, "Idempotency-Key": "k"})
    assert ok.status_code == 200
    assert store.snapshot("guest-a", NOW, None).successful_turns == 1


def test_busy_commit_releases_the_reservation_and_a_retry_charges_once() -> None:
    client, store, _q = make({"commit"})
    h = {**AUTH, "Idempotency-Key": "k"}
    assert_busy(client.post("/v1/agent/turns", json=TURN, headers=h))
    usage = store.snapshot("guest-a", NOW, None)
    assert usage.reserved_keys == set() and usage.successful_turns == 0
    store.ops.clear()
    assert client.post("/v1/agent/turns", json=TURN, headers=h).status_code == 200
    again = client.post("/v1/agent/turns", json=TURN, headers=h)  # replay
    assert again.status_code == 200
    assert store.snapshot("guest-a", NOW, None).successful_turns == 1


def test_busy_release_does_not_mask_the_typed_answer() -> None:
    client, store, _q = make({"commit"})
    original = store.release

    def busy_release(*_a, **_k):
        raise StoreBusy()

    store.release = busy_release  # type: ignore[method-assign]
    assert_busy(client.post("/v1/agent/turns", json=TURN, headers={**AUTH, "Idempotency-Key": "k"}))
    store.release = original  # type: ignore[method-assign]


@pytest.mark.parametrize("path", ["/v1/me/usage", "/v1/me/capabilities"])
def test_busy_on_an_unpaid_read_is_a_retryable_503(path: str) -> None:
    client, _s, _q = make({"ensure", "snapshot"})
    assert_busy(client.get(path, headers=AUTH))
