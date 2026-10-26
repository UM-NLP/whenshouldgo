"""Review finding 19: one kill switch per provider path, with distinct reasons; the whole-pilot spend
cap still stops every path. Memory and the Firestore adapter on the fake."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from composition_support import build
from maps_simulator import FakePlacesTransport

from app import composition
from app.places import PlacesPriceTable
from app.stores import StoreKillSwitch, firestore_stores, memory_stores
from app.stores.fake_firestore import FakeFirestoreClient, fake_transactional
from app.stores.protocols import (
    KILL_PATH_DISCOVERY,
    KILL_PATH_GLOBAL,
    KILL_PATH_PLACES,
    CostEntryDelta,
    KillSwitchReason,
    SpendBudget,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", "fake-firestore"])
def costs(request: pytest.FixtureRequest):
    if request.param == "memory":
        return memory_stores(lambda: NOW).costs
    return firestore_stores(FakeFirestoreClient(lambda: NOW), lambda: NOW,
                            transactional=fake_transactional).costs


def switches(costs):
    discovery = StoreKillSwitch(costs, lambda: NOW)
    places = StoreKillSwitch(costs, lambda: NOW, trip_reason=KillSwitchReason.PLACES,
                             path=KILL_PATH_PLACES)
    return discovery, places


def test_a_places_trip_does_not_switch_discovery_off(costs) -> None:
    discovery, places = switches(costs)
    places.trip()
    assert places.tripped is True and discovery.tripped is False
    assert costs.kill_switch(KILL_PATH_PLACES).reason == KillSwitchReason.PLACES
    assert costs.kill_switch(KILL_PATH_DISCOVERY).tripped is False
    assert costs.kill_switch(KILL_PATH_GLOBAL).tripped is False


def test_a_discovery_trip_does_not_switch_places_off(costs) -> None:
    discovery, places = switches(costs)
    discovery.trip()
    assert discovery.tripped is True and places.tripped is False
    assert costs.kill_switch(KILL_PATH_DISCOVERY).reason == KillSwitchReason.DISCOVERY


def test_the_whole_pilot_spend_cap_stops_every_path(costs) -> None:
    discovery, places = switches(costs)
    costs.add_spend(CostEntryDelta(900, 0, 0, provider_attributed=False),
                    SpendBudget(max_tokens=100), NOW)
    assert costs.kill_switch().reason == KillSwitchReason.SPEND_CAP
    assert discovery.tripped is True and places.tripped is True


def test_reset_clears_only_that_path(costs) -> None:
    discovery, places = switches(costs)
    discovery.trip()
    places.trip()
    places.reset()
    assert places.tripped is False and discovery.tripped is True


def test_an_unknown_path_is_refused(costs) -> None:
    with pytest.raises(ValueError):
        costs.kill_switch("not-a-path")


def test_the_composition_gives_places_its_own_switch() -> None:
    table = PlacesPriceTable(
        version="t", reviewed=True, details_sku="d", routes_sku="r",
        details_usd_per_request=Decimal("0.01"), routes_usd_per_request=Decimal("0.005"))
    runtime = build(places_price_table=table, places_transport=FakePlacesTransport(),
                    spend_budget=composition.SpendBudget(max_usd=Decimal("50")))
    assert runtime.places_kill_switch is not runtime.kill_switch
    runtime.places_service._kill.trip()  # noqa: SLF001 - what repeated Places access-denied does
    assert runtime.places_service.tripped is True
    assert runtime.kill_switch.tripped is False
    assert runtime.discovery_client._kill_switch.tripped is False  # noqa: SLF001
