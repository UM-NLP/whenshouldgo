"""Injectable transport for the two direct Google APIs (Places Details, Routes computeRoutes).

Fail-closed like the discovery transport: exact hosts, exact path shapes, exact header allowlists
(the field mask must equal the pinned minimal mask for that endpoint), a body shape check, size and
time bounds, no redirects, no proxy environment, identity encoding only. The API key reaches the
wire only through an ``httpx.Auth`` object (header ``X-Goog-Api-Key``), never a URL or a headers
dict. Raised errors carry no payload, URL or key.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import ConfigDict, Field, model_validator

from ..domain import Frozen
from ..gateway.errors import TransportFailure, TransportTimeout
from ..gateway.openrouter import Credential, guarded, strict_loads
from .errors import PlacesConfigError

PLACES_HOST = "places.googleapis.com"
ROUTES_HOST = "routes.googleapis.com"
API_KEY_HEADER = "X-Goog-Api-Key"
MASK_HEADER = "X-Goog-FieldMask"

# The MINIMUM that renders a PlaceCard (name, address, open now, rating). No location, photos,
# reviews, links, phone numbers or hours text are requested. (Place Details masks carry no
# "places." prefix.)
DETAILS_FIELD_MASK = "id,displayName,formattedAddress,currentOpeningHours.openNow,rating"
# The ONLY route fields: no polyline, no legs, no steps, no traffic data.
ROUTES_FIELD_MASK = "routes.duration,routes.distanceMeters"

MAX_RESPONSE_BYTES = 65_536
MAX_TIMEOUT_SECONDS = 10.0
_DETAILS_PATH = re.compile(r"^/v1/places/[A-Za-z0-9_-]{1,300}$")
_ROUTES_PATH = "/directions/v2:computeRoutes"
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")
_GET_HEADERS = {"Accept": "application/json", MASK_HEADER: DETAILS_FIELD_MASK}
_POST_HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
                 MASK_HEADER: ROUTES_FIELD_MASK}
_MODES = ("DRIVE", "WALK")


class TransportOversize(Exception):
    """Raised by a transport when the reply exceeded the size cap."""


def _number(value: Any, low: float, high: float) -> bool:
    return isinstance(value, float | int) and not isinstance(value, bool) and low <= value <= high


def _valid_route_body(body: Any) -> bool:
    """Exactly origin (a lat/lng), destination (a place id) and travelMode: nothing else (no
    routing preference, modifiers, polyline options or extra computations)."""
    try:
        if set(body) != {"origin", "destination", "travelMode"} or body["travelMode"] not in _MODES:
            return False
        origin = body["origin"]
        if set(origin) != {"location"} or set(origin["location"]) != {"latLng"}:
            return False
        lat_lng = origin["location"]["latLng"]
        if set(lat_lng) != {"latitude", "longitude"}:
            return False
        destination = body["destination"]
        return (_number(lat_lng["latitude"], -90, 90) and _number(lat_lng["longitude"], -180, 180)
                and set(destination) == {"placeId"} and isinstance(destination["placeId"], str)
                and bool(_PLACE_ID.fullmatch(destination["placeId"])))
    except (TypeError, KeyError):
        return False


class PlacesRequest(Frozen):
    """What a transport (and any trace) sees: no credential, pinned endpoint, pinned headers."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any] | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(gt=0, le=MAX_TIMEOUT_SECONDS)

    @model_validator(mode="after")
    def _checked(self) -> PlacesRequest:
        try:
            parts = httpx.URL(self.url)
        except Exception:  # noqa: BLE001
            raise ValueError("request is not an allowlisted Places or Routes call") from None
        clean = (parts.scheme == "https" and not parts.query and not parts.fragment
                 and not parts.userinfo and parts.port is None and self.url.isascii()
                 and self.url.isprintable() and "@" not in self.url)
        if self.method == "GET" and clean and parts.host == PLACES_HOST and self.body is None:
            ok = bool(_DETAILS_PATH.fullmatch(parts.path)) and self.headers == _GET_HEADERS
        elif self.method == "POST" and clean and parts.host == ROUTES_HOST and self.body is not None:
            ok = (parts.path == _ROUTES_PATH and self.headers == _POST_HEADERS
                  and _valid_route_body(self.body))
        else:
            ok = False
        if not ok:
            raise ValueError("request is not an allowlisted Places or Routes call")
        return self


class PlacesResponse(Frozen):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    status_code: int
    body: Any = Field(default=None, repr=False)
    size_bytes: int = Field(default=0, ge=0)


@runtime_checkable
class PlacesTransport(Protocol):
    def send(self, request: PlacesRequest, credential: Credential) -> PlacesResponse: ...


class _ApiKeyAuth(httpx.Auth):
    def __init__(self, credential: Credential) -> None:
        self._credential = credential

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers[API_KEY_HEADER] = self._credential.reveal()
        yield request

    def __repr__(self) -> str:
        return "_ApiKeyAuth(<redacted>)"


class HttpxPlacesTransport:
    """Real transport: its own locked-down client (TLS verified, no redirects, no proxy env)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, _transport: Any = None) -> None:
        kwargs: dict[str, Any] = {
            "trust_env": False, "follow_redirects": False, "verify": True,
            "timeout": httpx.Timeout(10.0, connect=5.0)}
        if _transport is not None:  # test seam only
            kwargs["transport"] = _transport
        self._client = httpx.Client(**kwargs)
        self._clock = clock

    @classmethod
    def for_tests(cls, transport: httpx.BaseTransport,
                  *, clock: Callable[[], float] = time.monotonic) -> HttpxPlacesTransport:
        return cls(clock=clock, _transport=transport)

    def send(self, request: PlacesRequest, credential: Credential) -> PlacesResponse:
        # Re-validate: a model_copy cannot smuggle a different endpoint or mask past this point.
        PlacesRequest.model_validate(request.model_dump())
        timeout = float(request.timeout_seconds)
        deadline = self._clock() + timeout
        outcome = "ok"
        status = 0
        media = ""
        chunks: list[bytes] = []
        total = 0
        try:
            with self._client.stream(
                request.method, request.url, json=request.body,
                headers={**request.headers, "Accept-Encoding": "identity"},
                auth=_ApiKeyAuth(credential),
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            ) as response:
                status = response.status_code
                media = response.headers.get("content-type", "").split(";")[0].strip().lower()
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    outcome = "failure"
                else:
                    source = [response.content] if response.is_stream_consumed else response.iter_raw()
                    for chunk in source:
                        if self._clock() > deadline:
                            outcome = "timeout"
                            break
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            outcome = "oversize"
                            break
                        chunks.append(chunk)
        except httpx.TimeoutException:
            outcome = "timeout"
        except Exception:  # noqa: BLE001 - TLS, encoding, invalid url ...; nothing is kept
            outcome = "failure"
        if outcome == "timeout":
            raise TransportTimeout
        if outcome == "oversize":
            raise TransportOversize
        if outcome != "ok":
            raise TransportFailure
        body: Any = None
        if media == "application/json":
            parsed, ok = guarded(strict_loads, b"".join(chunks))
            body = parsed if ok else None
        return PlacesResponse(status_code=status, body=body, size_bytes=total)


def credential_from(provider: Callable[[], str]) -> Credential:
    value, ok = guarded(provider)
    if not ok or not isinstance(value, str) or not value or not re.fullmatch(r"[\x21-\x7e]+", value):
        raise PlacesConfigError("places API key is missing or malformed")
    return Credential(value)


def exchange(transport: PlacesTransport, request: PlacesRequest, credential: Credential) -> dict[str, Any]:
    """Send one request and return the parsed JSON object, or raise a typed ``PlacesError``.

    Every failure path first builds a typed error and raises it OUTSIDE the ``except`` block, so no
    exception context can hold the request, a payload or the key."""
    from .errors import (
        MalformedPlacesResult,
        OversizePlacesResult,
        PlacesError,
        PlacesTimeout,
        PlacesUnavailable,
        error_for_status,
    )

    failure: PlacesError | None = None
    response: PlacesResponse | None = None
    try:
        response = transport.send(request, credential)
    except TransportTimeout:
        failure = PlacesTimeout(calls=1)
    except TransportOversize:
        failure = OversizePlacesResult(calls=1)
    except TransportFailure:
        failure = PlacesUnavailable(calls=1)
    except Exception:  # noqa: BLE001 - a transport bug is a provider-side failure, content dropped
        failure = PlacesUnavailable(calls=1)
    if failure is None and response is not None:
        failure = error_for_status(response.status_code)
        if failure is None and response.size_bytes > MAX_RESPONSE_BYTES:
            failure = OversizePlacesResult(calls=1)
        if failure is None and not isinstance(response.body, dict):
            failure = MalformedPlacesResult(calls=1)
    if failure is not None or response is None:
        raise failure or PlacesUnavailable(calls=1)
    return response.body
