"""scripts/dev_server.py: refusal conditions and the dry-run app, fully offline."""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("dev_server_under_test", _SCRIPTS / "dev_server.py")
dev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev)  # type: ignore[union-attr]

FLAG = "--i-understand-this-is-dev"


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, list[str], list[Any]]:
    lines: list[str] = []
    served: list[Any] = []
    code = dev.main(argv, emit=lines.append, env=env if env is not None else {},
                    serve=lambda app, **kw: served.append((app, kw)))
    return code, lines, served


def test_refuses_without_the_explicit_flag() -> None:
    code, lines, served = run(["--dry-run"])
    assert code == 2 and served == []
    assert any(FLAG in line for line in lines)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
def test_refuses_a_non_loopback_host(host: str) -> None:
    code, _, served = run([FLAG, "--dry-run", "--host", host])
    assert code == 2 and served == []


@pytest.mark.parametrize("port", ["0", "80", "70000"])
def test_refuses_a_bad_port(port: str) -> None:
    assert run([FLAG, "--dry-run", "--port", port])[0] == 2


def test_refuses_a_non_positive_spend_cap() -> None:
    assert run([FLAG, "--dry-run", "--max-spend-usd", "0"])[0] == 2


def test_live_mode_without_keys_refuses_and_names_no_value() -> None:
    code, lines, served = run([FLAG])
    assert code == 2 and served == []
    assert "refusing to start" in "\n".join(lines)


def test_live_mode_with_an_unreadable_env_file_refuses() -> None:
    assert run([FLAG, "--env-file", str(_SCRIPTS / "no-such-file.env")])[0] == 2


def test_dry_run_starts_on_loopback_with_logging_off() -> None:
    code, lines, served = run([FLAG, "--dry-run"])
    assert code == 0 and len(served) == 1
    _, kw = served[0]
    assert kw["host"] == "127.0.0.1" and kw["access_log"] is False
    assert "NON-PRODUCTION" in lines[0]


def test_dry_run_app_accepts_only_the_dev_token_and_serves_a_turn() -> None:
    app, _ = dev.build_dev_app(dev.build_parser().parse_args([FLAG, "--dry-run"]), {})
    client = TestClient(app)
    key = {"Idempotency-Key": "dev-test-0001"}
    assert client.post("/v1/agent/turns", json={"message": "dinner in Paris"}, headers=key).status_code == 401
    bad = {**key, "Authorization": "Bearer nope"}
    assert client.post("/v1/agent/turns", json={"message": "dinner in Paris"}, headers=bad).status_code == 401
    ok = {**key, "Authorization": f"Bearer {dev.DEV_TOKEN}"}
    response = client.post("/v1/agent/turns", json={"message": "dinner in Paris"}, headers=ok)
    assert response.status_code == 200
    assert response.json()["discovery"]["places"]
    again = client.post("/v1/agent/turns", json={"message": "lunch near Rome"},
                        headers={**ok, "Idempotency-Key": "dev-test-0002"})
    assert again.status_code == 200  # the fake Maps transport never runs out


def test_dry_run_without_a_place_is_the_needs_area_clarification() -> None:
    app, _ = dev.build_dev_app(dev.build_parser().parse_args([FLAG, "--dry-run"]), {})
    headers = {"Authorization": f"Bearer {dev.DEV_TOKEN}", "Idempotency-Key": "dev-test-0003"}
    body = TestClient(app).post("/v1/agent/turns", json={"message": "dinner tonight"},
                                headers=headers).json()
    assert body["discovery"] is None and "NEEDS_AREA" in body["warnings"]


def test_spend_cap_refuses_a_turn_before_any_work(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        called = False

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        Fake.called = True

    class _Rt:
        pass

    capped = dev.SpendCap(inner, lambda: _Rt(), Decimal("0.01"))
    monkeypatch.setattr(dev.live_smoke, "ledger_totals", lambda runtime: {"cost": Decimal("0.02")})
    response = TestClient(capped).post("/v1/agent/turns", json={})
    assert response.status_code == 429 and response.json()["reason"] == "OVER_BUDGET"
    assert Fake.called is False


def test_production_app_stays_fail_closed_and_never_imports_the_dev_server() -> None:
    from app.main import create_app

    headers = {"Authorization": f"Bearer {dev.DEV_TOKEN}", "Idempotency-Key": "dev-test-0004"}
    response = TestClient(create_app()).post("/v1/agent/turns", json={"message": "hi"}, headers=headers)
    assert response.status_code == 401
    sources = "".join(p.read_text(encoding="utf-8") for p in (_SCRIPTS.parent / "app").rglob("*.py"))
    assert "dev_server" not in sources and dev.DEV_TOKEN not in sources
