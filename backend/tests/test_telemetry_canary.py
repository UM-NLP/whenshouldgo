"""Log-canary: no request content may reach any log record, sink event, stdout or stderr."""
import logging
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import telemetry as t
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import PrincipalKind
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService
from tests.test_api_identity import small_registry

CANARIES = (
    "43.6532,-79.3832",
    "-79.3832",
    "123 Queen Street West",
    "M5V 2T6",
    "canary.user@example.com",
    "sk-live-CANARYBEARER0123456789",
    "AIzaSyFAKEKEYCANARY0123456789abcdef",
    "Grounded: Cafe Example is open until 9pm",
    "Cafe Example",
    "canary-tok-guest",
    "guest-canary",
)
TRAVEL_CANARY = (
    "Plan a trip to a Toronto restaurant near 43.6532,-79.3832 at 123 Queen Street West "
    "M5V 2T6 email canary.user@example.com key AIzaSyFAKEKEYCANARY0123456789abcdef "
    "Grounded: Cafe Example is open until 9pm"
)
OUT_OF_SCOPE_CANARY = "Write my python homework 43.6532,-79.3832 canary.user@example.com Cafe Example"
HEADERS = {
    "X-Canary": "43.6532,-79.3832 123 Queen Street West",
    "X-Api-Key": "AIzaSyFAKEKEYCANARY0123456789abcdef",
    "Cookie": "session=sk-live-CANARYBEARER0123456789",
}
IDEM = "idem-canary.user@example.com-43.6532,-79.3832"
TOKENS = {"canary-tok-guest": VerifiedIdentity("guest-canary", "anonymous")}


def build(sink):
    quotas = QuotaService(small_registry())
    orchestrator = TurnOrchestrator(quotas, sink=sink, runtime_config_version="rc-1", dev_scaffold=True)
    application = create_app(
        verifier=StaticTokenVerifier(TOKENS),
        resolver=PrincipalResolver(policies=quotas.policies),
        orchestrator=orchestrator,
        trace_sink=sink,
    )
    return TestClient(application, raise_server_exceptions=False), orchestrator


@pytest.fixture
def env():
    sink = t.InMemorySink()
    client, orchestrator = build(sink)
    return client, sink, orchestrator


def send(client, message, key=IDEM, token="canary-tok-guest", auth=None, body=None):
    headers = dict(HEADERS)
    headers["Idempotency-Key"] = key
    auth_value = auth if auth is not None else (f"Bearer {token}" if token else None)
    if auth_value:
        headers["Authorization"] = auth_value
    payload = body if body is not None else {"message": message}
    return client.post("/v1/agent/turns", json=payload, headers=headers)


def everything_logged(caplog, sink, capsys) -> str:
    out, err = capsys.readouterr()
    parts = [caplog.text, out, err, repr(sink.events)]
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(sorted(record.__dict__.items(), key=lambda kv: kv[0])))
    return "\n".join(str(p) for p in parts)


def assert_clean(blob: str) -> None:
    for canary in CANARIES:
        assert canary not in blob, f"canary leaked: {canary}"


def run_all_paths(client, orchestrator, monkeypatch):
    assert send(client, TRAVEL_CANARY, key="k-travel-" + IDEM).status_code == 200
    assert send(client, TRAVEL_CANARY, key="k-travel-" + IDEM).status_code == 200  # replay
    assert send(client, TRAVEL_CANARY, key="k-travel2-" + IDEM).status_code == 200
    exhausted = send(client, TRAVEL_CANARY, key="k-exhausted-" + IDEM)  # guest allowance is 2
    assert exhausted.status_code == 200 and exhausted.json()["allowance_exhausted"] is True
    assert send(client, OUT_OF_SCOPE_CANARY, key="k-oos-" + IDEM).status_code == 200
    conflict = send(client, TRAVEL_CANARY + " more", key="k-travel-" + IDEM)
    assert conflict.status_code == 409
    invalid = send(client, None, body={"message": TRAVEL_CANARY, "uid": "Cafe Example"})
    assert invalid.status_code == 422
    assert send(client, None, body={"msg": TRAVEL_CANARY}).status_code == 422
    assert send(client, TRAVEL_CANARY, token=None).status_code == 401
    bad_auth = send(client, TRAVEL_CANARY, auth="Bearer sk-live-CANARYBEARER0123456789")
    assert bad_auth.status_code == 401
    for response in (conflict, invalid, bad_auth):
        assert_clean(response.text)

    def boom(*args, **kwargs):
        raise RuntimeError("explode " + TRAVEL_CANARY + " Bearer sk-live-CANARYBEARER0123456789")

    monkeypatch.setattr(orchestrator, "run_turn", boom)
    failed = send(client, TRAVEL_CANARY, key="k-boom-" + IDEM)
    assert failed.status_code == 500
    assert failed.json() == {
        "schema_version": 1, "kind": "error", "code": "PROVIDER", "message": "internal error",
    }
    assert_clean(failed.text)


def test_no_canary_reaches_logs_sink_stdout_or_stderr(env, caplog, capsys, monkeypatch):
    client, sink, orchestrator = env
    for name in ("", "app", "uvicorn", "uvicorn.error", "fastapi", "starlette", "httpx"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    run_all_paths(client, orchestrator, monkeypatch)
    assert_clean(everything_logged(caplog, sink, capsys))
    kinds = {e.event for e in sink.events}
    assert {
        t.EventType.TURN_STARTED, t.EventType.TURN_FINISHED, t.EventType.QUOTA,
        t.EventType.POLICY, t.EventType.ERROR,
    } <= kinds
    errors = [e for e in sink.events if e.event == t.EventType.ERROR]
    assert any(e.exception_type == "RuntimeError" for e in errors)
    assert all(e.exception_type in (None, "RuntimeError") for e in errors)


def test_real_logging_sink_path_is_clean_too(caplog, capsys, monkeypatch):
    sink = t.LoggingSink()
    client, orchestrator = build(sink)
    caplog.set_level(logging.DEBUG)
    run_all_paths(client, orchestrator, monkeypatch)
    assert any(r.name == t.TRACE_LOGGER_NAME for r in caplog.records)
    assert_clean(everything_logged(caplog, t.InMemorySink(), capsys))


def test_unhandled_exception_is_answered_not_reraised_to_the_server(env, caplog, capsys):
    client, sink, orchestrator = env

    def boom(*args, **kwargs):
        raise ValueError("secret 43.6532,-79.3832")

    orchestrator.run_turn = boom  # type: ignore[method-assign]
    caplog.set_level(logging.DEBUG)
    strict = TestClient(client.app)  # raise_server_exceptions=True
    response = send(strict, "Plan a trip to a Toronto cafe")
    assert response.status_code == 500
    assert response.json() == {
        "schema_version": 1, "kind": "error", "code": "PROVIDER", "message": "internal error",
    }
    # Nothing is logged beyond typed trace events, and the only event is the typed error.
    app_records = [r for r in caplog.records if r.name.startswith("app")]
    assert [r for r in app_records if r.name != t.TRACE_LOGGER_NAME] == []
    assert all(r.name == "app.telemetry" for r in app_records)
    assert not [r for r in caplog.records if r.name.startswith(("uvicorn", "starlette", "fastapi"))]
    assert capsys.readouterr() == ("", "")
    errors = [e for e in sink.events if e.event == t.EventType.ERROR]
    assert len(errors) == 1
    assert errors[0].exception_type == "ValueError" and errors[0].http_status == 500
    assert "secret" not in repr(sink.events)


def test_trace_events_carry_no_identity_or_key(env):
    client, sink, _ = env
    send(client, "Plan a trip to a Toronto cafe")
    assert_clean(repr(sink.events))
    started = next(e for e in sink.events if e.event == t.EventType.TURN_STARTED)
    assert started.principal_kind == PrincipalKind.GUEST
    assert started.runtime_config_version == "rc-1"
    assert len({e.trace_id for e in sink.events}) == 1


# --- mutation-style: prove the assertion can fail and the guard blocks -----------------

LEAK = "leak 43.6532,-79.3832 Grounded: Cafe Example is open until 9pm"


def test_plain_logging_calls_in_app_namespace_are_blocked(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("app.orchestrator").info(LEAK)
    logging.getLogger("app.anything").error("bad %s", "43.6532,-79.3832", extra={"k": LEAK})
    try:
        raise ValueError(LEAK)
    except ValueError:
        logging.getLogger("app.anything").exception("failed " + LEAK)
    blob = caplog.text + repr([r.__dict__ for r in caplog.records])
    assert_clean(blob)
    assert t.BLOCKED_MESSAGE in caplog.text
    assert all(r.exc_info is None and not hasattr(r, "k") for r in caplog.records)


def test_root_logger_call_from_app_code_is_blocked(caplog):
    """Code located in the app package cannot dodge the guard by using the root logger.

    No file is written: a code object is compiled with a synthetic filename inside app/.
    """
    synthetic = str(pathlib.Path(t.__file__).parent / "synthetic_probe.py")
    code = compile("import logging\nlogging.warning(text)\n", synthetic, "exec")
    caplog.set_level(logging.DEBUG)
    exec(code, {"text": LEAK})  # noqa: S102 - test-only synthetic module
    assert_clean(caplog.text)
    assert t.BLOCKED_MESSAGE in caplog.text
    assert not (pathlib.Path(t.__file__).parent / "synthetic_probe.py").exists()


def test_guard_does_not_touch_non_app_loggers(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("thirdparty.lib").info("hello %s", "world")
    assert "hello world" in caplog.text


def test_mutation_unguarded_logging_is_caught_by_the_assertion(caplog):
    """With the guard removed the same call leaks, and the canary assertion fails."""
    t._uninstall_log_guard_for_tests()
    try:
        caplog.set_level(logging.DEBUG)
        logging.getLogger("app.orchestrator").info(LEAK)
        with pytest.raises(AssertionError, match="canary leaked"):
            assert_clean(caplog.text)
    finally:
        t.install_log_guard()
    caplog.clear()
    logging.getLogger("app.orchestrator").info(LEAK)
    assert_clean(caplog.text)


def test_mutation_print_would_be_caught(capsys):
    print(LEAK)
    out, _ = capsys.readouterr()
    with pytest.raises(AssertionError, match="canary leaked"):
        assert_clean(out)


# --- Maps-text canaries through the full wired pipeline ---------------------------------

MAPS_CANARIES = (
    "Grounded: Cafe Example is open until 9pm",
    "Cafe Example",
    "SYNTH-MAPS-CANARY-ADDRESS 99 Nowhere Road",
)


def test_maps_text_canaries_through_the_full_pipeline_reach_no_log_sink_or_stream(
    caplog, capsys, monkeypatch
):
    from maps_simulator import FakeMcpTransport, mcp_ok, mcp_status, synthetic_payload
    from test_gateway_support import make_config
    from wiring_support import (
        Env,
        ScriptedGateway,
        deep_text,
        frame_turn,
        real_client,
    )

    from app.orchestrator import discovery_cache_key

    summary = f"{MAPS_CANARIES[0]} near {MAPS_CANARIES[2]} [0] [1]."
    payload = synthetic_payload(text=summary, places=2)
    payload["places"][0]["attribution"]["title"] = "Cafe Example - Google Maps"
    for name in ("", "app", "uvicorn", "uvicorn.error", "fastapi", "starlette", "httpx"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config) for _ in range(4)], config)
    transport = FakeMcpTransport([mcp_ok(payload), mcp_status(403), mcp_ok(payload)])
    env = Env(gateway=gateway, config=config, discovery=lambda e: real_client(
        config, transport, e.sink, e.clock))
    ok = env.post(key="ok-" + IDEM)
    assert ok.status_code == 200 and MAPS_CANARIES[0] in ok.text  # shown to the user, only there
    assert env.post(key="ok-" + IDEM).status_code == 200  # replay
    assert env.post(key="denied-" + IDEM).status_code == 503  # tool failure path
    env.cache.delete_owner("guest-a")
    assert env.post(key="ok-" + IDEM).status_code == 409  # expired replay
    forged = env.post(key="again-" + IDEM)  # a third tool call, answered normally
    assert forged.status_code == 200

    def boom(*args, **kwargs):
        raise RuntimeError("explode " + summary)

    monkeypatch.setattr(env.orchestrator, "run_turn", boom)
    assert env.post(key="boom-" + IDEM).status_code == 500
    logged = "\n".join([
        caplog.text, capsys.readouterr().out, capsys.readouterr().err, repr(env.sink.events),
        deep_text(env.sink.events), deep_text(env.orchestrator.workspaces),
        deep_text(env.orchestrator._committed), deep_text(env.costs.entries()),
        *(r.getMessage() for r in caplog.records),
        *(repr(sorted(r.__dict__.items(), key=lambda kv: kv[0])) for r in caplog.records),
    ])
    for canary in MAPS_CANARIES:
        assert canary not in logged, f"Maps canary leaked: {canary}"
    assert discovery_cache_key("x")  # the cache key is a hash, never the idempotency key
    assert IDEM not in discovery_cache_key(IDEM)
