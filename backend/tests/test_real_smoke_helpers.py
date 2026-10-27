"""scripts/real_smoke.py: pure helpers and refusals only (no network, no credentials)."""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "real_smoke.py"
_spec = importlib.util.spec_from_file_location("real_smoke_under_test", _PATH)
rs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rs
_spec.loader.exec_module(rs)  # type: ignore[union-attr]


def run(argv, env=None):
    lines: list[str] = []
    code = rs.main(argv, emit=lines.append, env=env or {})
    return code, "\n".join(lines)


def test_parse_skip():
    assert rs.parse_skip(None) == frozenset()
    assert rs.parse_skip("turn, secrets") == {"turn", "secrets"}
    with pytest.raises(rs.SmokeRefused):
        rs.parse_skip("turn,everything")


def test_run_marker_is_unique_and_collection_safe():
    a, b = rs.run_marker(), rs.run_marker()
    assert a != b and a.isalnum() and a == a.lower()


def test_sanitize_detail_drops_free_text():
    clean = rs.sanitize_detail({"n": 3, "ok": True, "cost": "0.0001", "text": "a b\nsecret://x?k=1",
                                "obj": {"a": 1}, "long": "x" * 200})
    assert clean["n"] == 3 and clean["ok"] is True and clean["cost"] == "0.0001"
    assert clean["text"] == clean["obj"] == clean["long"] == "<redacted>"


def test_safe_error_is_class_name_only():
    class Boom(Exception):
        pass

    assert rs.safe_error(Boom("token=ya29.secret")) == "Boom"


def test_find_leaks_counts_without_content():
    doc = {"goal": "dinner", "when": __import__("datetime").datetime(2026, 1, 1, 12, 0, 0, 123456),
           "pos": "48.8584, 2.2945", "latitude": 1, "note": "Cafe Maps Verbatim Title"}
    found = rs.find_leaks(doc, ["Maps Verbatim Title", "zz"])
    assert found == {"maps_text": 1, "coordinate_pairs": 1, "coordinate_keys": 1}
    clean = rs.find_leaks({"goal": "x", "at": __import__("datetime").datetime.now()}, ["Title here"])
    assert clean == {"maps_text": 0, "coordinate_pairs": 0, "coordinate_keys": 0}


def test_usd_to_cad():
    assert rs.usd_to_cad(Decimal("0.01"), Decimal("1.43")) == Decimal("0.0143")


def test_project_refusals():
    assert rs.check_project("departure-method-test", False) == "departure-method-test"
    with pytest.raises(rs.SmokeRefused):
        rs.check_project("some-other-project", False)
    with pytest.raises(rs.SmokeRefused):
        rs.check_project("Bad Project!", True)
    assert rs.check_project("some-other-project", True) == "some-other-project"


def test_prefixed_client_prefixes_and_delegates():
    class Inner:
        def __init__(self):
            self.names = []

        def collection(self, name):
            self.names.append(name)
            return name

        def transaction(self, **kw):
            return "txn"

    inner = Inner()
    pc = rs.PrefixedClient(inner, "smkabc123_")
    assert pc.collection("workspaces") == "smkabc123_workspaces"
    assert pc.transaction(max_attempts=3) == "txn"
    assert getattr(pc, "transactional", None) is None  # the real client has none
    with pytest.raises(ValueError):
        rs.PrefixedClient(inner, "Bad Prefix")


def test_secret_url_is_the_global_resource_path():
    url = rs.secret_url("departure-method-test", "openrouter-api-key")
    assert url.startswith("https://secretmanager.googleapis.com/v1/projects/departure-method-test/")
    assert url.endswith("/secrets/openrouter-api-key/versions/latest:access")


def test_reporter_counts_failures_and_sanitizes():
    lines: list[str] = []
    rep = rs.Reporter(lines.append)
    rep.add("a", True, n=1)
    rep.add("b", False, error="Boom", body="has spaces\nand newline")
    rep.add("c", None)
    assert rep.failed == 1
    assert [r.status for r in rep.results] == ["PASS", "FAIL", "SKIP"]
    assert "newline" not in "\n".join(lines)


def test_refusals_offline(tmp_path):
    code, out = run(["--project", "other-project-xyz", "--skip", "firestore,identity,secrets,turn"])
    assert code == rs.EXIT_USAGE and "refused" in out
    code, out = run(["--skip", "bogus"])
    assert code == rs.EXIT_USAGE and "refused" in out
    code, out = run(["--skip", "identity,firestore,secrets"])  # turn needs the provider keys
    assert code == rs.EXIT_USAGE and "provider keys" in out
    with pytest.raises(SystemExit):
        run(["--max-spend-usd", "5"])
    with pytest.raises(SystemExit):
        run(["--max-spend-usd", "0"])
    with pytest.raises(SystemExit):
        run(["--ssl-mode", "off"])
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=a\nGOOGLE_MAPS_API_KEY=b\n")
    code, out = run(["--env-file", str(env), "--skip", "firestore,secrets"])  # no web key
    assert code == rs.EXIT_USAGE and "web API key" in out
