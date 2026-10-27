"""Review finding 6: production never starts with App Check silently off."""
from __future__ import annotations

import pytest
from composition_support import ENV, build

from app import server
from app.composition import ConfigurationError


def test_production_without_the_project_number_refuses_to_start() -> None:
    with pytest.raises(ConfigurationError, match="FIREBASE_PROJECT_NUMBER"):
        build(env={**ENV, "APP_CHECK_ENFORCE": "false"}, require_app_check=True)


def test_production_without_an_explicit_enforce_decision_refuses_to_start() -> None:
    with pytest.raises(ConfigurationError, match="APP_CHECK_ENFORCE"):
        build(env={**ENV, "FIREBASE_PROJECT_NUMBER": "123456789012"}, require_app_check=True)


@pytest.mark.parametrize("value", ["true", "false"])
def test_production_starts_with_an_explicit_decision(value: str) -> None:
    runtime = build(env={**ENV, "FIREBASE_PROJECT_NUMBER": "123456789012",
                         "APP_CHECK_ENFORCE": value}, require_app_check=True)
    policy = runtime.app.state.app_check_policy
    assert policy is not None and policy._enforce is (value == "true")  # noqa: SLF001


def test_development_builds_are_unchanged() -> None:
    assert build().app.state.app_check_policy is None


def test_the_production_entry_point_requires_app_check(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    class Done(Exception):
        pass

    def fake_build(env, **kwargs):
        seen.update(kwargs)
        raise Done

    monkeypatch.setattr(server, "build_runtime", fake_build)
    monkeypatch.setattr(server, "firebase_identity_from_env", lambda env, revocation=None: (object(), set()))
    with pytest.raises(Done):
        server.create_production_app()
    assert seen["require_app_check"] is True


def test_turns_from_an_unverified_client_get_the_tighter_window() -> None:
    from composition_support import HEADERS, MESSAGE
    from fastapi.testclient import TestClient

    from app.appcheck import AppCheckSignal

    runtime = build()
    state = runtime.app.state

    def monitor(request, header):
        request.state.app_check_signal = AppCheckSignal.ABSENT

    state.app_check_policy = monitor
    state.unverified_paid_limiter = type(state.unverified_paid_limiter)(max_operations=1)
    http = TestClient(runtime.app, raise_server_exceptions=False)
    first = http.post("/v1/agent/turns", json=MESSAGE, headers={**HEADERS, "Idempotency-Key": "k-1"})
    second = http.post("/v1/agent/turns", json=MESSAGE, headers={**HEADERS, "Idempotency-Key": "k-2"})
    assert first.status_code != 429
    assert second.status_code == 429 and second.json()["reason"] == "RATE_LIMITED"
