"""Review finding 1 and 6: the paid Places routes are not a generic proxy and are rate limited
per principal (tighter for App Check ABSENT/INVALID). Full composition, memory and Firestore."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from composition_support import build
from fastapi.testclient import TestClient
from maps_simulator import (
    FakePlacesTransport,
    places_ok,
    synth_id,
    synthetic_compute_route,
    synthetic_place_details,
)

from app import composition
from app.appcheck import AppCheckSignal
from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.consent import LOCATION_NOTICE_VERSION
from app.domain import ConsentPurpose
from app.places import PlacesPriceTable
from app.stores.fake_firestore import FakeFirestoreClient

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
OWN, FOREIGN = synth_id(1), synth_id(2)
BUDGET = composition.SpendBudget(max_tokens=10_000_000, max_usd=Decimal("100"))
REVIEWED = PlacesPriceTable(
    version="t-1", reviewed=True, details_sku="places_details_test", routes_sku="routes_test",
    details_usd_per_request=Decimal("0.01"), routes_usd_per_request=Decimal("0.005"))
TOKENS = {"tok-a": VerifiedIdentity("reg-a", "google.com"),
          "tok-b": VerifiedIdentity("reg-b", "google.com")}
HA = {"Authorization": "Bearer tok-a", "Idempotency-Key": "edit-key-0001"}


def make(kind: str, n_responses: int = 10):
    transport = FakePlacesTransport(
        [places_ok(synthetic_place_details(1)) for _ in range(n_responses)])
    kwargs = dict(identity_verifier=StaticTokenVerifier(TOKENS), clock=lambda: NOW,
                  places_price_table=REVIEWED, places_transport=transport, spend_budget=BUDGET)
    if kind == "firestore":
        kwargs.update(firestore_client=FakeFirestoreClient(), allow_in_memory_state=False)
    runtime = build(**kwargs)
    return runtime, TestClient(runtime.app, raise_server_exceptions=False), transport


def select(http: TestClient, place_id: str, who: str = "tok-a") -> None:
    edit = {"base_version": 0, "upsert_facts": [{"key": "selected_place_id", "value": place_id}]}
    done = http.patch("/v1/workspace", json=edit, headers={
        "Authorization": f"Bearer {who}", "Idempotency-Key": "edit-key-0001"})
    assert done.status_code == 200, done.text


def details(http: TestClient, ids, who: str = "tok-a"):
    return http.post("/v1/places/details", json={"place_ids": list(ids)},
                     headers={"Authorization": f"Bearer {who}"})


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_a_foreign_place_id_is_a_typed_404_with_no_google_call_and_no_cost(kind: str) -> None:
    runtime, http, transport = make(kind)
    select(http, OWN)
    response = details(http, [FOREIGN])
    assert response.status_code == 404 and response.json()["code"] == "VALIDATION"
    assert transport.requests == []
    assert runtime.costs.entries() == ()


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_a_mixed_request_is_refused_whole(kind: str) -> None:
    _, http, transport = make(kind)
    select(http, OWN)
    assert details(http, [OWN, FOREIGN]).status_code == 404
    assert transport.requests == []


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_the_selected_place_is_allowed_and_another_principal_cannot_use_it(kind: str) -> None:
    _, http, transport = make(kind)
    select(http, OWN)
    assert details(http, [OWN]).status_code == 200
    assert len(transport.requests) == 1
    assert details(http, [OWN], who="tok-b").status_code == 404  # b never referenced it
    assert len(transport.requests) == 1


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_a_caller_with_no_workspace_gets_404_for_any_id(kind: str) -> None:
    _, http, transport = make(kind)
    assert details(http, [OWN]).status_code == 404
    assert transport.requests == []


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_route_check_has_the_same_ownership_rule(kind: str) -> None:
    runtime, http, transport = make(kind)
    transport.push(places_ok(synthetic_compute_route()))
    select(http, OWN)
    runtime.app.state.consents.grant("reg-a", ConsentPurpose.LOCATION_FOREGROUND,
                                     LOCATION_NOTICE_VERSION)
    loc = {"latitude": 43.65321, "longitude": -79.38321}
    refused = http.post("/v1/routes/check", json={"place_id": FOREIGN, "location": loc},
                        headers={"Authorization": "Bearer tok-a"})
    assert refused.status_code == 404 and transport.requests == []


def test_more_than_three_ids_is_still_a_validation_error() -> None:
    _, http, transport = make("memory")
    select(http, OWN)
    assert details(http, [OWN, synth_id(3), synth_id(4), synth_id(5)]).status_code == 422
    assert transport.requests == []


@pytest.mark.parametrize("kind", ["memory", "firestore"])
def test_display_cache_place_ids_are_scoped_to_the_owner_and_expiry(kind: str) -> None:
    from datetime import timedelta

    from maps_simulator import NOW as MAPS_NOW
    from maps_simulator import wrap_unvalidated as W
    from test_discovery_contracts import make_answer

    from app.discovery.cache import DisplayCache
    from app.stores.firestore import FirestoreDisplayCache

    clock = {"now": MAPS_NOW}
    answer = make_answer()
    cache = (DisplayCache(lambda: clock["now"]) if kind == "memory" else
             FirestoreDisplayCache(FakeFirestoreClient(), lambda: clock["now"]))
    cache.put("reg-a", "k1", W(answer))
    ids = {p.place_id for p in answer.places}
    assert cache.place_ids("reg-a") == ids and ids
    assert cache.place_ids("reg-b") == frozenset()
    clock["now"] = answer.cache_expires_at + timedelta(seconds=1)
    assert cache.place_ids("reg-a") == frozenset()


def test_no_place_references_configured_refuses_everything() -> None:
    from app.places.ownership import NoPlaceReferences
    assert NoPlaceReferences().permits("u", [OWN]) is False


# --- limiter and App Check (finding 6) -----------------------------------------------------------
def test_places_calls_are_rate_limited_per_principal() -> None:
    runtime, http, _ = make("memory", n_responses=0)
    select(http, OWN)
    runtime.app.state.paid_limiter = type(runtime.app.state.paid_limiter)(
        lambda: NOW, max_operations=3)
    codes = [details(http, [OWN]).status_code for _ in range(5)]
    assert codes[3:] == [429, 429]
    assert details(http, [OWN], who="tok-b").status_code != 429  # b has its own window


def test_absent_or_invalid_app_check_gets_the_tighter_window_never_a_ban() -> None:
    runtime, http, _ = make("memory", n_responses=0)
    select(http, OWN)

    class Monitor:  # monitor mode: records the signal, never blocks
        signal = AppCheckSignal.ABSENT

        def __call__(self, request, header):
            request.state.app_check_signal = self.signal
            return self.signal

    policy = Monitor()
    runtime.app.state.app_check_policy = policy
    limiter = type(runtime.app.state.unverified_paid_limiter)
    runtime.app.state.unverified_paid_limiter = limiter(lambda: NOW, max_operations=2)
    statuses = [details(http, [OWN]).status_code for _ in range(4)]
    assert statuses[2:] == [429, 429] and 429 not in statuses[:2]
    # A verified client is not held to the tighter window.
    policy.signal = AppCheckSignal.VERIFIED
    assert details(http, [OWN]).status_code != 429
    policy.signal = AppCheckSignal.INVALID
    assert details(http, [OWN], who="tok-b").status_code == 404  # still served (not banned)
