"""The production image must run only the fail-closed entry point and ship no secrets."""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DOCKERFILE = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = [
    line.strip()
    for line in (BACKEND / ".dockerignore").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
]
INSTRUCTIONS = [
    line for line in DOCKERFILE.splitlines() if line.strip() and not line.lstrip().startswith("#")
]


def _cmd() -> str:
    cmds = [line for line in INSTRUCTIONS if line.startswith(("CMD", "ENTRYPOINT"))]
    assert cmds, "Dockerfile needs a CMD"
    return "\n".join(cmds)


def test_cmd_runs_the_fail_closed_factory() -> None:
    cmd = _cmd()
    assert "app.server:create_production_app" in cmd
    assert "--factory" in cmd
    assert "${PORT" in cmd


def test_image_never_references_dev_or_smoke_entry_points() -> None:
    lowered = "\n".join(INSTRUCTIONS).lower()
    for banned in ("dev_server", "live_smoke", "create_app"):
        assert banned not in lowered


def test_image_runs_as_non_root() -> None:
    users = [line.split()[1] for line in INSTRUCTIONS if line.startswith("USER ")]
    assert users and users[-1] not in {"root", "0"}


def test_dockerignore_excludes_secrets_and_results() -> None:
    for required in (".env", ".env.*", "evals/frame_bakeoff/results/", "tests/"):
        assert required in DOCKERIGNORE, required


def test_dockerfile_copies_no_env_and_sets_no_key_like_env() -> None:
    assert not re.search(r"^(COPY|ADD)\b.*\.env", DOCKERFILE, re.MULTILINE)
    key_like = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
    for line in INSTRUCTIONS:
        if line.startswith(("ENV ", "ARG ")):
            names = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", line) or line.split()[1:2]
            for name in names:
                assert not key_like.search(name), line


def test_image_installs_the_auth_extra_production_needs() -> None:
    installs = [line for line in INSTRUCTIONS if "pip install" in line and "venv" in line]
    assert installs and all("auth" in line for line in installs)  # Firebase and App Check verifiers


def test_ci_installs_the_auth_extra_so_the_verifier_tests_run() -> None:
    ci = (BACKEND.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '".[dev,auth]"' in ci
