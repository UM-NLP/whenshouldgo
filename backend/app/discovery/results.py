"""Small typed results for the non-search tools, on the output shapes seen in a live capture
(verified live 2026-10-15: camelCase keys, ``result.structuredContent``).

Each parser takes only its own tool's client-issued ``ToolResult`` plus the run and principal it
belongs to, verifies the payload against the run ledger, tolerates only documented keys (anything
else rejects the payload), keeps the attribution ``{title, url}`` exactly, and drops everything it
does not need (coordinates, ``returnedLocation``, the icon url, pressure, cloud cover, gusts,
heat index, rainfall quantities). Per-item ``failedRequests`` messages are untrusted text and are
dropped; only an index and a code survive. Walking routes carry a required warning flag and the
warning text. ``saveToMapsUrl`` is checked against its pinned shape and displayed as-is.

The weather icon (``iconBaseUri``) is validated against ``maps.gstatic.com/weather/v1/<name>`` and
then DROPPED: no screen needs it yet, and whether it needs its own attribution or caching rule is
open (TODO(verify-live)). Weather text, values and its attribution url are MAPS_GROUNDED content.

TODO(verify-live): both casings of a key are accepted for the resolve tools (``failedRequests`` is
what the server sends); a payload may not mix them.
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, StringConstraints, model_validator

from ._mint import IssuedToken
from .client import ToolRun
from .contracts import GroundFrozen, payload_digest
from .errors import MalformedToolResult
from .ids import ToolId
from .text import has_lone_surrogate, short_field_problem
from .uris import LinkField, check_link

# Google's text for every walking route (beta). A WALK result must be shown with it.
WALKING_WARNING_TEXT = (
    "Walking routes are in beta and might sometimes be missing clear sidewalks or pedestrian paths."
)

_PLACE_RESOURCE = re.compile(r"^places/([A-Za-z0-9_-]{1,300})$")
_DURATION = re.compile(r"^(\d{1,9}(?:\.\d{1,9})?)s$")
_ENUM = re.compile(r"^[A-Z_]{1,40}$")
_WEATHER_KEYS = frozenset({
    "weatherCondition", "precipitation", "wind", "temperature", "feelsLikeTemperature", "heatIndex",
    "maxTemperature", "minTemperature", "feelsLikeMaxTemperature", "feelsLikeMinTemperature",
    "maxHeatIndex", "airPressure", "sunEvents", "moonEvents", "relativeHumidity", "uvIndex",
    "thunderstormProbability", "cloudCover", "returnedLocation", "attribution",
})
_TEMP_UNITS = {"CELSIUS": (-100.0, 100.0), "FAHRENHEIT": (-150.0, 212.0)}
_WIND_UNITS = ("KILOMETERS_PER_HOUR", "MILES_PER_HOUR")


class ItemFailureCode(IntEnum):
    """google.rpc.Code values the resolution API documents."""

    INVALID_ARGUMENT = 3
    NOT_FOUND = 5
    INTERNAL = 13
    OTHER = 0


class Attributed(GroundFrozen):
    """Shared attribution. Subclasses pin the url shape of THEIR tool (a route's attribution is
    not a weather attribution)."""

    attribution_title: Annotated[str, StringConstraints(min_length=1, max_length=200)] = Field(repr=False)
    attribution_url: str = Field(repr=False)
    tool_call_id: str

    url_field: ClassVar[LinkField]

    @model_validator(mode="after")
    def _safe(self) -> Attributed:
        if short_field_problem(self.attribution_title):
            raise ValueError("hidden or control characters in the attribution title")
        check_link(self.url_field, self.attribution_url)
        return self


class WeatherResult(Attributed):
    url_field: ClassVar[LinkField] = LinkField.WEATHER_ATTRIBUTION_URL
    temperature_unit: Literal["CELSIUS", "FAHRENHEIT"]
    temperature_degrees: float = Field(repr=False)
    feels_like_unit: Literal["CELSIUS", "FAHRENHEIT"] | None = None
    feels_like_degrees: float | None = Field(default=None, repr=False)
    condition_type: str | None = Field(default=None, repr=False)
    condition_text: Annotated[str, StringConstraints(max_length=100)] | None = Field(default=None, repr=False)
    precipitation_percent: int | None = Field(default=None, ge=0, le=100, repr=False)
    precipitation_type: str | None = Field(default=None, repr=False)
    wind_speed_unit: Literal["KILOMETERS_PER_HOUR", "MILES_PER_HOUR"] | None = None
    wind_speed_value: float | None = Field(default=None, ge=0, le=500, repr=False)
    wind_cardinal: str | None = Field(default=None, repr=False)
    wind_degrees: int | None = Field(default=None, ge=0, le=360, repr=False)
    relative_humidity: int | None = Field(default=None, ge=0, le=100, repr=False)
    uv_index: int | None = Field(default=None, ge=0, le=30, repr=False)

    @model_validator(mode="after")
    def _ranges(self) -> WeatherResult:
        low, high = _TEMP_UNITS[self.temperature_unit]
        if not low <= self.temperature_degrees <= high:
            raise ValueError("temperature out of range")
        if self.feels_like_degrees is not None:
            if self.feels_like_unit is None:
                raise ValueError("feels-like needs a unit")
            low, high = _TEMP_UNITS[self.feels_like_unit]
            if not low <= self.feels_like_degrees <= high:
                raise ValueError("feels-like out of range")
        if self.condition_text is not None and short_field_problem(self.condition_text):
            raise ValueError("hidden or control characters in the condition text")
        return self


class RouteResult(Attributed):
    url_field: ClassVar[LinkField] = LinkField.ROUTE_ATTRIBUTION_URL
    distance_meters: int = Field(ge=0, repr=False)
    duration_seconds: float = Field(ge=0, repr=False)
    travel_mode: Literal["DRIVE", "WALK"]
    walking_warning: bool  # REQUIRED for WALK: the UI must show Google's beta warning

    @model_validator(mode="after")
    def _walking_needs_the_warning(self) -> RouteResult:
        if self.walking_warning != (self.travel_mode == "WALK"):
            raise ValueError("walking routes require the walking warning, others must not set it")
        return self

    @property
    def walking_warning_text(self) -> str | None:
        """The exact text to render with this route (None for driving)."""
        return WALKING_WARNING_TEXT if self.walking_warning else None


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
    save_to_maps_url: str | None = Field(default=None, repr=False)  # display as-is, never constructed

    @model_validator(mode="after")
    def _pinned(self) -> ResolveResult:
        if self.save_to_maps_url is not None:
            check_link(LinkField.SAVE_TO_MAPS_URL, self.save_to_maps_url)
        return self

    @property
    def all_failed(self) -> bool:
        return bool(self.items) and all(i.place_id is None for i in self.items)


def _guard(result: Any, run: ToolRun, principal_id: str, tool: ToolId) -> dict[str, Any]:
    """The result must be client-issued, for this tool, this run and this principal, and its
    stored payload must hash to what the client recorded in the ledger for that call."""
    issued = result.issued
    if not isinstance(issued, IssuedToken) or issued.call_id != result.tool_call_id:
        raise ValueError("result was not issued by the discovery client")
    if not isinstance(run, ToolRun) or run.principal_id != principal_id or result.run_id != run.run_id:
        raise ValueError("result belongs to another run or principal")
    record = run.record_for(result.tool_call_id)
    if record is None or record.tool != tool or result.tool != tool:
        raise ValueError("wrong tool or unknown call")
    if record.payload_hash is None or record.payload_hash != payload_digest(result.payload_json):
        raise ValueError("payload does not match the ledger")
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


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not an integer")
    return value


def _reading(raw: Any) -> tuple[str, float]:
    """A ``{unit, degrees}`` temperature."""
    if not isinstance(raw, dict) or set(raw) != {"unit", "degrees"} or raw["unit"] not in _TEMP_UNITS:
        raise ValueError("temperature shape")
    return raw["unit"], _number(raw["degrees"])


def _enum_text(raw: Any, limit: int = 40) -> str:
    if not isinstance(raw, str) or not _ENUM.fullmatch(raw) or len(raw) > limit:
        raise ValueError("enum text")
    return raw


def _weather(result: Any, run: Any, principal_id: str) -> WeatherResult:
    payload = _guard(result, run, principal_id, ToolId.LOOKUP_WEATHER)
    if not set(payload) <= _WEATHER_KEYS or not {"attribution", "temperature"} <= set(payload):
        raise ValueError("weather keys")
    title, url = _attribution(payload["attribution"])
    unit, degrees = _reading(payload["temperature"])
    fields: dict[str, Any] = {}
    if "feelsLikeTemperature" in payload:
        fields["feels_like_unit"], fields["feels_like_degrees"] = _reading(payload["feelsLikeTemperature"])
    condition = payload.get("weatherCondition")
    if condition is not None:
        if not isinstance(condition, dict) or not set(condition) <= {"iconBaseUri", "description", "type"}:
            raise ValueError("condition shape")
        if "type" in condition:
            fields["condition_type"] = _enum_text(condition["type"])
        description = condition.get("description")
        if description is not None:
            if not isinstance(description, dict) or not set(description) <= {"languageCode", "text"} or not isinstance(
                    description.get("text"), str):
                raise ValueError("description shape")
            fields["condition_text"] = description["text"]
        if "iconBaseUri" in condition:  # shape-checked, then dropped (see module docstring)
            if not isinstance(condition["iconBaseUri"], str):
                raise ValueError("icon type")
            check_link(LinkField.WEATHER_ICON_URI, condition["iconBaseUri"])
    precipitation = payload.get("precipitation")
    if precipitation is not None:
        probability = precipitation.get("probability") if isinstance(precipitation, dict) else None
        if not isinstance(probability, dict) or not set(probability) <= {"type", "percent"}:
            raise ValueError("precipitation shape")
        if "percent" in probability:
            fields["precipitation_percent"] = _int(probability["percent"])
        if "type" in probability:
            fields["precipitation_type"] = _enum_text(probability["type"])
    wind = payload.get("wind")
    if wind is not None:
        if not isinstance(wind, dict) or not set(wind) <= {"direction", "gust", "speed"}:
            raise ValueError("wind shape")
        speed = wind.get("speed")
        if speed is not None:
            if not isinstance(speed, dict) or set(speed) != {"unit", "value"} or speed["unit"] not in _WIND_UNITS:
                raise ValueError("wind speed shape")
            fields["wind_speed_unit"], fields["wind_speed_value"] = speed["unit"], _number(speed["value"])
        direction = wind.get("direction")
        if direction is not None:
            if not isinstance(direction, dict) or set(direction) != {"cardinal", "degrees"}:
                raise ValueError("wind direction shape")
            fields["wind_cardinal"], fields["wind_degrees"] = _enum_text(direction["cardinal"], 16), _int(direction["degrees"])
    for key, name in (("relativeHumidity", "relative_humidity"), ("uvIndex", "uv_index")):
        if key in payload:
            fields[name] = _int(payload[key])
    return WeatherResult(
        attribution_title=title, attribution_url=url, tool_call_id=result.tool_call_id,
        temperature_unit=unit, temperature_degrees=degrees, **fields,
    )


def _route(result: Any, run: Any, principal_id: str) -> RouteResult:
    payload = _guard(result, run, principal_id, ToolId.COMPUTE_ROUTES)
    routes = payload.get("routes")
    if set(payload) != {"routes"} or not isinstance(routes, list) or len(routes) != 1:
        raise ValueError("routes shape")  # documented and seen live: exactly one route
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
        distance_meters=distance,
        duration_seconds=float(match.group(1)), travel_mode=mode, walking_warning=mode == "WALK",
    )


def _spelled(payload: dict[str, Any], snake: str, camel: str) -> Any:
    if snake in payload and camel in payload:
        raise ValueError("mixed casing")
    return payload.get(snake, payload.get(camel))


def _resolve(result: Any, run: Any, principal_id: str, tool: ToolId, items_key: str) -> ResolveResult:
    payload = _guard(result, run, principal_id, tool)
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
    if save is not None and not isinstance(save, str):
        raise ValueError("save url")
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


def parse_weather(result: Any, run: ToolRun, principal_id: str) -> WeatherResult:
    return _typed(_weather, result, run, principal_id)  # type: ignore[no-any-return]


def parse_route(result: Any, run: ToolRun, principal_id: str) -> RouteResult:
    return _typed(_route, result, run, principal_id)  # type: ignore[no-any-return]


def parse_resolve_names(result: Any, run: ToolRun, principal_id: str) -> ResolveResult:
    return _typed(_resolve, result, run, principal_id, ToolId.RESOLVE_NAMES, "results")  # type: ignore[no-any-return]


def parse_resolve_urls(result: Any, run: ToolRun, principal_id: str) -> ResolveResult:
    return _typed(_resolve, result, run, principal_id, ToolId.RESOLVE_MAPS_URLS, "entities")  # type: ignore[no-any-return]
