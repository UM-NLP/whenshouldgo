"""Item 5: server-rendered Place Details and route checks (display-only, never seen by a model).

Synthetic data only (tests/maps_simulator.py). No network: a scripted transport stands in for
Google, and an httpx.MockTransport exercises the real client's wire rules.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app import telemetry as t
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.composition import pinned_runtime_config
from app.consent import LOCATION_NOTICE_VERSION
from app.costs import Attribution, CostLedger
from app.discovery.client import KillSwitch
from app.domain import ConsentPurpose, PlaceCard, RouteCheck, RouteMode
from app.gateway import GatewayPolicy
from app.gateway.errors import TransportFailure, TransportTimeout
from app.gateway.openrouter import Credential
from app.location import RoundedLocation
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.places import (
    UNREVIEWED_PRICE_TABLE,
    HttpxPlacesTransport,
    PlacesLimits,
    PlacesPriceTable,
    PlacesRequest,
    build_places_service,
    validate_place_ids,
)
from app.places.errors import PlacesError, PlacesInvalidRequest
from app.places.render import describe_route
from app.places.transport import (
    DETAILS_FIELD_MASK,
    MASK_HEADER,
    MAX_RESPONSE_BYTES,
    ROUTES_FIELD_MASK,
)
from app.quota import QuotaService
from tests.gateway_fakes import FakeGateway
from tests.maps_simulator import (
    PLACES_CANARIES,
    PLACES_CANARY_KEY,
    FakePlacesTransport,
    places_ok,
    places_status,
    synth_id,
    synthetic_compute_route,
    synthetic_place_details,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
PID1, PID2, PID3, PID4 = (synth_id(i) for i in (1, 2, 3, 4))
REVIEWED = PlacesPriceTable(
    version="test-reviewed-1", reviewed=True, details_sku="places_details_test",
    routes_sku="routes_test", details_usd_per_request=Decimal("0.01"),
    routes_usd_per_request=Decimal("0.005"))
TOKENS = {
    "tok-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-b": VerifiedIdentity("guest-b", "anonymous"),
    "tok-off": VerifiedIdentity("off-1", "google.com"),
}
HA = {"Authorization": "Bearer tok-a"}
HB = {"Authorization": "Bearer tok-b"}
LOC = {"latitude": 43.65321, "longitude": -79.38321}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Env:
    def __init__(self, *, prices: PlacesPriceTable = REVIEWED, script=(), limits=None,
                 walk: bool = False, service: bool = True, kill: KillSwitch | None = None) -> None:
        self.clock = Clock()
        self.sink = t.InMemorySink()
        self.transport = FakePlacesTransport(script)
        self.ledger = CostLedger(self.clock)
        self.kill = kill or KillSwitch()
        self.config = pinned_runtime_config()
        self.gateway = FakeGateway([], GatewayPolicy(tool_allowlist=("search_places",)))
        quotas = QuotaService()
        self.app = create_app(
            verifier=StaticTokenVerifier(TOKENS),
            resolver=PrincipalResolver(policies=quotas.policies, disabled_uids={"off-1"}),
            orchestrator=TurnOrchestrator(quotas, self.sink, gateway=self.gateway,
                                          runtime_config=self.config, cost_ledger=self.ledger,
                                          clock=self.clock),
            trace_sink=self.sink)
        self.service = build_places_service(
            lambda: PLACES_CANARY_KEY, ledger=self.ledger, price_table=prices, config=self.config,
            kill_switch=self.kill, transport=self.transport, now=self.clock, sink=self.sink,
            walk_enabled=walk, limits=limits)
        if service:
            self.app.state.places_service = self.service
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def consent(self, uid: str = "guest-a") -> None:
        self.app.state.consents.grant(uid, ConsentPurpose.LOCATION_FOREGROUND, LOCATION_NOTICE_VERSION)

    def details(self, ids=(PID1,), headers=HA):
        return self.client.post("/v1/places/details", json={"place_ids": list(ids)}, headers=headers)

    def route(self, body=None, headers=HA):
        body = {"place_id": PID1, "location": LOC} if body is None else body
        return self.client.post("/v1/routes/check", json=body, headers=headers)


def detail(index: int = 1):
    return places_ok(synthetic_place_details(index))


def route_ok(**kw):
    return places_ok(synthetic_compute_route(**kw))


# --- the feature is off without a reviewed price table -----------------------------------------
def test_off_without_reviewed_pricing_makes_no_network_call() -> None:
    env = Env(prices=UNREVIEWED_PRICE_TABLE, script=[detail()])
    assert not env.service.enabled
    first = env.details()
    assert first.status_code == 503 and first.json()["reason"] == "SERVER_NOT_CONFIGURED"
    env.consent()
    second = env.route()
    assert second.status_code == 503 and second.json()["reason"] == "SERVER_NOT_CONFIGURED"
    assert env.transport.requests == []
    assert env.ledger.entries() == ()


def test_unreviewed_table_is_the_default_and_builds_no_transport() -> None:
    service = build_places_service(lambda: "k", ledger=CostLedger())
    assert service.enabled is False and UNREVIEWED_PRICE_TABLE.reviewed is False


def test_no_service_on_the_app_is_a_typed_503() -> None:
    env = Env(service=False)
    for response in (env.details(), env.route({"place_id": PID1})):
        assert response.status_code == 503
        assert response.json()["reason"] == "SERVER_NOT_CONFIGURED"


def test_missing_key_provider_is_off() -> None:
    service = build_places_service(None, ledger=CostLedger(), price_table=REVIEWED)
    assert not service.enabled


# --- request shape: field mask exactness, key header, ids ----------------------------------------
def test_details_request_is_exact() -> None:
    env = Env(script=[detail()])
    assert env.details().status_code == 200
    (request,) = env.transport.requests
    assert request.method == "GET"
    assert request.url == f"https://places.googleapis.com/v1/places/{PID1}"
    assert request.headers == {"Accept": "application/json", MASK_HEADER: DETAILS_FIELD_MASK}
    assert DETAILS_FIELD_MASK == "id,displayName,formattedAddress,currentOpeningHours.openNow,rating"
    assert request.body is None
    assert env.transport.keys == [PLACES_CANARY_KEY]
    assert PLACES_CANARY_KEY not in request.url and PLACES_CANARY_KEY not in str(request.headers)
    for forbidden in ("photos", "reviews", "location", "googleMapsUri", "editorialSummary"):
        assert forbidden not in DETAILS_FIELD_MASK


def test_route_request_is_exact_and_has_no_polyline_or_traffic() -> None:
    env = Env(script=[route_ok()])
    env.consent()
    assert env.route().status_code == 200
    (request,) = env.transport.requests
    assert request.method == "POST"
    assert request.url == "https://routes.googleapis.com/directions/v2:computeRoutes"
    assert request.headers[MASK_HEADER] == ROUTES_FIELD_MASK == "routes.duration,routes.distanceMeters"
    assert request.body == {
        "origin": {"location": {"latLng": {"latitude": 43.653, "longitude": -79.383}}},
        "destination": {"placeId": PID1}, "travelMode": "DRIVE"}
    wire = json.dumps(request.body) + str(request.headers)
    for forbidden in ("polyline", "routingPreference", "TRAFFIC", "extraComputations"):
        assert forbidden not in wire


def test_request_models_refuse_other_masks_hosts_and_bodies() -> None:
    good = {"Accept": "application/json", MASK_HEADER: DETAILS_FIELD_MASK}
    url = f"https://places.googleapis.com/v1/places/{PID1}"
    PlacesRequest(method="GET", url=url, headers=good, timeout_seconds=1)
    with pytest.raises(ValueError):
        PlacesRequest(method="GET", url=url, headers={**good, MASK_HEADER: "*"}, timeout_seconds=1)
    with pytest.raises(ValueError):
        PlacesRequest(method="GET", url=url, headers={**good, MASK_HEADER: DETAILS_FIELD_MASK + ",photos"},
                      timeout_seconds=1)
    for bad_url in (f"http://places.googleapis.com/v1/places/{PID1}",
                    f"https://evil.example/v1/places/{PID1}",
                    f"https://places.googleapis.com/v1/places/{PID1}?key=abc",
                    f"https://places.googleapis.com:8443/v1/places/{PID1}",
                    f"https://user@places.googleapis.com/v1/places/{PID1}",
                    "https://places.googleapis.com/v1/places/../x"):
        with pytest.raises(ValueError):
            PlacesRequest(method="GET", url=bad_url, headers=good, timeout_seconds=1)
    post = {"Accept": "application/json", "Content-Type": "application/json",
            MASK_HEADER: ROUTES_FIELD_MASK}
    body = {"origin": {"location": {"latLng": {"latitude": 1.0, "longitude": 2.0}}},
            "destination": {"placeId": PID1}, "travelMode": "DRIVE"}
    route_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    PlacesRequest(method="POST", url=route_url, headers=post, body=body, timeout_seconds=1)
    for extra in ({"routingPreference": "TRAFFIC_AWARE"}, {"polylineQuality": "HIGH_QUALITY"}):
        with pytest.raises(ValueError):
            PlacesRequest(method="POST", url=route_url, headers=post, body={**body, **extra},
                          timeout_seconds=1)
    with pytest.raises(ValueError):
        PlacesRequest(method="POST", url=route_url, headers={**post, MASK_HEADER: "routes.polyline"},
                      body=body, timeout_seconds=1)


def test_at_most_three_ids_and_invalid_ids() -> None:
    env = Env(script=[detail(1), detail(2), detail(3)])
    assert env.details([PID1, PID2, PID3]).status_code == 200
    assert len(env.transport.requests) == 3
    four = env.details([PID1, PID2, PID3, PID4])
    assert four.status_code == 422
    assert len(env.transport.requests) == 3
    assert validate_place_ids([PID1, PID1, PID2]) == (PID1, PID2)  # duplicates are not billed twice
    for bad in ([], ["a/b"], ["a b"], ["../x"], [""], ["x" * 301], ["a?b"], [5], "abc"):
        with pytest.raises(PlacesInvalidRequest):
            validate_place_ids(bad)
    for bad in (["a/b"], ["x" * 301], ["a?key=1"], []):
        assert env.details(bad).status_code == 422
    assert len(env.transport.requests) == 3


# --- rendering ---------------------------------------------------------------------------------
def test_place_card_is_rendered_by_the_server() -> None:
    env = Env(script=[detail()])
    response = env.details()
    assert response.status_code == 200
    body = response.json()
    card = PlaceCard.model_validate(body["places"][0])
    assert card.place_id == PID1 and card.display_name.startswith("Synthetic Canary Teahouse")
    assert card.open_now is True and card.rating == 4.4 and card.display_only is True
    assert card.attributions == ("Google Maps",)
    assert card.fetched_at == NOW
    assert "location" not in json.dumps(body) and "43.98" not in json.dumps(body)


def test_malformed_or_hostile_text_is_refused_or_dropped() -> None:
    env = Env(script=[
        places_ok(synthetic_place_details(1, id="SOMETHING_ELSE")),          # wrong id
        places_ok(synthetic_place_details(1, displayName={"text": "bad‮name"})),  # bidi
        places_ok(synthetic_place_details(1, displayName={"text": "x" * 201})),         # too long
        places_ok(synthetic_place_details(1, displayName=None)),
        places_ok(synthetic_place_details(1, formattedAddress="a​b", rating=9)),   # dropped
    ])
    for _ in range(4):
        response = env.details()
        assert response.status_code == 502 and response.json()["reason"] == "OUTPUT_REJECTED"
    ok = env.details()
    assert ok.status_code == 200
    card = ok.json()["places"][0]
    assert card["formatted_address"] is None and card["rating"] is None


def test_walk_beta_warning_and_flag() -> None:
    env = Env(script=[route_ok()])
    env.consent()
    off = env.route({"place_id": PID1, "location": LOC, "mode": "WALK"})
    assert off.status_code == 422 and env.transport.requests == []
    on = Env(script=[route_ok()], walk=True)
    on.consent()
    response = on.route({"place_id": PID1, "location": LOC, "mode": "WALK"})
    assert response.status_code == 200
    route = RouteCheck.model_validate(response.json()["route"])
    assert route.mode == RouteMode.WALK and route.beta_warning is True
    assert on.transport.requests[0].body["travelMode"] == "WALK"
    assert "beta" in describe_route(route)


def test_drive_route_is_rounded_and_attributed() -> None:
    env = Env(script=[route_ok(distance=4321, duration="905s")])
    env.consent()
    response = env.route()
    route = RouteCheck.model_validate(response.json()["route"])
    assert route.mode == RouteMode.DRIVE and route.beta_warning is False
    assert route.duration_minutes == 16  # 905 s rounded UP to whole minutes
    assert route.distance_meters == 4300  # 100 m steps
    assert route.attributions == ("Google Maps",)


def test_wording_makes_no_precision_promise() -> None:
    env = Env(script=[route_ok(distance=4321, duration="905s")])
    env.consent()
    route = RouteCheck.model_validate(env.route().json()["route"])
    text = describe_route(route)
    assert text.startswith("About ") and "about 4.3 km" in text and "without live traffic" in text
    for promise in ("exactly", "guarantee", "will arrive", "4321", "905", "precise", "arrive by"):
        assert promise not in text.lower()
    assert route.distance_meters % 100 == 0


def test_no_route_and_malformed_route() -> None:
    env = Env(script=[places_ok({}), places_ok({"routes": [{"duration": "soon", "distanceMeters": 1}]}),
                      places_ok({"routes": [{"duration": "60s", "distanceMeters": -4}]})])
    env.consent()
    assert env.route().status_code == 404
    assert env.route().status_code == 502
    assert env.route().status_code == 502


# --- consent and origin --------------------------------------------------------------------------
def test_route_requires_consent_when_a_location_is_sent() -> None:
    env = Env(script=[route_ok()])
    refused = env.route()
    assert refused.status_code == 403
    assert refused.json()["reason"] == "LOCATION_CONSENT_REQUIRED"
    assert env.transport.requests == [] and env.ledger.entries() == ()
    env.consent()
    assert env.route().status_code == 200


def test_withdrawn_consent_is_refused() -> None:
    env = Env(script=[route_ok()])
    env.consent()
    env.app.state.consents.withdraw("guest-a", ConsentPurpose.LOCATION_FOREGROUND)
    assert env.route().status_code == 403
    assert env.transport.requests == []


def test_no_origin_is_a_fixed_prompt_never_a_guess() -> None:
    env = Env(script=[route_ok()])
    env.consent()
    response = env.route({"place_id": PID1})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION"
    assert response.json()["message"].startswith("A current location is needed")
    assert env.transport.requests == [] and env.ledger.entries() == ()


def test_origin_is_the_rounded_location_only() -> None:
    env = Env(script=[route_ok()])
    env.consent()
    env.route({"place_id": PID1, "location": {"latitude": 43.653219, "longitude": -79.383219}})
    origin = env.transport.requests[0].body["origin"]["location"]["latLng"]
    assert origin == {"latitude": 43.653, "longitude": -79.383}


def test_bad_origin_is_not_echoed() -> None:
    env = Env()
    env.consent()
    response = env.route({"place_id": PID1, "location": {"latitude": 912.5, "longitude": -79.1}})
    assert response.status_code == 422 and "912.5" not in response.text


# --- provider failures ----------------------------------------------------------------------------
@pytest.mark.parametrize("status,expected,reason", [
    (400, 502, "OUTPUT_REJECTED"), (401, 503, "PROVIDER_UNAVAILABLE"),
    (403, 503, "PROVIDER_UNAVAILABLE"), (404, 404, None), (429, 503, "PROVIDER_RATE_LIMITED"),
    (500, 503, "PROVIDER_UNAVAILABLE"), (503, 503, "PROVIDER_UNAVAILABLE"),
])
def test_provider_status_mapping(status, expected, reason) -> None:
    env = Env(script=[places_status(status, {"error": {"message": PLACES_CANARIES[0]}})])
    response = env.details()
    assert response.status_code == expected
    assert response.json().get("reason") == reason
    assert all(c not in response.text for c in PLACES_CANARIES)


def test_timeout_transport_failure_and_oversize() -> None:
    env = Env(script=[TransportTimeout(), TransportFailure(), RuntimeError("boom " + PLACES_CANARIES[1])])
    first, second, third = env.details(), env.details(), env.details()
    assert (first.status_code, first.json()["reason"]) == (504, "PROVIDER_TIMEOUT")
    assert (second.status_code, second.json()["reason"]) == (503, "PROVIDER_UNAVAILABLE")
    assert third.status_code == 503 and PLACES_CANARIES[1] not in third.text
    big = Env(script=[places_ok({"id": PID1, "displayName": {"text": "x"}, "pad": "y" * 70_000})])
    big_response = big.details()
    assert big_response.status_code == 502 and big_response.json()["reason"] == "OUTPUT_REJECTED"


def test_non_object_body_is_malformed() -> None:
    env = Env(script=[places_ok({}).model_copy(update={"body": ["not", "an", "object"]})])
    assert env.details().status_code == 502


def test_partial_not_found_returns_the_found_cards() -> None:
    env = Env(script=[places_status(404), detail(2)])
    response = env.details([PID1, PID2])
    assert response.status_code == 200
    assert [p["place_id"] for p in response.json()["places"]] == [PID2]
    all_missing = Env(script=[places_status(404)]).details()
    assert all_missing.status_code == 404


# --- the real transport's wire rules -----------------------------------------------------------------
def _httpx_service(handler):
    mock = httpx.MockTransport(handler)
    transport = HttpxPlacesTransport.for_tests(mock)
    ledger = CostLedger()
    return build_places_service(lambda: PLACES_CANARY_KEY, ledger=ledger, price_table=REVIEWED,
                                transport=transport), ledger


def test_httpx_transport_sends_key_only_in_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=synthetic_place_details(1))

    service, _ = _httpx_service(handler)
    cards = service.place_details("guest-a", [PID1])
    assert len(cards) == 1
    (request,) = seen
    assert request.headers["x-goog-api-key"] == PLACES_CANARY_KEY
    assert PLACES_CANARY_KEY not in str(request.url)
    assert request.headers["x-goog-fieldmask"] == DETAILS_FIELD_MASK
    assert request.url.host == "places.googleapis.com" and not request.url.query
    assert request.headers["accept-encoding"] == "identity"


def test_httpx_transport_does_not_follow_redirects_and_caps_size() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    service, _ = _httpx_service(redirect)
    with pytest.raises(PlacesError) as caught:
        service.place_details("guest-a", [PID1])
    assert caught.value.status == 503  # a 3xx is a provider failure, never followed

    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b" " * (MAX_RESPONSE_BYTES + 10) + b"}",
                              headers={"content-type": "application/json"})

    service, _ = _httpx_service(huge)
    with pytest.raises(PlacesError) as big:
        service.place_details("guest-a", [PID1])
    assert big.value.status == 502


def test_httpx_transport_refuses_a_compressed_reply() -> None:
    def gz(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-encoding": "br",
                                                           "content-type": "application/json"})

    service, _ = _httpx_service(gz)
    with pytest.raises(PlacesError):
        service.place_details("guest-a", [PID1])


def test_credential_is_redacted() -> None:
    assert PLACES_CANARY_KEY not in repr(Credential(PLACES_CANARY_KEY))


# --- cache: 5 minutes, per tenant, in memory, stale refetch -------------------------------------------
def test_cache_hit_within_five_minutes_and_stale_refetch() -> None:
    env = Env(script=[detail(), detail()])
    assert env.details().status_code == 200
    env.clock.advance(minutes=4, seconds=59)
    again = env.details()
    assert again.status_code == 200 and len(env.transport.requests) == 1  # served from memory
    assert again.json()["places"][0]["fetched_at"].startswith("2026-10-26T12:00:00")
    env.clock.advance(seconds=2)  # past 5 minutes: stale, never served
    env.details()
    assert len(env.transport.requests) == 2
    events = [e for e in env.sink.events if isinstance(e, t.PlaceDetailsEvent)]
    assert [e.cache_hits for e in events] == [0, 1, 0]


def test_cache_is_per_tenant() -> None:
    env = Env(script=[detail(), detail()])
    env.details(headers=HA)
    env.details(headers=HB)
    assert len(env.transport.requests) == 2  # B never reads A's cached card
    entries = {e.principal_id for e in env.ledger.entries()}
    assert entries == {"guest-a", "guest-b"}


# --- cost ledger, ceilings, kill switch, budget ---------------------------------------------------------
def test_every_call_is_recorded_user_attributed_with_a_price() -> None:
    env = Env(script=[detail(1), detail(2), route_ok()])
    env.consent()
    env.details([PID1, PID2])
    env.route()
    entries = env.ledger.entries()
    assert [e.label for e in entries] == ["place_details", "place_details", "route_check"]
    assert all(e.principal_id == "guest-a" and e.kind == "tool" and e.tool_calls == 1
               and e.attribution == Attribution.USER for e in entries)
    assert [e.estimated_cost_usd for e in entries] == [Decimal("0.01"), Decimal("0.01"), Decimal("0.005")]
    assert env.ledger.usage_for("guest-a").input_tokens == 0  # tools do not touch the token ceiling


def test_provider_failure_is_provider_attributed_and_still_recorded() -> None:
    env = Env(script=[places_status(500), places_status(404)])
    env.details()
    env.details()
    first, second = env.ledger.entries()
    assert first.attribution == Attribution.PROVIDER and second.attribution == Attribution.USER


def test_daily_call_ceiling_is_per_user() -> None:
    env = Env(script=[detail(), detail(2)], limits=PlacesLimits(daily_calls_per_user=1))
    assert env.details([PID1], headers=HA).status_code == 200
    denied = env.details([PID2], headers=HA)
    assert denied.status_code == 429 and denied.json()["reason"] == "RATE_LIMITED"
    assert int(denied.headers["Retry-After"]) >= 1
    assert env.details([PID2], headers=HB).status_code == 200  # another tenant is unaffected
    assert len(env.transport.requests) == 2


def test_daily_cost_ceiling() -> None:
    env = Env(script=[detail(), detail(2)], limits=PlacesLimits(daily_usd_per_user=Decimal("0.015")))
    assert env.details([PID1]).status_code == 200
    denied = env.details([PID2])
    assert denied.status_code == 429 and len(env.transport.requests) == 1
    env.clock.advance(hours=25)
    assert env.details([PID2]).status_code == 200


def test_kill_switch_stops_everything_before_the_network() -> None:
    env = Env(script=[detail(), route_ok()])
    env.consent()
    env.kill.trip()
    for response in (env.details(), env.route()):
        assert response.status_code == 503 and response.json()["reason"] == "DISCOVERY_UNAVAILABLE"
    assert env.transport.requests == [] and env.ledger.entries() == ()


def test_provider_401_trips_the_kill_switch_but_one_403_does_not() -> None:
    env = Env(script=[places_status(403), detail()])
    assert env.details().status_code == 503
    assert not env.kill.tripped
    assert env.details().status_code == 200  # a success resets the streak
    env2 = Env(script=[places_status(401)])
    env2.details()
    assert env2.kill.tripped


def test_per_run_tool_budget() -> None:
    env = Env(script=[detail(i) for i in (1, 2, 3)])
    config = env.config.model_copy(update={"token_ceilings": env.config.token_ceilings.model_copy(
        update={"run_tool_calls": 2})})
    service = build_places_service(
        lambda: "k", ledger=env.ledger, price_table=REVIEWED, config=config,
        transport=env.transport, now=env.clock)
    with pytest.raises(PlacesError) as caught:
        service.place_details("guest-a", [PID1, PID2, PID3])
    assert caught.value.status == 429 and caught.value.reason.value == "OVER_BUDGET"
    assert len(env.transport.requests) == 2


# --- identity, auth, rate limiting ---------------------------------------------------------------------
def test_requires_auth_and_refuses_disabled_principals() -> None:
    env = Env(script=[detail()])
    anon = env.client.post("/v1/places/details", json={"place_ids": [PID1]})
    assert anon.status_code == 401
    off = env.details(headers={"Authorization": "Bearer tok-off"})
    assert off.status_code == 403
    assert env.transport.requests == []


def test_tenant_comes_from_the_principal_only() -> None:
    env = Env(script=[detail()])
    response = env.client.post("/v1/places/details", headers=HA,
                               json={"place_ids": [PID1], "uid": "guest-b", "tenant": "x"})
    assert response.status_code == 422  # extra fields are refused, never an identity
    ok = env.details()
    assert ok.status_code == 200 and {e.principal_id for e in env.ledger.entries()} == {"guest-a"}


def test_the_shared_rate_limiter_applies() -> None:
    env = Env(script=[detail()] * 3)
    statuses = [env.details().status_code for _ in range(125)]
    assert statuses[-1] == 429


# --- canary: nothing provider-side leaks anywhere ----------------------------------------------------------
def test_canary_no_provider_content_in_logs_traces_telemetry_stores_or_model() -> None:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler = Capture(level=logging.DEBUG)
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        env = Env(script=[detail(1), route_ok(), places_status(500), places_status(404)])
        env.consent()
        assert env.details().status_code == 200
        assert env.route().status_code == 200
        env.details([PID2])
        env.details([PID3])
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)

    def blob(item: object) -> str:
        return repr(item)

    haystacks = {
        "logs": "\n".join(r.getMessage() + repr(r.__dict__) for r in records),
        "telemetry": "\n".join(e.model_dump_json() for e in env.sink.events),
        "ledger": blob(env.ledger.entries()) + blob(env.ledger._by_principal),  # noqa: SLF001
        "consents": blob(env.app.state.consents.ledger("guest-a")),
        "orchestrator": blob(vars(env.app.state.orchestrator).keys()),
        "model": blob(env.gateway.requests),
    }
    coordinates = ("43.653", "-79.383", "43.65321", "-79.38321")
    for where, text in haystacks.items():
        for canary in (*PLACES_CANARIES, *coordinates):
            assert canary not in text, f"{canary!r} leaked into {where}"
    assert env.gateway.requests == []  # the model was never called
    assert {type(e).__name__ for e in env.sink.events} >= {"PlaceDetailsEvent", "RouteCheckEvent"}
    for event in env.sink.events:
        if isinstance(event, (t.PlaceDetailsEvent, t.RouteCheckEvent)):
            assert set(event.model_dump()) <= {
                "schema_version", "trace_id", "runtime_config_version", "event", "place_count",
                "cache_hits", "outcome", "latency_ms", "billable_units", "mode"}


def test_service_holds_no_coordinates_or_geometry_after_a_call() -> None:
    env = Env(script=[detail(), route_ok()])
    env.consent()
    env.details()
    env.route()
    state = repr(vars(env.service)) + repr(vars(env.service._cache)) + repr(  # noqa: SLF001
        vars(env.service._routes)) + repr(vars(env.service._details))  # noqa: SLF001
    for needle in (str(43.987654), "-79.123456", PLACES_CANARY_KEY.lower(), "SYNTHPOLYLINE", "43.653"):
        assert needle not in state
    parsed = list(env.service._cache._items.values())  # noqa: SLF001
    assert parsed and not hasattr(parsed[0], "location") and "latitude" not in repr(parsed[0].__dict__)


def test_repr_of_origin_and_results_is_redacted() -> None:
    assert "43.6" not in repr(RoundedLocation(43.653, -79.383))
