"""Grounding Lite client: a protocol plus an MCP adapter with an injectable transport.

Fail-closed, like the model gateway: exact endpoint and host, tool allowlist, schema-checked
arguments (the bounded validator in ``app.gateway.schema``), size and time bounds, typed
errors with static messages, a per-run tool-call budget and a kill switch. The API key reaches
the wire only through an ``httpx.Auth`` object.

Tool results are UNTRUSTED data: they may carry injection text. They are never interpreted as
instructions, never logged, and never reach telemetry (events hold enums and counts).

Raised errors hold no payload, arguments or key: every internal step RETURNS a result or an
error object, and only ``call_tool`` raises, after dropping its reference to the arguments.

Sources: the MCP reference, tool references and resolution-API page under
developers.google.com/maps/ai/grounding-lite (retrieved 2026-10-15) document the endpoint,
the tool names, the ``X-Goog-Api-Key`` header and ``Accept: application/json,
text/event-stream``. Still unverified, marked TODO(verify-live): snake_case versus camelCase
argument names, the response encoding in practice, and any initialize/session handshake.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from .. import telemetry
from ..domain import ContentClass, Ident, RuntimeConfig
from ..gateway.errors import TransportFailure, TransportTimeout
from ..gateway.openrouter import Credential, guarded, strict_loads
from ..gateway.schema import MAX_ARGUMENT_DEPTH, json_depth, validate
from ..gateway.types import ToolSpec
from . import _mint
from .contracts import (
    GroundFrozen,
    canonical_json,
    content_hash,
    display_hash,
    extract_search_places,
    payload_digest,
)
from .errors import (
    DiscoveryAccessDenied,
    DiscoveryBadRequest,
    DiscoveryConfigError,
    DiscoveryDisabledError,
    DiscoveryError,
    DiscoveryProtocolError,
    DiscoveryRateLimited,
    DiscoverySessionRequired,
    DiscoveryTimeout,
    DiscoveryToolError,
    DiscoveryUnavailable,
    InvalidToolArguments,
    MalformedToolResult,
    OversizeResult,
    ToolBudgetExceeded,
    ToolNotAllowed,
)
from .ids import ToolId
from .text import has_lone_surrogate, short_field_problem
from .uris import check_input_maps_url

GROUNDING_LITE_HOST = "mapstools.googleapis.com"
GROUNDING_LITE_ENDPOINT = f"https://{GROUNDING_LITE_HOST}/mcp"
API_KEY_HEADER = "X-Goog-Api-Key"
ALLOWED_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

MAX_RESPONSE_BYTES = 262_144
MAX_ARGUMENT_BYTES = 4_096
MAX_RESULT_DEPTH = 8
MAX_TIMEOUT_SECONDS = 10.0
# One tenant's request must not disable discovery for everyone. 401 and 402 mean OUR key or
# billing is broken (global by nature) and trip at once; a 403 needs this many in a row.
ACCESS_DENIED_TRIP_THRESHOLD = 3
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_API_KEY = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no whitespace or controls


def _object(properties: dict[str, Any], required: list[str], **extra: Any) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": required,
            "properties": properties, **extra}


_LATLNG = _object(
    {"latitude": {"type": "number", "minimum": -90, "maximum": 90},
     "longitude": {"type": "number", "minimum": -180, "maximum": 180}},
    ["latitude", "longitude"],
)
_TEXT = {"type": "string", "minLength": 1, "maxLength": 300}
_CODE = {"type": "string", "minLength": 1, "maxLength": 16}
_WAYPOINT = _object(
    {"address": _TEXT, "lat_lng": _LATLNG, "place_id": _TEXT}, [],
    x_exactly_one_of=["address", "lat_lng", "place_id"],
)

# TODO(verify-live): argument names are snake_case as in the tool prose and example calls; the
# REST schemas spell them camelCase (textQuery, locationBias, latLng ...). The structure,
# required fields and limits below follow the documented reference.
TOOL_SPECS: dict[ToolId, ToolSpec] = {
    ToolId.SEARCH_PLACES: ToolSpec(
        name="search_places", description="Search places for a text query.",
        parameters=_object(
            {"text_query": _TEXT,
             "location_bias": _object({"circle": _object(
                 {"center": _LATLNG, "radius_meters": {"type": "number", "minimum": 1, "maximum": 50_000}},
                 ["center"])}, ["circle"]),
             "language_code": _CODE, "region_code": _CODE},
            ["text_query"]),
    ),
    ToolId.LOOKUP_WEATHER: ToolSpec(
        name="lookup_weather", description="Look up weather for a location.",
        parameters=_object(
            {"location": _object(
                {"lat_lng": _LATLNG, "place_id": _TEXT, "address": _TEXT}, [],
                x_exactly_one_of=["lat_lng", "place_id", "address"]),
             "date": _object({"year": {"type": "integer", "minimum": 2000, "maximum": 2100},
                              "month": {"type": "integer", "minimum": 1, "maximum": 12},
                              "day": {"type": "integer", "minimum": 1, "maximum": 31}},
                             ["year", "month", "day"]),
             "hour": {"type": "integer", "minimum": 0, "maximum": 23},
             "units_system": {"type": "string", "enum": ["METRIC", "IMPERIAL"]}},
            ["location"]),
    ),
    ToolId.COMPUTE_ROUTES: ToolSpec(
        name="compute_routes", description="Compute a route between two waypoints.",
        parameters=_object(
            {"origin": _WAYPOINT, "destination": _WAYPOINT,
             "travel_mode": {"type": "string", "enum": ["DRIVE", "WALK"]}},
            ["origin", "destination"]),
    ),
    ToolId.RESOLVE_NAMES: ToolSpec(
        name="resolve_names", description="Resolve specific place names.",
        parameters=_object(
            {"queries": {"type": "array", "minItems": 1, "maxItems": 20,
                         "items": _object({"text": _TEXT}, ["text"])},
             "location_bias": _object({"viewport": _object({"low": _LATLNG, "high": _LATLNG},
                                                           ["low", "high"])}, ["viewport"]),
             "region_code": _CODE},
            ["queries"]),
    ),
    ToolId.RESOLVE_MAPS_URLS: ToolSpec(
        name="resolve_maps_urls", description="Resolve single-place Google Maps URLs.",
        parameters=_object(
            {"urls": {"type": "array", "minItems": 1, "maxItems": 20,
                      "items": {"type": "string", "minLength": 1, "maxLength": 2048}}},
            ["urls"]),
    ),
}


def validate_endpoint(url: str) -> None:
    """https, the exact host, the exact path; no userinfo, port, query, fragment or controls."""
    bad = (
        not isinstance(url, str) or not url.isascii() or not url.isprintable()
        or any(ch.isspace() for ch in url)
    )
    parts = None if bad else guarded(urlsplit, url)[0]
    if (
        parts is None or parts.scheme != "https" or parts.netloc != GROUNDING_LITE_HOST
        or parts.path != "/mcp" or parts.query or parts.fragment
    ):
        raise DiscoveryConfigError("endpoint must be the pinned Grounding Lite endpoint")


# --- run ledger ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class LedgerInfo:
    """What a tool returned that later claims are checked against: ids and hashes only."""

    place_ids: frozenset[str] = frozenset()
    text_hash: str | None = None  # search_places only
    display_hash: str | None = None  # search_places only
    payload_hash: str | None = None  # EVERY tool: hash of the whole canonical payload


@dataclass(frozen=True)
class CallRecord:
    tool: ToolId
    place_ids: frozenset[str] = frozenset()
    text_hash: str | None = None
    display_hash: str | None = None
    payload_hash: str | None = None


@dataclass(frozen=True)
class CallTicket:
    """A reserved call: its id and a per-run sequence number used as the JSON-RPC request id."""

    call_id: str
    seq: int


def _check_authority(authority: object) -> None:
    if authority is not _mint.AUTHORITY:
        raise DiscoveryConfigError("only the discovery client may write the run ledger")


class ToolRun:
    """One run's tool-call budget and provenance ledger, bound to a principal and a run id
    given by the orchestrator. Thread-safe. Only the client (holding the private authority)
    can begin a call or write the ledger; the validator and model code can only read it."""

    def __init__(self, *, principal_id: str, run_id: str, max_calls: int = 12) -> None:
        if not (isinstance(principal_id, str) and _ID.fullmatch(principal_id)
                and isinstance(run_id, str) and _ID.fullmatch(run_id)):
            raise ValueError("principal_id and run_id must be bounded identifiers")
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.principal_id = principal_id
        self.run_id = run_id
        self.max_calls = max_calls
        self._lock = threading.Lock()
        self._used = 0
        self._records: dict[str, CallRecord] = {}

    @classmethod
    def from_config(cls, config: RuntimeConfig, *, principal_id: str, run_id: str) -> ToolRun:
        return cls(principal_id=principal_id, run_id=run_id, max_calls=config.token_ceilings.run_tool_calls)

    @property
    def calls_used(self) -> int:
        with self._lock:
            return self._used

    def begin(self, tool: ToolId, *, authority: object) -> CallTicket:
        """Reserve one call. Failed calls count too; refused-before-sending ones never begin."""
        _check_authority(authority)
        with self._lock:
            if self._used >= self.max_calls:
                raise ToolBudgetExceeded()
            self._used += 1
            call_id = f"tc-{uuid4().hex[:16]}"
            self._records[call_id] = CallRecord(tool=tool)
            return CallTicket(call_id, self._used)

    def record(self, call_id: str, info: LedgerInfo, *, authority: object) -> None:
        _check_authority(authority)
        with self._lock:
            current = self._records.get(call_id)
            if current is None:
                raise KeyError("unknown call id")
            self._records[call_id] = CallRecord(
                current.tool, info.place_ids, info.text_hash, info.display_hash, info.payload_hash)

    def record_for(self, call_id: str) -> CallRecord | None:
        with self._lock:
            return self._records.get(call_id)


def payload_ledger_info(tool: ToolId, payload: object) -> LedgerInfo:
    """Ids and hashes of a raw tool payload, computed with the same extraction the parser
    uses. Every tool gets a whole-payload hash; a payload that is not the documented
    search_places shape gets no text or display hash, so it can never match a GroundedAnswer."""
    whole = payload_digest(payload)  # every tool: anchors the whole payload
    if tool != ToolId.SEARCH_PLACES:
        return LedgerInfo(payload_hash=whole)
    extracted = extract_search_places(payload)
    if extracted is None:
        return LedgerInfo(payload_hash=whole)
    text, places = extracted
    return LedgerInfo(
        place_ids=frozenset(p[0] for p in places),
        text_hash=content_hash(text),
        display_hash=display_hash(text, places),
        payload_hash=whole,
    )


class ToolResult(GroundFrozen):
    """An untrusted tool result. The payload is stored as canonical JSON and re-parsed on every
    access, so it cannot be mutated through the result and ``model_copy`` cannot swap it
    unnoticed: the parsers also check its hash against the run ledger. It appears in no repr.
    Built only by the client: it carries a private, REQUIRED token."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True,
                              arbitrary_types_allowed=True)

    tool: ToolId
    tool_call_id: Ident
    run_id: Ident
    retrieved_at: AwareDatetime
    payload_json: str = Field(repr=False)
    size_bytes: int = Field(ge=0)
    billable_units: int = Field(ge=0)
    content_class: ContentClass = ContentClass.MAPS_GROUNDED
    travel_mode: str | None = None  # compute_routes only: the mode that was requested
    issued: _mint.IssuedToken = Field(repr=False)

    def __init__(self, /, **data: Any) -> None:
        if "payload" in data:  # the constructor takes the payload; the model stores frozen JSON
            data["payload_json"] = canonical_json(data.pop("payload"))
        super().__init__(**data)

    @property
    def payload(self) -> dict[str, Any]:
        """A fresh copy every time: mutating it changes nothing."""
        parsed = json.loads(self.payload_json)
        if not isinstance(parsed, dict):
            raise ValueError("payload is not an object")
        return parsed


# --- kill switch ----------------------------------------------------------------------------------
@runtime_checkable
class KillSwitchProbe(Protocol):
    """What the client may do with a kill switch: read it and trip it. Never reset it."""

    @property
    def tripped(self) -> bool: ...

    def trip(self) -> None: ...


class KillSwitch:
    """In-memory global kill switch, consistent with the gateway's QuotaLatch; reset only
    explicitly and only by operators (``reset`` is not part of ``KillSwitchProbe``).

    PRODUCTION NOTE: state must be shared across Cloud Run instances, so the real switch needs
    a Firestore-backed ``KillSwitchProbe``. This one protects a single process only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped = False

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    def trip(self) -> None:
        with self._lock:
            self._tripped = True

    def reset(self) -> None:
        with self._lock:
            self._tripped = False


# --- transport ----------------------------------------------------------------------------------------
def validate_mcp_body(body: Any) -> None:
    ok = (
        isinstance(body, dict) and set(body) == {"jsonrpc", "id", "method", "params"}
        and body["jsonrpc"] == "2.0" and body["method"] == "tools/call"
        and isinstance(body["id"], int) and not isinstance(body["id"], bool)
        and isinstance(body["params"], dict) and set(body["params"]) == {"name", "arguments"}
        and body["params"]["name"] in {t.value for t in ToolId}
        and isinstance(body["params"]["arguments"], dict)
    )
    if not ok:
        raise DiscoveryConfigError("request body is not an allowlisted tools/call")


class McpRequest(GroundFrozen):
    """What a transport (and any trace) sees: no credential, exact headers, exact endpoint."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any] = Field(repr=False)
    timeout_seconds: float = Field(gt=0, le=MAX_TIMEOUT_SECONDS)

    @field_validator("headers")
    @classmethod
    def _exact_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        if headers != ALLOWED_HEADERS:
            raise ValueError("headers must be exactly the allowlist")
        return headers

    @model_validator(mode="after")
    def _checked(self) -> McpRequest:
        validate_endpoint(self.url)
        validate_mcp_body(self.body)
        return self


class McpResponse(GroundFrozen):
    status_code: int
    body: Any = Field(default=None, repr=False)
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _measure(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("size_bytes") is None:
            body = data.get("body")
            size, ok = guarded(lambda: len(json.dumps(body))) if body is not None else (0, True)
            return {**data, "size_bytes": size if ok else MAX_RESPONSE_BYTES + 1}
        return data


@runtime_checkable
class McpTransport(Protocol):
    def send(self, request: McpRequest, credential: Credential) -> McpResponse: ...


def parse_single_sse_event(data: bytes) -> dict[str, Any] | None:
    """The JSON object in a text/event-stream reply that carries exactly one COMPLETE data event
    of type ``message`` (or no type), else None. An event is complete only when a blank line ends
    it, so a final unterminated event is discarded. Comments and other fields are ignored,
    multi-line data is joined with newlines, and any other event type (``error``, ``ping`` ...)
    rejects the reply. TODO(verify-live): whether the server really answers with one event."""
    text, ok = guarded(lambda: data.decode("utf-8"))
    if not ok or not text:
        return None
    events: list[tuple[str | None, list[str]]] = []
    kind: str | None = None
    lines: list[str] = []
    # The last element after the final newline is an unterminated fragment (or empty): drop it.
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")[:-1]:
        if line == "":
            if lines or kind is not None:
                events.append((kind, lines))
            kind, lines = None, []
            continue
        if line.startswith("data:"):
            value = line[5:]
            lines.append(value[1:] if value.startswith(" ") else value)
        elif line.startswith("event:"):
            value = line[6:]
            kind = value[1:] if value.startswith(" ") else value
    # whatever follows the last blank line was never terminated: discarded
    complete = [event for event in events if event[1]]
    if len(complete) != 1:
        return None
    event_kind, event_lines = complete[0]
    if event_kind not in (None, "message"):
        return None
    parsed, ok = guarded(strict_loads, "\n".join(event_lines))
    return parsed if ok and isinstance(parsed, dict) else None


class _ApiKeyAuth(httpx.Auth):
    """Adds the API key header inside httpx, so no headers dict holds the key."""

    def __init__(self, credential: Credential) -> None:
        self._credential = credential

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers[API_KEY_HEADER] = self._credential.reveal()
        yield request

    def __repr__(self) -> str:
        return "_ApiKeyAuth(<redacted>)"


class HttpxMcpTransport:
    """Real transport. Builds its own locked-down client (no injection), re-validates the
    endpoint, headers and body, never follows redirects or reads proxy settings, asks for
    identity encoding, reads raw bytes under a size cap and stops at the request deadline."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, _transport: Any = None
    ) -> None:
        kwargs: dict[str, Any] = {
            "trust_env": False, "follow_redirects": False, "verify": True,
            "timeout": httpx.Timeout(10.0, connect=5.0),
        }
        if _transport is not None:  # test seam only; see for_tests
            kwargs["transport"] = _transport
        self._client = httpx.Client(**kwargs)
        self._clock = clock

    @classmethod
    def for_tests(
        cls, transport: httpx.BaseTransport, *, clock: Callable[[], float] = time.monotonic
    ) -> HttpxMcpTransport:
        return cls(clock=clock, _transport=transport)

    def send(self, request: McpRequest, credential: Credential) -> McpResponse:
        validate_endpoint(request.url)
        validate_mcp_body(request.body)
        if request.headers != ALLOWED_HEADERS:
            raise DiscoveryConfigError("headers must be exactly the allowlist")
        timeout = float(request.timeout_seconds)
        deadline = self._clock() + timeout
        outcome = "ok"
        status = 0
        media = ""
        chunks: list[bytes] = []
        total = 0
        try:
            with self._client.stream(
                "POST", request.url, json=request.body,
                headers={**request.headers, "Accept-Encoding": "identity"},
                auth=_ApiKeyAuth(credential),
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            ) as response:
                status = response.status_code
                media = response.headers.get("content-type", "application/json").split(";")[0].strip().lower()
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    outcome = "failure"  # never decode: a small body must not expand past the cap
                else:
                    # A pre-read response only happens with in-memory test transports.
                    source = [response.content] if response.is_stream_consumed else response.iter_raw()
                    for chunk in source:
                        total += len(chunk)
                        if self._clock() > deadline:
                            outcome = "timeout"
                            break
                        if total > MAX_RESPONSE_BYTES:
                            outcome = "oversize"
                            break
                        chunks.append(chunk)
        except httpx.TimeoutException:
            outcome = "timeout"
        except Exception:  # noqa: BLE001 - httpx.InvalidURL, TLS, encoding ...; nothing is kept
            outcome = "failure"
        if outcome == "timeout":
            raise TransportTimeout
        if outcome != "ok":
            raise TransportFailure
        raw = b"".join(chunks)
        body: Any = None
        if media == "application/json":
            parsed, ok = guarded(strict_loads, raw)
            body = parsed if ok else None
        elif media == "text/event-stream":
            body = parse_single_sse_event(raw)
        return McpResponse(status_code=status, body=body, size_bytes=total)


# --- the client -------------------------------------------------------------------------------------------
@runtime_checkable
class GroundingLiteClient(Protocol):
    def call_tool(self, run: ToolRun, tool: str, arguments: Mapping[str, Any]) -> ToolResult: ...


def _strings(value: Any) -> Iterator[str]:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _outcome_for(error: DiscoveryError) -> telemetry.ToolOutcome:
    if isinstance(error, DiscoveryTimeout):
        return telemetry.ToolOutcome.TIMEOUT
    if isinstance(error, (MalformedToolResult, OversizeResult)):
        return telemetry.ToolOutcome.REJECTED
    if error.calls == 0:
        return telemetry.ToolOutcome.DENIED
    return telemetry.ToolOutcome.ERROR


class GroundingLiteMcpClient:
    def __init__(
        self,
        transport: McpTransport,
        secret_provider: Callable[[], str],
        *,
        config: RuntimeConfig,
        kill_switch: KillSwitchProbe | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        endpoint: str = GROUNDING_LITE_ENDPOINT,
        sink: telemetry.TraceSink | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        validate_endpoint(endpoint)
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise DiscoveryConfigError("timeout must be in (0, 10] seconds")
        self._transport = transport
        self._secret_provider = secret_provider
        self._config = config
        self._kill_switch = kill_switch if kill_switch is not None else KillSwitch()
        self._clock = clock
        self._now = now
        self._endpoint = endpoint
        self._sink = sink
        self._timeout = timeout_seconds
        self._streak_lock = threading.Lock()
        self._denied_streak = 0

    # -- public ---------------------------------------------------------------------------------
    def call_tool(self, run: ToolRun, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
        started = self._clock()
        outcome = self._run(run, tool, arguments)
        # Drop the caller's mapping from this frame: it may hold private trip text, and this
        # frame is the one that ends up in a raised error's traceback.
        del arguments
        elapsed_ms = int(max(0.0, self._clock() - started) * 1000)
        tool_name = self._tool_name(tool)
        if isinstance(outcome, DiscoveryError):
            self._emit(tool_name, _outcome_for(outcome), elapsed_ms, min(1, outcome.calls), outcome.call_id)
            raise outcome
        self._emit(tool_name, telemetry.ToolOutcome.OK, elapsed_ms, outcome.billable_units, outcome.tool_call_id)
        return outcome

    # -- internals: each step returns a value or an error object; none of them raises ---------------
    @staticmethod
    def _tool_name(tool: str) -> telemetry.ToolName:
        if tool in {t.value for t in ToolId}:
            return telemetry.ToolName(tool)
        return telemetry.ToolName.MAPS_GROUNDED_DISCOVERY  # a refused name cannot be an enum value

    def _emit(self, tool: telemetry.ToolName, outcome: telemetry.ToolOutcome, latency_ms: int,
              units: int, call_id: str | None) -> None:
        telemetry.trace(
            telemetry.ToolCall, self._sink,
            trace_id=telemetry.current_trace_id(),
            runtime_config_version=self._config.version,
            tool=tool, outcome=outcome, latency_ms=latency_ms, billable_units=units,
            evidence_ids=(f"ev-{call_id}",) if call_id else (),
        )

    def _run(self, run: ToolRun, tool: str, arguments: Mapping[str, Any]) -> ToolResult | DiscoveryError:
        if self._kill_switch.tripped:
            return DiscoveryDisabledError()
        tool_id = self._allowed_tool(tool)
        if isinstance(tool_id, DiscoveryError):
            return tool_id
        clean = self._checked_arguments(tool_id, arguments)
        if isinstance(clean, DiscoveryError):
            return clean
        credential = self._credential()
        if isinstance(credential, DiscoveryError):
            return credential
        ticket = self._begin(run, tool_id)
        if isinstance(ticket, DiscoveryError):
            return ticket
        outcome = self._send_and_parse(run, tool_id, clean, credential, ticket)
        if isinstance(outcome, DiscoveryError):
            outcome.call_id = ticket.call_id
        return outcome

    @staticmethod
    def _allowed_tool(tool: str) -> ToolId | DiscoveryError:
        for tool_id in ToolId:
            if tool == tool_id.value:
                return tool_id
        return ToolNotAllowed()

    @staticmethod
    def _checked_arguments(tool_id: ToolId, arguments: Mapping[str, Any]) -> dict[str, Any] | DiscoveryError:
        bad = InvalidToolArguments()
        if not isinstance(arguments, Mapping):
            return bad
        args = dict(arguments)
        encoded, ok = guarded(lambda: json.dumps(args, allow_nan=False))
        if not ok or len(encoded.encode("utf-8", "replace")) > MAX_ARGUMENT_BYTES:
            return bad
        depth, depth_ok = guarded(json_depth, args)
        valid, valid_ok = guarded(validate, TOOL_SPECS[tool_id].parameters, args)
        if not (depth_ok and depth <= MAX_ARGUMENT_DEPTH and valid_ok and valid):
            return bad
        if any(short_field_problem(text) or has_lone_surrogate(text) for text in _strings(args)):
            return bad
        if tool_id == ToolId.RESOLVE_MAPS_URLS:
            for url in args["urls"]:
                _, url_ok = guarded(check_input_maps_url, url)
                if not url_ok:
                    return bad
        return args

    def _credential(self) -> Credential | DiscoveryError:
        failed = False
        try:
            raw = self._secret_provider()
        except Exception:  # noqa: BLE001 - never keep the provider's exception (may hold the key)
            raw = None
            failed = True
        key = raw.strip() if isinstance(raw, str) else ""
        raw = None
        if failed or not _API_KEY.fullmatch(key):
            return DiscoveryConfigError("API key is unavailable or malformed")
        return Credential(key)

    @staticmethod
    def _begin(run: ToolRun, tool_id: ToolId) -> CallTicket | DiscoveryError:
        ticket: CallTicket | None = None
        try:
            ticket = run.begin(tool_id, authority=_mint.AUTHORITY)
        except ToolBudgetExceeded:
            ticket = None
        return ticket if ticket is not None else ToolBudgetExceeded()

    def _send_and_parse(
        self, run: ToolRun, tool_id: ToolId, args: dict[str, Any], credential: Credential, ticket: CallTicket
    ) -> ToolResult | DiscoveryError:
        request, built = guarded(
            lambda: McpRequest(
                url=self._endpoint, headers=dict(ALLOWED_HEADERS), timeout_seconds=self._timeout,
                body={"jsonrpc": "2.0", "id": ticket.seq, "method": "tools/call",
                      "params": {"name": tool_id.value, "arguments": args}},
            )
        )
        if not built:
            return DiscoveryConfigError("request could not be built")
        failure: DiscoveryError | None = None
        response: McpResponse | None = None
        try:
            response = self._transport.send(request, credential)
        except DiscoveryError as refusal:  # a transport's own refusal keeps its type
            failure = refusal
        except TransportTimeout:
            failure = DiscoveryTimeout()
        except Exception:  # noqa: BLE001 - TransportFailure and anything unexpected
            failure = DiscoveryUnavailable()
        if failure is None and not isinstance(response, McpResponse):
            failure = DiscoveryUnavailable()
        if failure is not None:
            failure.calls = max(failure.calls, 1)
            return failure
        assert response is not None
        outcome = self._parse(run, tool_id, args, ticket, response)
        if isinstance(outcome, DiscoveryError):
            outcome.calls = 1
        return outcome

    def _note_status(self, status: int) -> None:
        """Kill-switch policy. 401 and 402 trip at once (our key or billing is broken); 403
        trips only after ACCESS_DENIED_TRIP_THRESHOLD in a row; anything else breaks the streak."""
        if status in (401, 402):
            self._kill_switch.trip()
            return
        with self._streak_lock:
            self._denied_streak = self._denied_streak + 1 if status == 403 else 0
            tripped = self._denied_streak >= ACCESS_DENIED_TRIP_THRESHOLD
        if tripped:
            self._kill_switch.trip()

    def _parse(self, run: ToolRun, tool_id: ToolId, args: dict[str, Any], ticket: CallTicket,
               response: McpResponse) -> ToolResult | DiscoveryError:
        call_id = ticket.call_id
        status = response.status_code
        self._note_status(status)
        if status != 200:
            return self._status_error(status)
        if (response.size_bytes or 0) > MAX_RESPONSE_BYTES:
            return OversizeResult()
        body = response.body
        if isinstance(body, dict) and "error" in body:
            return DiscoveryProtocolError()  # a JSON-RPC error object; its message is dropped
        payload = self._payload(body, ticket.seq)
        if isinstance(payload, DiscoveryError):
            return payload
        depth, depth_ok = guarded(json_depth, payload)
        _, text_ok = guarded(self._check_strings, payload)
        if not depth_ok or depth > MAX_RESULT_DEPTH or not text_ok:
            return MalformedToolResult()
        info, info_ok = guarded(payload_ledger_info, tool_id, payload)
        if not info_ok:
            return MalformedToolResult()
        run.record(call_id, info, authority=_mint.AUTHORITY)
        result, built = guarded(
            lambda: ToolResult(
                tool=tool_id, tool_call_id=call_id, run_id=run.run_id, retrieved_at=self._now(),
                payload=payload, size_bytes=response.size_bytes or 0, billable_units=1,
                travel_mode=(args.get("travel_mode", "DRIVE") if tool_id == ToolId.COMPUTE_ROUTES else None),
                issued=_mint.mint(call_id),
            )
        )
        return result if built else MalformedToolResult()

    @staticmethod
    def _check_strings(payload: object) -> None:
        """A lone surrogate anywhere makes the whole payload unusable (it cannot be encoded)."""
        for text in _strings(payload):
            if has_lone_surrogate(text):
                raise ValueError("lone surrogate")

    @staticmethod
    def _status_error(status: int) -> DiscoveryError:
        error: DiscoveryError
        if status == 429:
            error = DiscoveryRateLimited()
        elif status in (401, 402, 403):
            error = DiscoveryAccessDenied()
        elif status == 404:
            error = DiscoverySessionRequired()
        elif status == 400:
            error = DiscoveryBadRequest()
        elif status in (408, 504):
            error = DiscoveryTimeout()
        elif status >= 500:
            error = DiscoveryUnavailable()
        else:
            error = MalformedToolResult()  # includes redirects: never followed, never trusted
        return error

    @staticmethod
    def _payload(body: Any, expected_id: int) -> dict[str, Any] | DiscoveryError:
        """The tool payload from a JSON-RPC reply. Nothing in it is interpreted. The reply must
        echo the request id exactly (an int, never a bool, float or string)."""
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return MalformedToolResult()
        reply_id = body.get("id")
        if isinstance(reply_id, bool) or not isinstance(reply_id, int) or reply_id != expected_id:
            return MalformedToolResult()
        result = body.get("result")
        if not isinstance(result, dict):
            return MalformedToolResult()
        if result.get("isError") is True:
            return DiscoveryToolError()  # the text is untrusted and dropped
        structured = result.get("structuredContent")
        if structured is not None:
            return structured if isinstance(structured, dict) else MalformedToolResult()
        content = result.get("content")
        first = content[0] if isinstance(content, list) and content else None
        if not isinstance(first, dict) or first.get("type") != "text" or not isinstance(first.get("text"), str):
            return MalformedToolResult()
        parsed, ok = guarded(strict_loads, first["text"])
        return parsed if ok and isinstance(parsed, dict) else MalformedToolResult()
