"""Hardening of telemetry: failure isolation, model-id parity, trace ids, frames, guard."""
import asyncio
import logging
import re
import traceback

import pytest
from fastapi.testclient import TestClient

from app import telemetry as t
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import ErrorCode
from app.gateway import policy as gateway_policy
from app.main import _TraceMiddleware, create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService
from tests.test_api_identity import small_registry

TID = "0123456789abcdef0123456789abcdef"
TOKENS = {"tok": VerifiedIdentity("u1", "anonymous")}
TRAVEL = {"message": "Plan a trip to a Toronto restaurant"}


class RaisingSink:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on  # None = always, else a set of event classes

    def emit(self, event):
        if self.fail_on is None or type(event) in self.fail_on:
            raise RuntimeError("sink down 43.6532,-79.3832")


def client_for(sink):
    quotas = QuotaService(small_registry())
    orchestrator = TurnOrchestrator(quotas, sink=sink, dev_scaffold=True)
    app = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=quotas.policies),
        orchestrator=orchestrator,
        trace_sink=sink,
    )
    return TestClient(app, raise_server_exceptions=False), quotas, orchestrator


def post(client, key="k1", body=TRAVEL, headers=None):
    h = {"Authorization": "Bearer tok", "Idempotency-Key": key, **(headers or {})}
    return client.post("/v1/agent/turns", json=body, headers=h)


def usage_of(quotas):
    for value in vars(quotas).values():
        if isinstance(value, dict) and "u1" in value:
            return value["u1"]
        if isinstance(value, dict):
            for key, item in value.items():
                if "u1" in key if isinstance(key, str) else "u1" in key:
                    return item
    raise AssertionError("usage not found")


@pytest.mark.parametrize(
    "fail_on",
    [None, {t.QuotaEvent}, {t.TurnStarted}, {t.TurnFinished}, {t.PolicyEvent}],
    ids=["all", "quota", "started", "finished", "policy"],
)
def test_failing_sink_changes_nothing(fail_on):
    t._reset_health_for_tests()
    ref, _, ref_orch = client_for(t.InMemorySink())
    expected = post(ref).json()
    client, quotas, orch = client_for(RaisingSink(fail_on))
    first = post(client)
    replay = post(client)
    assert first.status_code == 200 and replay.status_code == 200
    assert first.json()["reply"] == expected["reply"]
    assert replay.json() == first.json()
    assert len(orch._committed) == len(ref_orch._committed) == 1
    usage = usage_of(quotas)
    assert not usage.reserved_keys and usage.successful_turns == 1
    assert t.health().sink_errors > 0


def test_failing_sink_at_commit_does_not_release_or_lose_replay():
    class FailOnCommit:
        def emit(self, event):
            if isinstance(event, t.QuotaEvent) and event.outcome == t.QuotaOutcome.COMMITTED:
                raise RuntimeError("boom")

    client, quotas, orch = client_for(FailOnCommit())
    assert post(client).status_code == 200
    assert len(orch._committed) == 1
    assert usage_of(quotas).successful_turns == 1
    assert post(client).json()["workspace_id"] is not None
    assert usage_of(quotas).successful_turns == 1


def test_invalid_event_is_counted_not_raised():
    t._reset_health_for_tests()
    sink = t.InMemorySink()
    t.trace(t.ErrorEvent, sink, trace_id="43.6532,-79.3832", code="x", phase="y")
    t.emit("free text", sink)  # type: ignore[arg-type]
    assert sink.events == []
    assert t.health().validation_errors == 2
    t.trace(t.ErrorEvent, RaisingSink(), trace_id=TID, code=ErrorCode.POLICY, phase=t.Phase.AUTH)
    assert t.health().sink_errors == 1


def test_validation_failure_in_orchestrator_event_is_isolated():
    t._reset_health_for_tests()
    client, quotas, orch = client_for(t.InMemorySink())
    orch._runtime_config_version = "43.6532,-79.3832"  # invalid for VersionId on every event
    assert post(client).status_code == 200
    assert t.health().validation_errors > 0
    assert len(orch._committed) == 1
    assert usage_of(quotas).successful_turns == 1 and not usage_of(quotas).reserved_keys


# --- M6 model id parity ---------------------------------------------------------------


def gateway_model_pattern() -> str:
    value = getattr(gateway_policy, "MODEL_ID_PATTERN", None) or gateway_policy._PINNED_MODEL
    return getattr(value, "pattern", value)


def _pattern_of(annotated) -> str:
    return next(m.pattern for m in annotated.__metadata__ if getattr(m, "pattern", None))


def test_model_id_pattern_matches_gateway():
    assert _pattern_of(t.ModelId) == gateway_model_pattern()


@pytest.mark.parametrize("model", ["google/gemini-2.5-flash", "01-ai/yi-large", "a/b", "x.y/z_1"])
def test_every_gateway_pinned_model_is_emittable(model):
    assert gateway_policy.is_pinned_model_id(model)
    event = t.GatewayCall(
        trace_id=TID, model_id=model, provider="google-vertex", finish_reason=t.FinishReason.STOP,
        input_tokens=1, output_tokens=1, latency_ms=1, cost_microusd=1,
        call_role=t.CallRole.FRAME, model_calls=1, repairs=0, price_table_version="v1",
    )
    assert event.model_id == model


def test_provider_pattern_matches_gateway():
    assert _pattern_of(t.ProviderId) == _pattern_of(gateway_policy.ProviderSlug)


# --- M9 ---------------------------------------------------------------------------------


def test_turn_started_has_no_principal_ref():
    assert "principal_ref" not in t.TurnStarted.model_fields
    sink = t.InMemorySink()
    client, _, _ = client_for(sink)
    post(client)
    assert "principal_ref" not in repr(sink.events)


# --- L7 trace ids -----------------------------------------------------------------------


def test_client_headers_never_set_the_trace_id():
    sink = t.InMemorySink()
    client, _, _ = client_for(sink)
    spoof = "spoofedtraceid1234"
    post(client, headers={"X-Trace-Id": spoof, "X-Request-Id": spoof, "Traceparent": spoof})
    assert sink.events and all(e.trace_id != spoof for e in sink.events)
    assert all(re.fullmatch(r"[0-9a-f]{32}", e.trace_id) for e in sink.events)


def test_bind_trace_id_rejects_caller_supplied_values():
    with pytest.raises(TypeError):
        with t.bind_trace_id("43.6532,-79.3832"):  # type: ignore[call-arg]
            pass


# --- L4 -----------------------------------------------------------------------------------


def test_generic_runtimeerror_has_no_context_when_response_started():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ValueError("secret 43.6532,-79.3832")

    async def noop(*args):
        return None

    sink = t.InMemorySink()
    state = type("S", (), {"sink": sink})()
    scope = {"type": "http", "app": type("A", (), {"state": state})()}
    with pytest.raises(RuntimeError) as info:
        asyncio.run(_TraceMiddleware(app)(scope, noop, noop))
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert "secret" not in "".join(traceback.format_exception(info.value))


# --- M10 frames -----------------------------------------------------------------------------


def test_error_event_has_redacted_frames_only():
    sink = t.InMemorySink()
    client, _, orch = client_for(sink)

    def boom(*args, **kwargs):
        secret_local = "Cafe Example 43.6532,-79.3832"  # noqa: F841
        raise RuntimeError("msg 43.6532,-79.3832 Grounded: Cafe Example")

    orch.run_turn = boom  # type: ignore[method-assign]
    assert post(client).status_code == 500
    error = next(e for e in sink.events if e.event == t.EventType.ERROR)
    assert error.frames and len(error.frames) <= 8
    assert any(f.startswith("app/main.py:") for f in error.frames)
    assert all(re.fullmatch(r"^app/[a-z0-9_/]+\.py:[0-9]{1,5}$", f) for f in error.frames)
    blob = repr(error)
    for canary in ("43.6532", "Cafe Example", "secret_local", "Grounded"):
        assert canary not in blob


def test_frames_are_capped_and_pattern_enforced():
    with pytest.raises(Exception):
        t.ErrorEvent(trace_id=TID, code="POLICY", phase=t.Phase.AUTH, frames=("x" * 10,))
    with pytest.raises(Exception):
        t.ErrorEvent(
            trace_id=TID, code="POLICY", phase=t.Phase.AUTH,
            frames=tuple(f"app/a.py:{i}" for i in range(1, 10)),
        )


# --- M10 guard self-check ------------------------------------------------------------------


def test_guard_selfcheck_detects_replaced_factory_and_reinstalls():
    saved = logging.getLogRecordFactory()
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        assert not t.log_guard_active()
        before = t.health().guard_reinstalls
        create_app()
        assert t.log_guard_active()
        assert t.health().guard_reinstalls == before + 1
    finally:
        logging.setLogRecordFactory(saved)


def test_guard_selfcheck_fails_loudly_when_it_cannot_install(monkeypatch):
    saved = logging.getLogRecordFactory()
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        monkeypatch.setattr(t, "install_log_guard", lambda: None)
        with pytest.raises(RuntimeError):
            t.ensure_log_guard()
    finally:
        logging.setLogRecordFactory(saved)
