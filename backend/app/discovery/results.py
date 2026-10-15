"""Small typed results for the non-search tools, on the documented output shapes.

Each parser takes only its own tool's client-issued ``ToolResult``, tolerates only documented
keys (anything else rejects the payload), keeps the attribution ``{title, url}`` exactly, and
drops everything it does not need. Per-item ``failed_requests`` messages are untrusted text and
are dropped; only an index and a code survive. Walking routes carry a required warning flag
(Google: WALK is beta and the UI must warn). ``save_to_maps_url`` is an opaque string checked
against the Maps link allowlist, to be displayed as-is and never constructed.

TODO(verify-live): output casing (camelCase in the REST schema, snake_case in prose; both
spellings of the documented keys are accepted but not mixed).
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from ._mint import IssuedToken
from .contracts import GroundFrozen
from .errors import MalformedToolResult
from .ids import ToolId
from .text import has_lone_surrogate, short_field_problem
from .uris import check_attribution_url, check_maps_link

_PLACE_RESOURCE = re.compile(r"^places/([A-Za-z0-9_-]{1,300})$")
_DURATION = re.compile(r"^(\d{1,9}(?:\.\d{1,9})?)s$")
_ENUM = re.compile(r"^[A-Z_]{1,40}$")
_WEATHER_KEYS = frozenset({
    "weatherCondition", "precipitation", "wind", "temperature", "feelsLikeTemperature", "heatIndex",
    "maxTemperature", "minTemperature", "feelsLikeMaxTemperature", "feelsLikeMinTemperature",
    "maxHeatIndex", "airPressure", "sunEvents", "moonEvents", "relativeHumidity", "uvIndex",
    "thunderstormProbability", "cloudCover", "returnedLocation", "attribution",
})


class ItemFailureCode(IntEnum):
    """google.rpc.Code values the resolution API documents."""

    INVALID_ARGUMENT = 3
    NOT_FOUND = 5
    INTERNAL = 13
    OTHER = 0


class Attributed(GroundFrozen):
    attribution_title: Annotated[str, StringConstraints(min_length=1, max_length=200)] = Field(repr=False)
    attribution_url: str = Field(repr=False)
    tool_call_id: str

    @model_validator(mode="after")
    def _safe(self) -> Attributed:
        if short_field_problem(self.attribution_title):
            raise ValueError("hidden or control characters in the attribution title")
        check_attribution_url(self.attribution_url)
        return self


class WeatherResult(Attributed):
    condition_type: str | None = Field(default=None, repr=False)
    temperature_unit: Literal["CELSIUS", "FAHRENHEIT"] | None = None
    temperature_degrees: float | None = Field(default=None, repr=False)
    precipitation_percent: int | None = Field(default=None, ge=0, le=100, repr=False)


class RouteResult(Attributed):
    distance_meters: int = Field(ge=0, repr=False)
    duration_seconds: float = Field(ge=0, repr=False)
    travel_mode: Literal["DRIVE", "WALK"]
    walking_warning: bool  # REQUIRED for WALK: the UI must show Google's beta warning

    @model_validator(mode="after")
    def _walking_needs_the_warning(self) -> RouteResult:
        if self.walking_warning != (self.travel_mode == "WALK"):
            raise ValueError("walking routes require the walking warning, others must not set it")
        return self


class ResolvedPlace(GroundFrozen):
    index: int = Field(ge=0, le=19)
    place_id: str | None = None  # None: that item failed (empty result)
    confidence: Literal["HIGH", "MEDIUM", "CONFIDENCE_UNSPECIFIED"] | None = None


class ItemFailure(GroundFrozen):
    index: int = Field(ge=0, le=19)
    code: ItemFailureCode

    @property
    def retryable(self) -> bool:
        return self.code == ItemFailureCode.INTERNAL  # docs: retry only INTERNAL items


class ResolveResult(GroundFrozen):
    tool: ToolId
    tool_call_id: str
    items: tuple[ResolvedPlace, ...] = Field(max_length=20)
    failures: tuple[ItemFailure, ...] = Field(max_length=20)
    save_to_maps_url: str | None = Field(default=None, repr=False)  # display as-is

    @property
    def all_failed(self) -> bool:
        return bool(self.items) and all(i.place_id is None for i in self.items)


def _guard(result: Any, tool: ToolId) -> dict[str, Any]:
    issued = result.issued
    if not isinstance(issued, IssuedToken) or issued.call_id != result.tool_call_id:
        raise ValueError("result was not issued by the discovery client")
    if result.tool != tool or not isinstance(result.payload, dict):
        raise ValueError("wrong tool or payload")
    payload = result.payload
    if any(has_lone_surrogate(s) for s in _all_strings(payload)):
        raise ValueError("lone surrogate")
    return payload


def _all_strings(value: Any) -> list[str]:
    out: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _attribution(raw: Any) -> tuple[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"title", "url"}:
        raise ValueError("attribution shape")
    title, url = raw["title"], raw["url"]
    if not isinstance(title, str) or not isinstance(url, str):
        raise ValueError("attribution types")
    return title, url


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
        raise ValueError("not a finite number")
    return float(value)


def _weather(result: Any) -> WeatherResult:
    payload = _guard(result, ToolId.LOOKUP_WEATHER)
    if not set(payload) <= _WEATHER_KEYS or "attribution" not in payload:
        raise ValueError("weather keys")
    title, url = _attribution(payload["attribution"])
    condition = payload.get("weatherCondition")
    kind = None
    if condition is not None:
        if not isinstance(condition, dict) or not set(condition) <= {"iconBaseUri", "description", "type"}:
            raise ValueError("condition shape")
        kind = condition.get("type")
        if kind is not None and not (isinstance(kind, str) and _ENUM.fullmatch(kind)):
            raise ValueError("condition type")
    unit = degrees = None
    temperature = payload.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, dict) or set(temperature) != {"unit", "degrees"}:
            raise ValueError("temperature shape")
        unit, degrees = temperature["unit"], _number(temperature["degrees"])
    percent = None
    precipitation = payload.get("precipitation")
    if precipitation is not None:
        probability = precipitation.get("probability") if isinstance(precipitation, dict) else None
        if not isinstance(probability, dict) or not set(probability) <= {"type", "percent"}:
            raise ValueError("precipitation shape")
        raw = probability.get("percent")
        if raw is not None:
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError("percent")
            percent = raw
    return WeatherResult(
        attribution_title=title, attribution_url=url, tool_call_id=result.tool_call_id,
        condition_type=kind, temperature_unit=unit, temperature_degrees=degrees,
        precipitation_percent=percent,
    )


def _route(result: Any) -> RouteResult:
    payload = _guard(result, ToolId.COMPUTE_ROUTES)
    routes = payload.get("routes")
    if set(payload) != {"routes"} or not isinstance(routes, list) or len(routes) != 1:
        raise ValueError("routes shape")  # documented: currently exactly one route
    route = routes[0]
    if not isinstance(route, dict) or set(route) != {"distanceMeters", "duration", "attribution"}:
        raise ValueError("route shape")
    distance, duration = route["distanceMeters"], route["duration"]
    match = _DURATION.fullmatch(duration) if isinstance(duration, str) else None
    if isinstance(distance, bool) or not isinstance(distance, int) or match is None:
        raise ValueError("route values")
    title, url = _attribution(route["attribution"])
    mode = result.travel_mode if result.travel_mode in ("DRIVE", "WALK") else "DRIVE"
    return RouteResult(
        attribution_title=title, attribution_url=url, tool_call_id=result.tool_call_id,
        distance_meters=distance, duration_seconds=float(match.group(1)), travel_mode=mode,
        walking_warning=mode == "WALK",
    )


def _spelled(payload: dict[str, Any], snake: str, camel: str) -> Any:
    if snake in payload and camel in payload:
        raise ValueError("mixed casing")
    return payload.get(snake, payload.get(camel))


def _resolve(result: Any, tool: ToolId, items_key: str) -> ResolveResult:
    payload = _guard(result, tool)
    allowed = {items_key, "failed_requests", "failedRequests", "save_to_maps_url", "saveToMapsUrl"}
    if not set(payload) <= allowed:
        raise ValueError("resolve keys")
    raw_items = payload.get(items_key)
    if not isinstance(raw_items, list) or len(raw_items) > 20:
        raise ValueError("items shape")
    items: list[ResolvedPlace] = []
    for index, raw in enumerate(raw_items):
        entity = raw if tool == ToolId.RESOLVE_MAPS_URLS else (raw.get("entity") if isinstance(raw, dict) else None)
        place_id, confidence = None, None
        if not isinstance(raw, dict) or not set(raw) <= ({"place"} if tool == ToolId.RESOLVE_MAPS_URLS else {"entity", "confidence"}):
            raise ValueError("item shape")
        if raw:
            if tool == ToolId.RESOLVE_NAMES:
                confidence = raw.get("confidence")
                if confidence is not None and confidence not in ("HIGH", "MEDIUM", "CONFIDENCE_UNSPECIFIED"):
                    raise ValueError("confidence")
                if entity is not None and (not isinstance(entity, dict) or set(entity) != {"place"}):
                    raise ValueError("entity shape")
                entity = entity or {}
            place = entity.get("place") if isinstance(entity, dict) else None
            if place is not None:
                match = _PLACE_RESOURCE.fullmatch(place) if isinstance(place, str) else None
                if match is None:
                    raise ValueError("place resource")
                place_id = match.group(1)
        items.append(ResolvedPlace(index=index, place_id=place_id, confidence=confidence))
    failures: list[ItemFailure] = []
    failed = _spelled(payload, "failed_requests", "failedRequests") or {}
    if not isinstance(failed, dict) or len(failed) > 20:
        raise ValueError("failed_requests shape")
    for key, status in failed.items():
        if not (isinstance(key, str) and key.isdigit() and int(key) < max(len(items), 1)) or not isinstance(status, dict):
            raise ValueError("failed_requests entry")
        if not set(status) <= {"code", "message", "details"}:
            raise ValueError("status keys")
        code = status.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise ValueError("status code")
        failures.append(ItemFailure(index=int(key), code=ItemFailureCode(code) if code in (3, 5, 13) else ItemFailureCode.OTHER))
    save = _spelled(payload, "save_to_maps_url", "saveToMapsUrl")
    if save is not None:
        if not isinstance(save, str):
            raise ValueError("save url")
        check_maps_link(save)
    return ResolveResult(
        tool=tool, tool_call_id=result.tool_call_id, items=tuple(items),
        failures=tuple(sorted(failures, key=lambda f: f.index)), save_to_maps_url=save,
    )


def _typed(builder: Any, *args: Any) -> Any:
    built: Any = None
    try:
        built = builder(*args)
    except Exception:  # noqa: BLE001 - nothing from the failure is kept
        built = None
    if built is None:
        raise MalformedToolResult()
    return built


def parse_weather(result: Any) -> WeatherResult:
    return _typed(_weather, result)  # type: ignore[no-any-return]


def parse_route(result: Any) -> RouteResult:
    return _typed(_route, result)  # type: ignore[no-any-return]


def parse_resolve_names(result: Any) -> ResolveResult:
    return _typed(_resolve, result, ToolId.RESOLVE_NAMES, "results")  # type: ignore[no-any-return]


def parse_resolve_urls(result: Any) -> ResolveResult:
    return _typed(_resolve, result, ToolId.RESOLVE_MAPS_URLS, "entities")  # type: ignore[no-any-return]
