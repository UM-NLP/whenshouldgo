"""scripts/live_smoke.py with --location and --grant-location-consent (dry run, offline)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from maps_simulator import FakeMcpTransport

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_smoke.py"
_spec = importlib.util.spec_from_file_location("live_smoke_location", _PATH)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)  # type: ignore[union-attr]

MESSAGE = "something fun for the kids this morning"
COORDS = ("12.3456", "65.4321", "12.346", "65.432")


def run(argv: list[str]) -> tuple[int, str]:
    lines: list[str] = []
    code = smoke.main(argv, emit=lines.append, env={})
    return code, "\n".join(lines)


def test_a_located_dry_run_searches_and_prints_no_coordinates(capsys, tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    code, out = run(["--dry-run", "--message", MESSAGE, "--location", "12.3456,-65.4321",
                     "--grant-location-consent", "--json-out", str(target)])
    assert code == 0 and "location: sent (rounded)" in out
    assert "grounding_lite_calls=1" in out and "NEEDS_AREA" not in out
    raw = target.read_text(encoding="utf-8")
    assert json.loads(raw)["turns"][0]["location"] == "sent"
    for text in (out, raw, capsys.readouterr().out, capsys.readouterr().err):
        assert not any(c in text for c in COORDS)


def test_the_rounded_bias_is_what_goes_to_the_discovery_transport(monkeypatch) -> None:
    seen: list[FakeMcpTransport] = []
    original = smoke.dry_transports

    def spy(message: str, turns: int):
        model, maps = original(message, turns)
        seen.append(maps)
        return model, maps

    monkeypatch.setattr(smoke, "dry_transports", spy)
    code, _ = run(["--dry-run", "--message", MESSAGE, "--location", "12.3456,-65.4321",
                   "--grant-location-consent"])
    assert code == 0
    body = json.dumps(seen[0].requests[0].body)
    assert '"latitude": 12.346' in body and '"longitude": -65.432' in body
    assert "12.3456" not in body and "65.4321" not in body


def test_without_consent_the_located_turn_is_refused_and_still_prints_no_coordinates() -> None:
    code, out = run(["--dry-run", "--message", MESSAGE, "--location", "12.3456,-65.4321"])
    assert code == smoke.EXIT_FAILED_TURN and "LOCATION_CONSENT_REQUIRED" in out
    assert not any(c in out for c in COORDS)


def test_no_location_is_printed_as_none() -> None:
    code, out = run(["--dry-run"])
    assert code == 0 and "location: none" in out


@pytest.mark.parametrize("bad", ["abc", "12.3", "91,0", "0,181", "nan,1", "1,2,3", ""])
def test_a_bad_location_argument_is_a_usage_error_that_never_echoes_it(bad, capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        run(["--dry-run", "--location", bad])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert bad not in err or bad == ""
