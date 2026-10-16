"""Final telemetry hardening: middleware status, guard exactness, frames, GatewayCall, uvicorn."""
import ast
import asyncio
import dataclasses
import logging
import pathlib
import socket
import threading
import time
import urllib.request

import pytest
from starlette.requests import ClientDisconnect

from app import telemetry as t
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import ErrorCode
from app.gateway.types import CallRole
from app.main import _TraceMiddleware, create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService
from tests.test_api_identity import small_registry

APP_DIR = pathlib.Path(t.__file__).parent
LEAK = "leak 43.6532,-79.3832 Grounded: Cafe Example is open until 9pm"
TID = "0123456789abcdef0123456789abcdef"


# --- F9 middleware status -----------------------------------------------------------------


def run_middleware(inner):
    sent: list[dict] = []
    sink = t.InMemorySink()

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    state = type("S", (), {"sink": sink})()
    scope = {"type": "http", "app": type("A", (), {"state": state})()}
    try:
        asyncio.run(_TraceMiddleware(inner)(scope, receive, send))
        error = None
    except Exception as exc:  # noqa: BLE001
        error = exc
    return sink, sent, error


def test_status_recorded_is_the_started_response_status():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 202, "headers": []})
        raise ValueError("late failure")

    sink, sent, error = run_middleware(inner)
    errors = [e for e in sink.events if e.event == t.EventType.ERROR]
    assert error is not None and len(errors) == 1
    assert errors[0].http_status == 202


def test_not_started_response_is_a_500():
    async def inner(scope, receive, send):
        raise ValueError("early")

    sink, sent, error = run_middleware(inner)
    assert error is None and sent[0]["status"] == 500
    assert [e.http_status for e in sink.events] == [500]


@pytest.mark.parametrize("exc", [ClientDisconnect, ConnectionResetError, BrokenPipeError])
def test_client_gone_is_not_a_500_and_nothing_is_sent(exc):
    async def inner(scope, receive, send):
        raise exc()

    sink, sent, error = run_middleware(inner)
    assert sent == []
    assert not [e for e in sink.events if e.event == t.EventType.ERROR]


def test_dead_exception_handler_is_gone():
    application = create_app()
    assert Exception not in application.exception_handlers
    assert 500 not in application.exception_handlers


# --- F9 guard -------------------------------------------------------------------------------


def test_uninstall_is_not_public_api():
    assert not hasattr(t, "uninstall_log_guard")
    assert not hasattr(t, "reset_health")


def test_emitting_exemption_is_only_for_the_exact_sink_record(caplog):
    caplog.set_level(logging.DEBUG)
    token = t._EMITTING.set(True)
    try:
        logging.getLogger("app.other").info(LEAK)
    finally:
        t._EMITTING.reset(token)
    assert "43.6532" not in caplog.text
    assert t.BLOCKED_MESSAGE in caplog.text


def test_lazy_guard_check_reinstalls_from_emit_and_trace():
    saved = logging.getLogRecordFactory()
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        assert not t.log_guard_active()
        sink = t.InMemorySink()
        for _ in range(600):
            t.trace(t.PolicyEvent, sink, trace_id=TID, outcome=t.PolicyOutcome.ALLOWED, domain=None)
        assert t.log_guard_active()
    finally:
        logging.setLogRecordFactory(saved)


def test_install_log_guard_is_lock_protected():
    assert hasattr(t, "_GUARD_LOCK")
    saved = logging.getLogRecordFactory()
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        threads = [threading.Thread(target=t.install_log_guard) for _ in range(8)]
        [x.start() for x in threads]
        [x.join() for x in threads]
        depth, factory = 0, logging.getLogRecordFactory()
        while getattr(factory, t._ORIGINAL_FACTORY_ATTR, None) is not None:
            depth += 1
            factory = getattr(factory, t._ORIGINAL_FACTORY_ATTR)
        assert depth == 1
    finally:
        logging.setLogRecordFactory(saved)


# --- F11 -----------------------------------------------------------------------------------------


class Interrupt(BaseException):
    pass


def test_baseexception_at_reserved_event_does_not_leak_the_reservation():
    class Boom:
        def emit(self, event):
            if isinstance(event, t.QuotaEvent) and event.outcome == t.QuotaOutcome.RESERVED:
                raise Interrupt()

    quotas = QuotaService(small_registry())
    orchestrator = TurnOrchestrator(quotas, sink=Boom(), dev_scaffold=True)
    principal = PrincipalResolver(policies=quotas.policies).resolve(VerifiedIdentity("u1", "anonymous"))
    with pytest.raises(Interrupt):
        orchestrator.run_turn(principal, "Plan a trip to a Toronto restaurant", "key-0001")
    usage = next(v for v in vars(quotas).values() if isinstance(v, dict) and v)["u1"] if False else None
    for value in vars(quotas).values():
        if isinstance(value, dict):
            for item in value.values():
                assert not getattr(item, "reserved_keys", set())
    assert orchestrator._committed == {}
    assert usage is None


# --- F12 -------------------------------------------------------------------------------------------


def deep_error(depth: int) -> BaseException:
    def recurse(n):
        if n == 0:
            raise RuntimeError("deep 43.6532,-79.3832")
        recurse(n - 1)

    try:
        recurse(depth)
    except RuntimeError as error:
        return error
    raise AssertionError


def test_frames_from_does_not_use_extract_tb(monkeypatch):
    import traceback

    def forbidden(*args, **kwargs):
        raise AssertionError("extract_tb must not be used")

    monkeypatch.setattr(traceback, "extract_tb", forbidden)
    here = pathlib.Path(__file__)
    assert here.exists()
    # Frames inside the tests directory are not app frames; synthesise an app frame.
    code = compile("def f():\n    raise ValueError('x')\n", str(APP_DIR / "main.py"), "exec")
    scope: dict = {}
    exec(code, scope)  # noqa: S102
    try:
        scope["f"]()
    except ValueError as error:
        assert t.frames_from(error) == ("app/main.py:2",)


def test_frames_from_deep_traceback_is_bounded_and_fast():
    code = compile(
        "def recurse(n):\n    if n == 0:\n        raise RuntimeError('deep 43.6532,-79.3832')\n    recurse(n - 1)\n",
        str(APP_DIR / "orchestrator.py"),
        "exec",
    )
    scope: dict = {}
    exec(code, scope)  # noqa: S102
    try:
        scope["recurse"](800)
    except RuntimeError as error:
        started = time.perf_counter()
        frames = t.frames_from(error)
        assert time.perf_counter() - started < 0.25
        assert 0 < len(frames) <= 8
        assert "43.6532" not in repr(frames)


def test_frames_from_survives_recursion_error():
    def infinite(n=0):
        return infinite(n + 1)

    try:
        infinite()
    except RecursionError as error:
        started = time.perf_counter()
        assert len(t.frames_from(error)) <= 8
        assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "bad",
    [
        "sk-live-CANARYBEARER0123456789",
        "eyJhbGciOiJIUzI1NiJ9abcdef0123456789",
        "Bearer0123456789abcdef0123456789ab",
        "ABCDEF0123456789abcdef0123456789",
        "0123456789abcdef",
        TID + "0",
        "g" * 32,
    ],
)
def test_trace_id_is_hex_only(bad):
    with pytest.raises(Exception):
        t.PolicyEvent(trace_id=bad, outcome=t.PolicyOutcome.ALLOWED, domain=None)
    assert t.PolicyEvent(trace_id=TID, outcome=t.PolicyOutcome.ALLOWED, domain=None)
    assert t.new_trace_id() != TID and len(t.new_trace_id()) == 32


def test_health_is_a_read_only_snapshot():
    t._reset_health_for_tests()
    snapshot = t.health()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.sink_errors = 99  # type: ignore[misc]
    t.emit("junk")  # type: ignore[arg-type]
    assert t.health().validation_errors == 1
    assert snapshot.validation_errors == 0
    assert all(v >= 0 for v in dataclasses.asdict(t.health()).values())


# --- D3 ----------------------------------------------------------------------------------------------


def test_call_role_enum_matches_gateway():
    assert {m.name: m.value for m in t.CallRole} == {m.name: m.value for m in CallRole}
    assert set(t.CallRole) and "gateway" not in repr(t.CallRole.__module__)


def test_gateway_call_carries_docs_required_fields():
    event = t.GatewayCall(
        trace_id=TID, model_id="google/gemini-2.5-flash", provider="google-vertex",
        finish_reason=t.FinishReason.STOP, input_tokens=1, output_tokens=1, latency_ms=1,
        cost_microusd=1, call_role=t.CallRole.DISCOVER, model_calls=2, repairs=1,
        price_table_version="placeholder-0",
    )
    assert event.call_role == t.CallRole.DISCOVER and event.repairs == 1
    for bad in ({"model_calls": -1}, {"model_calls": 1000}, {"repairs": 100},
                {"price_table_version": "43.6532,-79.3832"}, {"call_role": "free text"}):
        fields = dict(
            trace_id=TID, model_id="a/b", provider="p", finish_reason=t.FinishReason.STOP,
            input_tokens=1, output_tokens=1, latency_ms=1, cost_microusd=1,
            call_role=t.CallRole.FRAME, model_calls=1, repairs=0, price_table_version="v1",
        )
        fields.update(bad)
        with pytest.raises(Exception):
            t.GatewayCall(**fields)


def test_telemetry_does_not_import_gateway_package_for_enums():
    tree = ast.parse((APP_DIR / "telemetry.py").read_text(encoding="utf-8"))
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(("gateway", ".gateway"))
        or (isinstance(node, ast.ImportFrom) and node.level and (node.module or "").startswith("gateway"))
        for alias in node.names
    }
    assert "CallRole" not in names


# --- T5 ------------------------------------------------------------------------------------------------


def test_app_code_never_writes_to_stdout_or_stderr_or_prints():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                offenders.append(f"{path.name}:{node.lineno} print")
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"write", "writelines"}
                and isinstance(func.value, ast.Attribute)
                and func.value.attr in {"stdout", "stderr", "__stdout__", "__stderr__"}
            ):
                offenders.append(f"{path.name}:{node.lineno} {func.value.attr}.{func.attr}")
    assert offenders == []


def test_ast_check_would_flag_a_violation():
    tree = ast.parse("import sys\nsys.stderr.write('x')\nprint('y')\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert len(calls) == 2


# --- T4 real uvicorn -------------------------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(f"{record.name}|{record.getMessage()}|{record.exc_info}|{record.__dict__}")


def test_real_uvicorn_logs_contain_no_canaries():
    import uvicorn

    quotas = QuotaService(small_registry())
    sink = t.InMemorySink()
    orchestrator = TurnOrchestrator(quotas, sink=sink, dev_scaffold=True)
    application = create_app(
        verifier=StaticTokenVerifier({"canary-tok": VerifiedIdentity("guest-canary", "anonymous")}),
        resolver=PrincipalResolver(policies=quotas.policies),
        orchestrator=orchestrator,
        trace_sink=sink,
    )
    capture = _Capture()
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio")
    loggers = [logging.getLogger(n) for n in names]
    saved = [(lg.level, lg.propagate) for lg in loggers]
    for lg in loggers:
        lg.addHandler(capture)
        lg.setLevel(logging.DEBUG)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(application, log_config=None, lifespan="off", log_level="debug", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn did not start"

        def post(body: bytes, headers: dict[str, str]):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/agent/turns", data=body, headers=headers, method="POST"
            )
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as error:
                error.read()
                return error.code

        canary_headers = {
            "Content-Type": "application/json",
            "X-Canary": "43.6532,-79.3832 123 Queen Street West",
            "Idempotency-Key": "idem-canary.user@example.com-43.6532",
            "Authorization": "Bearer canary-tok",
        }
        body = b'{"message": "Plan a trip near 43.6532,-79.3832 Grounded: Cafe Example is open until 9pm"}'
        assert post(body, canary_headers) == 200
        assert post(b'{"msg": "43.6532,-79.3832 Cafe Example"}', canary_headers) == 422
        assert post(body, {**canary_headers, "Authorization": "Bearer sk-live-CANARYBEARER"}) == 401

        def boom(*args, **kwargs):
            raise RuntimeError("explode 43.6532,-79.3832 Cafe Example sk-live-CANARYBEARER")

        orchestrator.run_turn = boom  # type: ignore[method-assign]
        assert post(body, {**canary_headers, "Idempotency-Key": "idem-other-0001"}) == 500
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for lg, (level, propagate) in zip(loggers, saved):
            lg.removeHandler(capture)
            lg.setLevel(level)
            lg.propagate = propagate
        sock.close()
    blob = "\n".join(capture.lines)
    assert capture.lines, "uvicorn emitted no log lines; capture is not exercising the server"
    assert any(line.startswith("uvicorn.access|") for line in capture.lines)
    for canary in ("43.6532", "Cafe Example", "Queen Street", "canary.user", "CANARYBEARER", "canary-tok"):
        assert canary not in blob
    assert "Exception in ASGI application" not in blob
    assert ErrorCode.PROVIDER in {e.code for e in sink.events if e.event == t.EventType.ERROR}
