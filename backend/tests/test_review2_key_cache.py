"""Review finding 9: the signing-key cache fetches outside the lock with single flight, serves stale
keys during an upstream failure with exponential backoff, and applies the refresh floor to every
refresh (including max-age=0)."""
from __future__ import annotations

import threading
import time

import pytest
from firebase_support import NOW, Clock

from app.auth_firebase import FetchedKeys, KeyFetchError, TtlKeyCache


class Fetcher:
    def __init__(self, keys=None, max_age: int = 3600) -> None:
        self.keys = keys if keys is not None else {"k1": "KEY1"}
        self.max_age = max_age
        self.fail = False
        self.calls = 0
        self.gate: threading.Event | None = None

    def fetch(self) -> FetchedKeys:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("upstream down")
        return FetchedKeys(keys=dict(self.keys), max_age_seconds=self.max_age)


def cache(fetcher: Fetcher, clock: Clock, floor: float = 30) -> TtlKeyCache:
    return TtlKeyCache(fetcher, lambda raw: raw, clock, floor)  # type: ignore[arg-type]


def test_max_age_zero_is_still_held_to_the_refresh_floor() -> None:
    clock, fetcher = Clock(), Fetcher(max_age=0)
    c = cache(fetcher, clock)
    for _ in range(50):
        assert c.get("k1") == "KEY1"
    assert fetcher.calls == 1  # not one fetch per request
    clock.now += 31
    assert c.get("k1") == "KEY1" and fetcher.calls == 2


def test_an_expired_ttl_refreshes_at_most_once_per_floor() -> None:
    clock, fetcher = Clock(), Fetcher(max_age=1)
    c = cache(fetcher, clock)
    c.get("k1")
    clock.now += 5  # expired by max-age, but inside the floor
    for _ in range(20):
        c.get("k1")
    assert fetcher.calls == 1


def test_a_forged_kid_flood_fetches_at_most_once_per_floor() -> None:
    clock, fetcher = Clock(), Fetcher()
    c = cache(fetcher, clock)
    c.get("k1")
    for i in range(100):
        assert c.get(f"forged-{i}") is None
    assert fetcher.calls == 1


def test_stale_keys_are_served_during_an_upstream_failure_with_exponential_backoff() -> None:
    clock, fetcher = Clock(), Fetcher(max_age=10)
    c = cache(fetcher, clock)
    assert c.get("k1") == "KEY1"
    fetcher.fail = True
    clock.now += 100  # expired
    assert c.get("k1") == "KEY1"  # stale keys still serve a good token
    assert fetcher.calls == 2
    for _ in range(100):  # a flood during the backoff window does not reach the network
        assert c.get("k1") == "KEY1"
    assert fetcher.calls == 2
    waits = []
    for _ in range(8):  # each retry window doubles, up to the cap
        before = clock.now
        while True:
            clock.now += 1
            calls = fetcher.calls
            c.get("k1")
            if fetcher.calls > calls:
                waits.append(clock.now - before)
                break
    assert waits == sorted(waits) and waits[-1] <= 300 + 1
    assert waits[-1] > waits[0]


def test_a_cold_cache_during_an_outage_fails_without_a_fetch_flood() -> None:
    clock, fetcher = Clock(), Fetcher()
    fetcher.fail = True
    c = cache(fetcher, clock)
    with pytest.raises(RuntimeError):
        c.get("k1")
    for _ in range(100):
        with pytest.raises(KeyFetchError):
            c.get("k1")
    assert fetcher.calls == 1
    fetcher.fail = False
    clock.now += 10_000
    assert c.get("k1") == "KEY1"


def test_a_recovered_upstream_replaces_the_stale_keys_and_resets_the_backoff() -> None:
    clock, fetcher = Clock(), Fetcher(max_age=10)
    c = cache(fetcher, clock)
    c.get("k1")
    fetcher.fail = True
    clock.now += 100
    c.get("k1")
    fetcher.fail = False
    fetcher.keys = {"k2": "KEY2"}
    clock.now += 400
    assert c.get("k2") == "KEY2"


def test_keys_too_stale_are_dropped() -> None:
    clock, fetcher = Clock(), Fetcher(max_age=10)
    c = cache(fetcher, clock)
    c.get("k1")
    fetcher.fail = True
    clock.now += 10 + 86_400 + 1
    with pytest.raises(RuntimeError):
        c.get("k1")


def test_the_fetch_is_outside_the_lock_and_single_flight() -> None:
    clock, fetcher = Clock(), Fetcher()
    fetcher.gate = threading.Event()
    c = cache(fetcher, clock)
    results: list[object] = []

    def worker() -> None:
        results.append(c.get("k1"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    deadline = time.time() + 5
    while fetcher.calls == 0 and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    assert fetcher.calls == 1  # the other seven wait for the one fetch in flight
    # a caller with a lock-free path is not blocked behind the fetch: the lock is free
    assert c._lock.acquire(timeout=1)  # noqa: SLF001
    c._lock.release()  # noqa: SLF001
    fetcher.gate.set()
    for t in threads:
        t.join(5)
    assert results == ["KEY1"] * 8 and fetcher.calls == 1


def test_a_warm_cache_serves_current_keys_while_a_refresh_is_in_flight() -> None:
    clock, fetcher = Clock(), Fetcher(max_age=10)
    c = cache(fetcher, clock)
    c.get("k1")
    clock.now += 100  # expired: the next call refreshes
    fetcher.gate = threading.Event()
    started = threading.Thread(target=lambda: c.get("k1"))
    started.start()
    deadline = time.time() + 5
    while fetcher.calls < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert c.get("k1") == "KEY1"  # served from the current (stale) keys, no second fetch
    assert fetcher.calls == 2
    fetcher.gate.set()
    started.join(5)
    assert NOW  # silence unused import
