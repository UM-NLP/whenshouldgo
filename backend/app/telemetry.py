"""Telemetry allowlist (docs/trip-assistant-security.md, "Telemetry is an allowlist by construction").

Design
------
* Every observable fact leaves the process as a ``TraceEvent``: a frozen, ``extra="forbid"``
  Pydantic model whose fields are only enums, ``Literal`` tags, bools, bounded ints/floats,
  and strictly pattern-bounded opaque identifiers. There is deliberately NO free-form
  ``str``, ``dict``, ``list[str]`` or ``Any`` field anywhere, so a message, address,
  coordinate, token or Maps content has no field to travel in. ``tests/test_telemetry_schema``
  introspects the models and fails if one is ever added.
* ``TraceSink.emit`` is the only emit API. Sinks reject anything that is not a ``TraceEvent``.
  ``InMemorySink`` is for tests; ``LoggingSink`` writes the event's typed fields only (the
  JSON of the validated model) on the ``app.telemetry`` logger.
* Free-text logging guard. A trace event is only trustworthy if nothing else can write
  application text to the logs, so ``install_log_guard()`` (run at import, idempotent)
  wraps the ``logging`` record factory. A record is considered *application-originated*
  when its logger name is ``app`` or ``app.*`` OR it was created by code located in the
  ``app`` package directory (this catches ``logging.info(...)`` on the root logger and
  loggers with other names). Such a record is rewritten at creation time, before any
  handler (including ones added later, e.g. pytest caplog) sees it: message replaced by a
  constant, args/exception/stack info dropped, and any ``extra=`` attributes dropped.
  The only exemption is the exact record ``LoggingSink`` builds from a validated event:
  its msg must be the private ``_TraceMessage`` type and it must be created while emitting.
  The factory is also re-verified lazily (every 256th ``emit``/``trace``) and at startup.
  Rewriting at the factory (instead of a Filter) is deliberate: Logger filters do
  not apply to child loggers and handler filters can be skipped by handlers added later.
* Best effort, not a sandbox. Known bypasses the guard does NOT cover: uvicorn and other
  third-party loggers (their records are not from app code); ``print``/``sys.stderr``
  writes; structlog or other custom logging stacks that never create ``LogRecord`` via the
  factory; ``LogRecord`` objects built directly and passed to ``Handler.handle``; a later
  ``logging.setLogRecordFactory`` that replaces the guard (``ensure_log_guard()`` runs at
  app startup and re-installs or fails loudly, but cannot see later replacement);
  records created by helper/wrapper code outside the app package that logs on behalf of
  app code (``stacklevel`` tricks); and ``warnings`` routed through logging
  (``py.warnings``). Code review and the canary test remain the primary control.
* Failure isolation: ``emit``/``trace`` never raise into callers. Invalid events and sink
  errors are counted in ``health()`` (``validation_errors``, ``sink_errors``).
* Operator diagnostics: ``ErrorEvent.frames`` carries at most 8 redacted code locations
  (``app/<module>.py:<line>``), never exception messages or locals.
* Trace ids are generated server-side only (``bind_trace_id`` takes no argument).
* ``ModelId``/``ProviderId`` are derived from the gateway's own patterns (one-way import).
* Out of scope: uvicorn and third-party loggers. ``main.py`` additionally catches
  unhandled exceptions in an ASGI middleware and answers 500 itself, so a request-bearing
  traceback is never handed to uvicorn's error logger; uvicorn's access log records
  method, path and status only (the API uses headers and a JSON body, never query strings).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import traceback
import dataclasses
from collections import deque
from dataclasses import dataclass
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Annotated, Literal, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .domain import DomainClass, ErrorCode, PrincipalKind
from .gateway import policy as _gateway_policy  # one-way: gateway must never import telemetry

SCHEMA_VERSION = 1

# Opaque identifiers: no '.', ',', '@', whitespace or ':' so coordinates, addresses,
# e-mails and sentences cannot match. Identifiers still cannot stop a caller from putting
# a short word in; they are only ever built from server-generated values or enums.
OpaqueId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}(\.[0-9]{1,6}){0,3}$"
VersionId = Annotated[str, StringConstraints(pattern=VERSION_PATTERN)]
# Model and provider names: must start with a letter (a bare number is never a model).
def _gateway_model_pattern() -> str:
    value = getattr(_gateway_policy, "MODEL_ID_PATTERN", None) or _gateway_policy._PINNED_MODEL
    return str(getattr(value, "pattern", value))


def _gateway_provider_pattern() -> str:
    meta = _gateway_policy.ProviderSlug.__metadata__
    return next(m.pattern for m in meta if getattr(m, "pattern", None))


# Derived from the gateway's own pinned-model and provider patterns so a valid pinned model
# can never fail GatewayCall construction (tests assert parity).
ModelId = Annotated[str, StringConstraints(pattern=_gateway_model_pattern(), max_length=200)]
ProviderId = Annotated[str, StringConstraints(pattern=_gateway_provider_pattern())]
ExceptionType = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
_FRAME_PATTERN = r"^app/[a-z0-9_/]+\.py:[0-9]{1,5}$"
FrameRef = Annotated[str, StringConstraints(pattern=_FRAME_PATTERN)]
# Server-generated uuid4 hex only; deliberately narrower than OpaqueId so bearer-token- or
# key-shaped values cannot be used as trace ids.
TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
EvidenceRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class EventType(StrEnum):
    TURN_STARTED = "TURN_STARTED"
    TURN_FINISHED = "TURN_FINISHED"
    GATEWAY_CALL = "GATEWAY_CALL"
    TOOL_CALL = "TOOL_CALL"
    VALIDATION_REJECTION = "VALIDATION_REJECTION"
    QUOTA = "QUOTA"
    POLICY = "POLICY"
    ERROR = "ERROR"


class Phase(StrEnum):
    REQUEST = "REQUEST"
    AUTH = "AUTH"
    GATE = "GATE"
    QUOTA = "QUOTA"
    MODEL = "MODEL"
    TOOL = "TOOL"
    PATCH = "PATCH"
    RESPONSE = "RESPONSE"


class FinishReason(StrEnum):
    STOP = "STOP"
    LENGTH = "LENGTH"
    TOOL_CALLS = "TOOL_CALLS"
    CONTENT_FILTER = "CONTENT_FILTER"
    ERROR = "ERROR"
    OTHER = "OTHER"


class CallRole(StrEnum):
    """Discovery-contract step of a model call. Defined locally (no gateway import for
    enums); tests assert parity with ``app.gateway.types.CallRole``."""

    FRAME = "FRAME"
    DISCOVER = "DISCOVER"
    COMPOSE = "COMPOSE"


class ToolName(StrEnum):
    """Server allowlist of tool names (mirrors guard.tools_for)."""

    MAPS_GROUNDED_DISCOVERY = "maps_grounded_discovery"
    ROUTE_ESTIMATE = "route_estimate"
    DEADLINE_VALIDATOR = "deadline_validator"
    # The five Grounding Lite tools (docs/model-hosting-decision.md). TODO(verify): names.
    SEARCH_PLACES = "search_places"
    LOOKUP_WEATHER = "lookup_weather"
    COMPUTE_ROUTES = "compute_routes"
    RESOLVE_NAMES = "resolve_names"
    RESOLVE_MAPS_URLS = "resolve_maps_urls"


class ToolOutcome(StrEnum):
    OK = "OK"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    REJECTED = "REJECTED"
    DENIED = "DENIED"


class ValidationReason(StrEnum):
    SCHEMA = "SCHEMA"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    STORAGE_CLASS = "STORAGE_CLASS"
    OVERSIZE = "OVERSIZE"
    HARD_CONSTRAINT = "HARD_CONSTRAINT"
    AREA_DROPPED = "AREA_DROPPED"  # a forgiving drop, not a refusal
    NEEDS_AREA = "NEEDS_AREA"  # discovery without a usable place: asked, no lookup
    OTHER = "OTHER"


class AllowanceOutcome(StrEnum):
    ALLOWED = "ALLOWED"
    EXHAUSTED = "EXHAUSTED"
    NOT_CHECKED = "NOT_CHECKED"


class QuotaOutcome(StrEnum):
    RESERVED = "RESERVED"
    DUPLICATE = "DUPLICATE"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    DENIED = "DENIED"


class PolicyOutcome(StrEnum):
    ALLOWED = "ALLOWED"
    BLOCKED_OUT_OF_SCOPE = "BLOCKED_OUT_OF_SCOPE"
    BLOCKED_MIXED = "BLOCKED_MIXED"
    BLOCKED_SAFETY = "BLOCKED_SAFETY"
    BLOCKED_TOO_LONG = "BLOCKED_TOO_LONG"
    DISABLED_PRINCIPAL = "DISABLED_PRINCIPAL"


class TraceEvent(BaseModel):
    """Base of every event. Subclasses add typed fields only (see module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1, le=1_000)
    trace_id: TraceId
    runtime_config_version: VersionId | None = None


class TurnStarted(TraceEvent):
    event: Literal[EventType.TURN_STARTED] = EventType.TURN_STARTED
    principal_kind: PrincipalKind
    policy_version: VersionId | None = None
    input_token_estimate: int = Field(ge=0, le=10_000_000)


class TurnFinished(TraceEvent):
    event: Literal[EventType.TURN_FINISHED] = EventType.TURN_FINISHED
    principal_kind: PrincipalKind
    domain: DomainClass | None
    allowance_outcome: AllowanceOutcome
    replay: bool
    tool_count: int = Field(ge=0, le=100)
    latency_ms: int = Field(ge=0, le=86_400_000)


class GatewayCall(TraceEvent):
    event: Literal[EventType.GATEWAY_CALL] = EventType.GATEWAY_CALL
    model_id: ModelId
    provider: ProviderId
    finish_reason: FinishReason
    input_tokens: int = Field(ge=0, le=100_000_000)
    output_tokens: int = Field(ge=0, le=100_000_000)
    latency_ms: int = Field(ge=0, le=86_400_000)
    cost_microusd: int = Field(ge=0, le=1_000_000_000)
    call_role: CallRole
    model_calls: int = Field(ge=0, le=100)
    repairs: int = Field(ge=0, le=20)
    price_table_version: VersionId


class ToolCall(TraceEvent):
    event: Literal[EventType.TOOL_CALL] = EventType.TOOL_CALL
    tool: ToolName
    outcome: ToolOutcome
    latency_ms: int = Field(ge=0, le=86_400_000)
    billable_units: int = Field(ge=0, le=1_000_000)
    evidence_ids: tuple[EvidenceRef, ...] = Field(default=(), max_length=50)


class ValidationRejection(TraceEvent):
    event: Literal[EventType.VALIDATION_REJECTION] = EventType.VALIDATION_REJECTION
    reason: ValidationReason
    phase: Phase


class QuotaEvent(TraceEvent):
    event: Literal[EventType.QUOTA] = EventType.QUOTA
    outcome: QuotaOutcome
    principal_kind: PrincipalKind


class PolicyEvent(TraceEvent):
    event: Literal[EventType.POLICY] = EventType.POLICY
    outcome: PolicyOutcome
    domain: DomainClass | None


class ErrorEvent(TraceEvent):
    event: Literal[EventType.ERROR] = EventType.ERROR
    code: ErrorCode
    phase: Phase
    http_status: int | None = Field(default=None, ge=100, le=599)
    # Class name only (never the message). Bounded identifier, code-authored.
    exception_type: ExceptionType | None = None
    # Code locations only ("app/main.py:123"), innermost last, at most 8. Never messages or locals.
    frames: tuple[FrameRef, ...] = Field(default=(), max_length=8)


# --- sinks ---------------------------------------------------------------------------


@runtime_checkable
class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...


def _require_event(event: object) -> TraceEvent:
    if not isinstance(event, TraceEvent):
        raise TypeError("telemetry accepts TraceEvent instances only")
    return event


class InMemorySink:
    """Test sink that keeps validated events."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: TraceEvent) -> None:
        checked = _require_event(event)
        with self._lock:
            self.events.append(checked)


class NullSink:
    def emit(self, event: TraceEvent) -> None:
        _require_event(event)


TRACE_LOGGER_NAME = "app.telemetry"
_EMITTING: ContextVar[bool] = ContextVar("wsg_trace_emitting", default=False)


class _TraceMessage(str):
    """Private marker type for the one log message ``LoggingSink`` builds from a validated
    event. The record factory exempts a record only if its msg is exactly this type."""

    __slots__ = ()


class LoggingSink:
    """Writes the validated event's typed fields as JSON on the ``app.telemetry`` logger."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(TRACE_LOGGER_NAME)
        install_log_guard()

    def emit(self, event: TraceEvent) -> None:
        checked = _require_event(event)
        token = _EMITTING.set(True)
        try:
            self._logger.info(_TraceMessage(checked.model_dump_json(exclude_none=True)))
        finally:
            _EMITTING.reset(token)


_default_sink: TraceSink = NullSink()  # replaced by LoggingSink at module end


def get_default_sink() -> TraceSink:
    return _default_sink


def set_default_sink(sink: TraceSink) -> None:
    global _default_sink
    _default_sink = sink


@dataclass(frozen=True)
class TelemetryHealth:
    """Read-only snapshot of telemetry counters (never negative, not settable)."""

    sink_errors: int = 0
    validation_errors: int = 0
    guard_reinstalls: int = 0
    guard_failures: int = 0


_counters: dict[str, int] = {f.name: 0 for f in dataclasses.fields(TelemetryHealth)}
_health_lock = threading.Lock()


def health() -> TelemetryHealth:
    """Return a snapshot of the counters; suitable for a future /healthz or metrics hook."""
    with _health_lock:
        return TelemetryHealth(**_counters)


def _reset_health_for_tests() -> None:
    with _health_lock:
        for name in _counters:
            _counters[name] = 0


def _count(field: str) -> None:
    with _health_lock:
        _counters[field] += 1


_LAZY_CHECK_EVERY = 256
_calls = 0


def _lazy_guard_check() -> None:
    """Cheap periodic re-verification of the log guard (every 256th emit/trace call)."""
    global _calls
    _calls += 1  # racy by design: only a cadence, never a correctness input
    if _calls % _LAZY_CHECK_EVERY:
        return
    try:
        ensure_log_guard()
    except Exception:  # noqa: BLE001 - telemetry never raises into callers
        _count("guard_failures")


def emit(event: object, sink: TraceSink | None = None) -> bool:
    """Deliver an event. Never raises: bad events and sink failures are counted instead."""
    _lazy_guard_check()
    try:
        checked = _require_event(event)
    except Exception:  # noqa: BLE001
        _count("validation_errors")
        return False
    try:
        (sink or _default_sink).emit(checked)
    except Exception:  # noqa: BLE001 - telemetry must never break callers
        _count("sink_errors")
        return False
    return True


def trace(event_cls: type[TraceEvent], sink: TraceSink | None = None, /, **fields: object) -> bool:
    """Build and emit an event; a validation failure is counted and swallowed."""
    _lazy_guard_check()
    try:
        event = event_cls(**fields)
    except Exception:  # noqa: BLE001
        _count("validation_errors")
        return False
    return emit(event, sink)


# --- request-scoped trace id ----------------------------------------------------------

_TRACE_ID: ContextVar[str | None] = ContextVar("wsg_trace_id", default=None)


def new_trace_id() -> str:
    return uuid4().hex


def current_trace_id() -> str:
    return _TRACE_ID.get() or new_trace_id()


@contextmanager
def bind_trace_id() -> Iterator[str]:
    """Bind a freshly generated, server-side trace id. Callers cannot supply one, so a
    client header (X-Request-Id, traceparent, ...) can never become a trace id."""
    value = new_trace_id()
    token = _TRACE_ID.set(value)
    try:
        yield value
    finally:
        _TRACE_ID.reset(token)


_MAX_WALK = 1000


def frames_from(error: BaseException, limit: int = 8) -> tuple[str, ...]:
    """Redacted traceback shape: "app/<module>.py:<line>" for frames inside this package.

    Walks at most ``_MAX_WALK`` frames with ``walk_tb`` (no source lookup, no locals) and
    keeps the innermost ``limit`` app frames, so deep or recursive tracebacks stay cheap.
    """
    out: deque[str] = deque(maxlen=limit)
    try:
        for walked, (frame, lineno) in enumerate(traceback.walk_tb(error.__traceback__)):
            if walked >= _MAX_WALK:
                break
            path = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
            if not path.startswith(_APP_DIR):
                continue
            ref = "app/" + path[len(_APP_DIR):].replace(os.sep, "/") + f":{lineno}"
            if re.fullmatch(_FRAME_PATTERN, ref):
                out.append(ref)
    except Exception:  # noqa: BLE001
        return ()
    return tuple(out)


def exception_type_name(error: BaseException) -> str:
    """Class name only, truncated; the message is never read."""
    return type(error).__name__[:64]


# --- free-text log guard ---------------------------------------------------------------

BLOCKED_MESSAGE = "[free-text log blocked by telemetry allowlist]"
_APP_DIR = os.path.normcase(os.path.dirname(os.path.abspath(__file__))) + os.sep
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("x", logging.INFO, "x", 0, "x", (), None).__dict__
) | {"message", "asctime"}
_ORIGINAL_FACTORY_ATTR = "_wsg_original_factory"


class _SealedDict(dict):  # type: ignore[type-arg]
    """Record __dict__ that ignores attributes added after creation (``extra=``)."""

    def __setitem__(self, key: str, value: object) -> None:
        if key in _STANDARD_ATTRS:
            super().__setitem__(key, value)


def _is_app_record(record: logging.LogRecord) -> bool:
    if record.name == "app" or record.name.startswith("app."):
        return True
    try:
        return os.path.normcase(os.path.abspath(record.pathname)).startswith(_APP_DIR)
    except Exception:  # noqa: BLE001 - guard must never raise from logging internals
        return False


def _guarded(record: logging.LogRecord) -> logging.LogRecord:
    if not _is_app_record(record):
        return record
    if _EMITTING.get() and type(record.msg) is _TraceMessage and not record.args:
        return record  # the exact record LoggingSink built from a validated event
    cleaned = _SealedDict({k: v for k, v in record.__dict__.items() if k in _STANDARD_ATTRS})
    record.__dict__ = cleaned  # type: ignore[assignment]
    record.msg = BLOCKED_MESSAGE
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    return record


_GUARD_LOCK = threading.RLock()


def install_log_guard() -> None:
    """Idempotently wrap the record factory so application free text cannot be logged."""
    with _GUARD_LOCK:
        current = logging.getLogRecordFactory()
        if getattr(current, _ORIGINAL_FACTORY_ATTR, None) is not None:
            return
        original = current

        def factory(*args: object, **kwargs: object) -> logging.LogRecord:
            return _guarded(original(*args, **kwargs))  # type: ignore[arg-type]

        setattr(factory, _ORIGINAL_FACTORY_ATTR, original)
        logging.setLogRecordFactory(factory)


def log_guard_active() -> bool:
    """Functional probe: does the current record factory block app free text?"""
    try:
        probe = logging.getLogRecordFactory()(
            "app.selfcheck", logging.INFO, "app/selfcheck.py", 0, "probe", (), None
        )
    except Exception:  # noqa: BLE001
        return False
    return probe.msg == BLOCKED_MESSAGE


def ensure_log_guard() -> None:
    """Startup self-check: re-install the guard if it was displaced, else fail loudly."""
    if log_guard_active():
        return
    install_log_guard()
    _count("guard_reinstalls")
    if not log_guard_active():
        raise RuntimeError("telemetry log guard is not active")


def _uninstall_log_guard_for_tests() -> None:
    """Test hook only (not production API): restore the pre-guard record factory."""
    with _GUARD_LOCK:
        original = getattr(logging.getLogRecordFactory(), _ORIGINAL_FACTORY_ATTR, None)
        if original is not None:
            logging.setLogRecordFactory(original)


install_log_guard()
set_default_sink(LoggingSink())
