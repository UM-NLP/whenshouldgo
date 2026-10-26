"""L7: when the global cap is reached a writer over its fair share evicts its OWN oldest entry;
only a writer within its share takes from the largest owner. A tenant at its share cannot
evict others, and a flooder cannot push a well-behaved tenant below its share."""
from __future__ import annotations

from maps_simulator import NOW, SyntheticMapsClient, new_run, wrap_unvalidated
from wiring_support import MutableClock, canary_payload

from app.discovery import DisplayCache, parse_grounded_answer
from test_gateway_support import make_config

CONFIG = make_config()


def answer_for(owner: str, index: int):
    client = SyntheticMapsClient(payload=canary_payload(canary=f"C{index}"))
    run = new_run(principal_id=owner, run_id=f"run-{owner}-{index}")
    result = client.call_tool(run, "search_places", {"text_query": "q"})
    return wrap_unvalidated(parse_grounded_answer(result, CONFIG.ttls), owner)


def fill(cache: DisplayCache, owner: str, count: int, start: int = 0) -> None:
    for i in range(start, start + count):
        cache.put(owner, f"k{i}", answer_for(owner, i))


def keys(cache: DisplayCache, owner: str, upto: int = 30) -> list[str]:
    return [f"k{i}" for i in range(upto) if cache.get(owner, f"k{i}") is not None]


def test_a_tenant_at_its_share_evicts_only_its_own_entries() -> None:
    cache = DisplayCache(MutableClock(NOW), max_entries=6, max_per_owner=10)
    fill(cache, "a", 3)
    fill(cache, "b", 3)
    assert len(cache) == 6
    fill(cache, "a", 2, start=3)  # a is at its share (3 of 6 with two owners) and keeps writing
    assert keys(cache, "b") == ["k0", "k1", "k2"], "a tenant at its share evicted another tenant"
    assert keys(cache, "a") == ["k2", "k3", "k4"]  # a's own oldest went first


def test_a_flooder_cannot_push_a_well_behaved_tenant_below_its_share() -> None:
    cache = DisplayCache(MutableClock(NOW), max_entries=6, max_per_owner=10)
    fill(cache, "flood", 6)
    fill(cache, "calm", 3)
    assert len(keys(cache, "calm")) == 3
    fill(cache, "flood", 10, start=6)
    assert len(keys(cache, "calm")) == 3 and len(cache) == 6


def test_a_new_tenant_takes_from_the_largest_owner() -> None:
    cache = DisplayCache(MutableClock(NOW), max_entries=4, max_per_owner=10)
    fill(cache, "big", 4)
    fill(cache, "new", 1)
    assert len(keys(cache, "new")) == 1 and len(keys(cache, "big")) == 3
    assert keys(cache, "big") == ["k1", "k2", "k3"]


def test_eviction_never_drops_the_entry_just_written() -> None:
    cache = DisplayCache(MutableClock(NOW), max_entries=2, max_per_owner=10)
    fill(cache, "a", 2)
    cache.put("a", "latest", answer_for("a", 99))
    assert cache.get("a", "latest") is not None and len(cache) == 2
