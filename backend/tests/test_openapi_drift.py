"""Item 5: the checked-in web OpenAPI document equals a fresh export (CI drift check)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
CHECKED_IN = BACKEND.parent / "web" / "src" / "api" / "openapi.json"
SCRIPT = BACKEND / "scripts" / "export_openapi.py"


def _fresh(tmp_path: Path) -> Path:
    out = tmp_path / "openapi.json"
    done = subprocess.run([sys.executable, str(SCRIPT), str(out)], capture_output=True, text=True,
                          cwd=BACKEND)
    assert done.returncode == 0, done.stderr
    return out


def test_checked_in_openapi_equals_fresh_export(tmp_path: Path) -> None:
    fresh = _fresh(tmp_path)
    assert json.loads(CHECKED_IN.read_text("utf-8")) == json.loads(fresh.read_text("utf-8"))


def test_export_script_check_mode_for_ci() -> None:
    ok = subprocess.run([sys.executable, str(SCRIPT), "--check", str(CHECKED_IN)],
                        capture_output=True, text=True, cwd=BACKEND)
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run([sys.executable, str(SCRIPT), "--check", str(BACKEND / "pyproject.toml")],
                         capture_output=True, text=True, cwd=BACKEND)
    assert bad.returncode == 1
