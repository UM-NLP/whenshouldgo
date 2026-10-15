"""In-memory DisplayCache: TTL, purge, delete-by-owner and the 30-day hard cap."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from maps_simulator import NOW
from test_discovery_contracts import make_answer
from test_discovery_validator import forged, run_and_answer

from app.discovery import CacheError, DisplayCache, DisplayCacheStore
from app.domain import MAX_MAPS_CACHE


class DtClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def test_the_interface_is_a_protocol_the_firestore_store_can_implement_later() -> None:
    assert isinstance(DisplayCache(DtClock()), DisplayCacheStore)


def test_an_entry_is_served_until_its_cache_expiry_and_never_after() -> None:
    clock = DtClock()
    cache = DisplayCache(clock)
    answer = make_answer()
    cache.put("u1", "k1", answer)
    assert cache.get("u1", "k1") is answer
    clock.now = answer.cache_expires_at - timedelta(seconds=1)
    assert cache.get("u1", "k1") is answer
    clock.now = answer.cache_expires_at  # the boundary is already expired
    assert cache.get("u1", "k1") is None
    assert len(cache) == 0  # an expired read also removes the entry


def test_expiry_is_enforced_even_if_purge_never_runs() -> None:
    clock = DtClock()
    cache = DisplayCache(clock)
    cache.put("u1", "k1", make_answer())
    clock.advance(timedelta(days=40))
    assert cache.get("u1", "k1") is None


def test_an_entry_is_never_served_past_thirty_days_from_retrieval() -> None:
    answer = make_answer()
    # contract-invalid: 90 days (built around the validators)
    long_lived = forged(answer, cache_expires_at=answer.retrieved_at + timedelta(days=90))
    cache = DisplayCache(DtClock())
    with pytest.raises(CacheError):
        cache.put("u1", "k1", long_lived)
    assert len(cache) == 0
    # exactly the hard maximum is allowed, but only until that instant
    edge = forged(answer, cache_expires_at=answer.retrieved_at + MAX_MAPS_CACHE)
    clock = DtClock()
    cache = DisplayCache(clock)
    cache.put("u1", "edge", edge)
    clock.now = answer.retrieved_at + MAX_MAPS_CACHE - timedelta(seconds=1)
    assert cache.get("u1", "edge") is edge
    clock.now = answer.retrieved_at + MAX_MAPS_CACHE
    assert cache.get("u1", "edge") is None


def test_already_expired_future_dated_and_invalid_answers_are_not_stored() -> None:
    clock = DtClock()
    cache = DisplayCache(clock)
    answer = make_answer()
    clock.now = answer.cache_expires_at + timedelta(seconds=1)
    with pytest.raises(CacheError):
        cache.put("u1", "k1", answer)
    clock.now = NOW
    with pytest.raises(CacheError):
        cache.put("u1", "k1", forged(answer, content_class="MAPS_PLACES"))
    with pytest.raises(CacheError):
        cache.put("u1", "k1", {"text": "x"})  # type: ignore[arg-type]
    with pytest.raises(CacheError):
        cache.put("u1", "k1", forged(answer, cache_expires_at=answer.retrieved_at))


def test_purge_removes_only_expired_entries_and_reports_the_count() -> None:
    clock = DtClock()
    cache = DisplayCache(clock)
    short = forged(make_answer(), cache_expires_at=NOW + timedelta(hours=1))
    cache.put("u1", "short", short)
    cache.put("u1", "long", make_answer())
    clock.advance(timedelta(hours=2))
    assert cache.purge() == 1 and len(cache) == 1
    assert cache.get("u1", "long") is not None


def test_delete_by_owner_removes_everything_for_that_owner_only() -> None:
    cache = DisplayCache(DtClock())
    answer = make_answer()
    cache.put("u1", "a", answer)
    cache.put("u1", "b", answer)
    cache.put("u2", "a", answer)
    assert cache.delete_owner("u1") == 2
    assert cache.get("u1", "a") is None and cache.get("u1", "b") is None
    assert cache.get("u2", "a") is answer
    assert cache.delete_owner("nobody") == 0


def test_entries_are_isolated_per_owner() -> None:
    cache = DisplayCache(DtClock())
    cache.put("u1", "k", make_answer())
    assert cache.get("u2", "k") is None


def test_keys_and_owner_ids_are_bounded_identifiers() -> None:
    cache = DisplayCache(DtClock())
    answer = make_answer()
    for owner, key in (("", "k"), ("u 1", "k"), ("u1", ""), ("u1", "k/../x"), ("u1", "k" * 200),
                       ("u" * 200, "k")):
        with pytest.raises(CacheError):
            cache.put(owner, key, answer)
        with pytest.raises(CacheError):
            cache.get(owner, key)


def test_capacity_evicts_the_oldest_entry_first() -> None:
    cache = DisplayCache(DtClock(), max_entries=2)
    answer = make_answer()
    cache.put("u", "1", answer)
    cache.put("u", "2", answer)
    cache.put("u", "3", answer)
    assert len(cache) == 2 and cache.get("u", "1") is None and cache.get("u", "3") is answer


def test_the_clock_must_be_timezone_aware() -> None:
    cache = DisplayCache(lambda: NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        cache.put("u", "k", make_answer())


def test_the_cache_never_logs_or_exposes_content_in_repr() -> None:
    cache = DisplayCache(DtClock())
    cache.put("u", "k", make_answer())
    assert "Synthetic" not in repr(cache) and "SYNTH_PLACE" not in repr(cache)
    assert run_and_answer  # imported helper kept for symmetry with the validator tests


# --- L9: per-owner cap, future timestamps, integrity on read -------------------------------------
def test_one_owner_flooding_the_cache_cannot_evict_another_owners_entries() -> None:
    cache = DisplayCache(DtClock(), max_entries=20, max_per_owner=3)
    answer = make_answer()
    cache.put("victim", "k", answer)
    for i in range(50):
        cache.put("attacker", f"k{i}", answer)
    assert cache.get("victim", "k") is answer
    assert sum(1 for i in range(50) if cache.get("attacker", f"k{i}") is not None) == 3
    assert len(cache) == 4


def test_the_global_capacity_still_holds_when_many_owners_each_stay_under_their_cap() -> None:
    cache = DisplayCache(DtClock(), max_entries=5, max_per_owner=3)
    answer = make_answer()
    for i in range(12):
        cache.put(f"u{i}", "k", answer)
    assert len(cache) == 5


def test_future_dated_and_overlong_retrieval_times_are_not_stored() -> None:
    clock = DtClock()
    cache = DisplayCache(clock)
    answer = make_answer()
    future = forged(answer, retrieved_at=NOW + timedelta(hours=2), cache_expires_at=NOW + timedelta(hours=26))
    with pytest.raises(CacheError):
        cache.put("u", "k", future)
    old = forged(answer, retrieved_at=NOW - timedelta(days=31), cache_expires_at=NOW - timedelta(days=30))
    with pytest.raises(CacheError):
        cache.put("u", "k", old)
    assert len(cache) == 0


def test_the_content_hashes_are_rechecked_on_every_read() -> None:
    cache = DisplayCache(DtClock())
    answer = make_answer()
    cache.put("u", "k", answer)
    cache._entries[("u", "k")] = forged(answer, text=answer.text + " (corrupted)")  # noqa: SLF001
    assert cache.get("u", "k") is None and len(cache) == 0
    cache.put("u", "k2", answer)
    cache._entries[("u", "k2")] = forged(answer, display_hash="0" * 64)  # noqa: SLF001
    assert cache.get("u", "k2") is None
