"""scripts/live_smoke.py in --dry-run (and its refusals), fully offline."""
from __future__ import annotations

import importlib.util
import json
import os
import ssl
from pathlib import Path

import pytest
from composition_support import GM_CANARY, OR_CANARY
from gateway_fakes import FakeTransport
from maps_simulator import SYNTH_NAMES, synth_summary

from app.discovery import HttpxMcpTransport
from app.gateway import HttpxTransport
from app.gateway.openrouter import TransportResponse

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_smoke.py"
_spec = importlib.util.spec_from_file_location("live_smoke_under_test", _PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)  # type: ignore[union-attr]


def run(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    lines: list[str] = []
    code = smoke.main(argv, emit=lines.append, env=env if env is not None else {})
    return code, "\n".join(lines)


def test_dry_run_runs_end_to_end_offline() -> None:
    code, out = run(["--dry-run"])
    assert code == 0
    assert "HTTP 200" in out and "places=3" in out and "framing=" in out
    assert "grounding_lite_calls=1" in out and "kill_switch_tripped=False" in out
    assert "in=250 out=60" in out and "running total" in out
    assert "Synthetic Noodle Bar" in out  # titles are printed to the terminal


def test_output_has_no_secrets_headers_or_raw_text(capsys: pytest.CaptureFixture[str]) -> None:
    code = smoke.main(["--dry-run", "--turns", "2"])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == 0
    for forbidden in (smoke.SMOKE_TOKEN, "Authorization", "Bearer", *smoke._DRY_KEYS.values(),
                      OR_CANARY, GM_CANARY, "Synthetic fixture", "WORKSPACE_STATE"):
        assert forbidden not in text


@pytest.mark.parametrize("turns", ["0", "4", "10", "-1"])
def test_turns_are_bounded_to_three(turns: str) -> None:
    with pytest.raises(SystemExit) as caught:
        run(["--dry-run", "--turns", turns])
    assert caught.value.code == 2


def test_three_turns_are_allowed() -> None:
    code, out = run(["--dry-run", "--turns", "3"])
    assert code == 0 and out.count("HTTP 200") == 3


def test_the_spend_cap_aborts_before_a_turn() -> None:
    code, out = run(["--dry-run", "--turns", "3", "--max-spend-usd", "0.00001"])
    assert code == smoke.EXIT_SPEND_CAP
    assert out.count("HTTP 200") == 1 and "aborted before sending" in out


def test_json_out_is_sanitized(tmp_path: Path) -> None:
    target = tmp_path / "summary.json"
    message = "Quiet vegetarian dinner near Union Station tonight, MESSAGECANARY please"
    code, _ = run(["--dry-run", "--message", message, "--json-out", str(target)])
    assert code == 0
    raw = target.read_text(encoding="utf-8")
    data = json.loads(raw)
    for forbidden in ("MESSAGECANARY", "Union Station", "Synthetic fixture", *SYNTH_NAMES[:3],
                      synth_summary(3)[:20], "places/", "https://", "title"):
        assert forbidden not in raw
    assert len(data["message_sha256_16"]) == 16
    assert data["turns"][0]["discovery"]["place_count"] == 3 and data["exit_code"] == 0


def test_a_non_200_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(message: str, turns: int):
        down = TransportResponse(status_code=503, body=None)
        return FakeTransport([down] * 6), FakeTransport([])

    monkeypatch.setattr(smoke, "dry_transports", failing)
    code, out = run(["--dry-run"])
    assert code == smoke.EXIT_FAILED_TURN and "error code=" in out and "HTTP 200" not in out


def test_live_mode_refuses_without_keys_and_never_echoes_them(tmp_path: Path) -> None:
    code, out = run([])
    assert code == smoke.EXIT_USAGE and "OPENROUTER_API_KEY" in out
    env_file = tmp_path / ".env"
    env_file.write_text(f"OPENROUTER_API_KEY=bad CANARY-ENV-KEY\nGOOGLE_MAPS_API_KEY={GM_CANARY}\n"
                        "OTHER_SECRET=unrelated-CANARY\n", encoding="utf-8")
    code, out = run(["--env-file", str(env_file)])
    assert code == smoke.EXIT_USAGE and "CANARY" not in out
    code, _ = run(["--env-file", str(tmp_path / "missing.env")])
    assert code == smoke.EXIT_USAGE


def test_the_env_file_parser_reads_only_the_two_variables(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"# c\nexport OPENROUTER_API_KEY='{OR_CANARY}'\nGOOGLE_MAPS_API_KEY=\"{GM_CANARY}\"\n"
        "OTHER=nope\n", encoding="utf-8")
    parsed = smoke.parse_env_file(env_file, ("OPENROUTER_API_KEY", "GOOGLE_MAPS_API_KEY"))
    assert parsed == {"OPENROUTER_API_KEY": OR_CANARY, "GOOGLE_MAPS_API_KEY": GM_CANARY}
    assert OR_CANARY not in os.environ.values()


def test_ssl_modes_keep_verification_on() -> None:
    strict = smoke.build_ssl_context("strict")
    relaxed = smoke.build_ssl_context("os-store-nonstrict")
    for ctx in (strict, relaxed):
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert not relaxed.verify_flags & ssl.VERIFY_X509_STRICT
    assert smoke.live_transports("strict") == (None, None)
    model, discovery = smoke.live_transports("os-store-nonstrict")
    assert type(model) is HttpxTransport and type(discovery) is HttpxMcpTransport
    with pytest.raises(smoke.SmokeRefused):
        smoke.build_ssl_context("none")


def test_a_message_without_a_place_prints_the_needs_area_turn_as_a_normal_200() -> None:
    code, out = run(["--dry-run", "--message", "something fun for the kids"])
    assert code == 0 and "HTTP 200" in out and "warnings=['NEEDS_AREA']" in out
    assert "discovery:" not in out and "grounding_lite_calls=0" in out
