import enum
import json
import types
import typing
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import ValidationError

from app import telemetry as t
from app.domain import DomainClass, ErrorCode, PrincipalKind


TID = "0123456789abcdef0123456789abcdef"


def all_event_classes() -> list[type[t.TraceEvent]]:
    found: list[type[t.TraceEvent]] = []
    stack = list(t.TraceEvent.__subclasses__())
    while stack:
        cls = stack.pop()
        found.append(cls)
        stack.extend(cls.__subclasses__())
    return found


def _has_pattern(field) -> bool:
    return any(getattr(meta, "pattern", None) for meta in field.metadata)


def _check_annotation(name: str, annotation: Any, field) -> None:
    origin = get_origin(annotation)
    if annotation is Any:
        pytest.fail(f"{name}: Any is not allowed")
    if origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            if arg is not type(None):
                _check_annotation(name, arg, field)
        return
    if origin is Literal:
        return
    if origin is tuple:
        args = [a for a in get_args(annotation) if a is not Ellipsis]
        assert field.metadata or any(get_origin(a) is Annotated for a in args), name
        for arg in args:
            _check_annotation(name, arg, field)
        return
    if origin is Annotated:
        base, *meta = get_args(annotation)
        if base is str:
            assert any(getattr(m, "pattern", None) for m in meta), f"{name}: str without pattern"
        else:
            _check_annotation(name, base, field)
        return
    if origin in (dict, list, set, frozenset) or annotation in (dict, list, set):
        pytest.fail(f"{name}: container {annotation} is not allowed")
    if annotation is str:
        assert _has_pattern(field), f"{name}: bare str"
        return
    assert annotation in (int, float, bool) or (
        isinstance(annotation, type) and issubclass(annotation, enum.Enum)
    ), f"{name}: unexpected type {annotation}"


def test_there_are_events_for_every_required_category():
    names = {cls.__name__ for cls in all_event_classes()}
    assert {
        "TurnStarted", "TurnFinished", "GatewayCall", "ToolCall",
        "ValidationRejection", "QuotaEvent", "PolicyEvent", "ErrorEvent",
    } <= names


@pytest.mark.parametrize("cls", all_event_classes(), ids=lambda c: c.__name__)
def test_no_free_form_field_anywhere(cls):
    for name, field in cls.model_fields.items():
        _check_annotation(f"{cls.__name__}.{name}", field.annotation, field)


@pytest.mark.parametrize("cls", all_event_classes(), ids=lambda c: c.__name__)
def test_models_are_frozen_and_forbid_extra(cls):
    assert cls.model_config["extra"] == "forbid"
    assert cls.model_config["frozen"] is True


def test_introspection_would_catch_a_bare_str():
    class Bad(t.TraceEvent):
        note: str

    with pytest.raises(AssertionError):
        _check_annotation("Bad.note", Bad.model_fields["note"].annotation, Bad.model_fields["note"])
    for annotation in (dict[str, str], list[str], Any):
        with pytest.raises(BaseException):
            _check_annotation("x", annotation, Bad.model_fields["note"])
    t.TraceEvent.__subclasses__()  # Bad is discoverable, so the walk above would fail on it
    assert Bad in all_event_classes()


def _error(**kw):
    return t.ErrorEvent(trace_id=TID, code=ErrorCode.POLICY, phase=t.Phase.AUTH, **kw)


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        t.ErrorEvent(trace_id=TID, code=ErrorCode.POLICY, phase=t.Phase.AUTH, message="hello")


def test_events_are_immutable():
    event = _error()
    with pytest.raises(ValidationError):
        event.trace_id = "other"


@pytest.mark.parametrize(
    "bad",
    [
        "43.6532,-79.3832",
        "43.6532 -79.3832",
        "123 Queen Street West",
        "M5V 2T6",
        "user@example.com",
        "Bearer abc.def",
        "Grounded: Cafe Example is open until 9pm",
        "",
        "a" * 65,
        "abc\n",
        "id/with/slash",
    ],
)
def test_opaque_id_rejects_free_text(bad):
    with pytest.raises(ValidationError):
        t.ErrorEvent(trace_id=bad, code=ErrorCode.POLICY, phase=t.Phase.AUTH)
    with pytest.raises(ValidationError):
        t.ToolCall(
            trace_id=TID, tool=t.ToolName.ROUTE_ESTIMATE, outcome=t.ToolOutcome.OK,
            latency_ms=1, billable_units=0, evidence_ids=(bad,),
        )


def test_enum_fields_reject_arbitrary_strings():
    with pytest.raises(ValidationError):
        t.ToolCall(
            trace_id=TID, tool="Cafe Example", outcome=t.ToolOutcome.OK,
            latency_ms=1, billable_units=0,
        )
    with pytest.raises(ValidationError):
        t.ErrorEvent(trace_id=TID, code="free text", phase=t.Phase.AUTH)


def test_model_and_exception_identifiers_are_bounded():
    t.GatewayCall(
        trace_id=TID, model_id="google/gemini-2.5-flash", provider="openrouter",
        finish_reason=t.FinishReason.STOP, input_tokens=1, output_tokens=2, latency_ms=3,
        cost_microusd=4, call_role=t.CallRole.FRAME, model_calls=1, repairs=0, price_table_version="v1", runtime_config_version="2026.10",
    )
    for model in ("43.6532,-79.3832", "a b", "x@y.z", "43.65"):
        with pytest.raises(ValidationError):
            t.GatewayCall(
                trace_id=TID, model_id=model, provider="openrouter",
                finish_reason=t.FinishReason.STOP, input_tokens=1, output_tokens=2,
                latency_ms=3, cost_microusd=4, call_role=t.CallRole.FRAME, model_calls=1, repairs=0, price_table_version="v1",
            )
    with pytest.raises(ValidationError):
        _error(exception_type="ValueError: 43.6532,-79.3832")
    assert _error(exception_type="ValueError").exception_type == "ValueError"


def test_counts_are_bounded():
    with pytest.raises(ValidationError):
        t.GatewayCall(
            trace_id=TID, model_id="m", provider="p", finish_reason=t.FinishReason.STOP,
            input_tokens=-1, output_tokens=0, latency_ms=0, cost_microusd=0,
            call_role=t.CallRole.FRAME, model_calls=1, repairs=0, price_table_version="v1",
        )


def test_sinks_reject_anything_but_trace_events():
    memory = t.InMemorySink()
    for sink in (memory, t.LoggingSink(), t.NullSink()):
        for junk in ("free text", {"trace_id": "a"}, None, 42):
            with pytest.raises(TypeError):
                sink.emit(junk)  # type: ignore[arg-type]
        t._reset_health_for_tests()
        assert t.emit("free text", sink) is False  # module emit never raises; it counts
        assert t.health().validation_errors == 1
    assert memory.events == []
    event = t.PolicyEvent(trace_id=TID, outcome=t.PolicyOutcome.ALLOWED, domain=DomainClass.TRAVEL)
    memory.emit(event)
    assert memory.events == [event]


def test_logging_sink_writes_typed_fields_only(caplog):
    caplog.set_level("DEBUG")
    t.LoggingSink().emit(
        t.TurnFinished(
            trace_id=TID, principal_kind=PrincipalKind.GUEST, domain=DomainClass.TRAVEL,
            allowance_outcome=t.AllowanceOutcome.ALLOWED, replay=False, tool_count=3, latency_ms=5,
        )
    )
    records = [r for r in caplog.records if r.name == t.TRACE_LOGGER_NAME]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["trace_id"] == TID
    assert payload["event"] == "TURN_FINISHED"
    assert t.BLOCKED_MESSAGE not in caplog.text
