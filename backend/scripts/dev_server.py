"""NON-PRODUCTION local dev server for the web client (a script, not part of the app package).

Run from ``backend/``. Offline (fake model and Maps simulator, no network, no keys):

    python scripts/dev_server.py --i-understand-this-is-dev --dry-run

Live (real model and Maps calls; reads ONLY the two key variables, like live_smoke.py):

    python scripts/dev_server.py --i-understand-this-is-dev --env-file C:\\path\\to\\.env --max-spend-usd 0.05

DEV ONLY. It accepts one fixed, public dev token as an identity (a guest), keeps every ledger in
process memory, binds to loopback only, and refuses to start without the explicit flag. It is not
imported by ``app`` and is never wired into ``create_production_app``: production stays fail-closed
with no verifier. There is no guest bootstrap route in ``app/main.py`` yet, so the web client's dev
auth provider simply sends the dev token.

Safety: keys go into a dict handed to ``build_runtime`` and are never printed, logged or put into
``os.environ``. Live mode enforces a spend cap: once the cost ledger total reaches it, every turn
is refused with a typed 429 before any model or Maps call. The cost of Grounding Lite is counted as
US$0 (its unit price is unconfirmed), exactly as in live_smoke.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
BACKEND = SCRIPTS.parent
for _path in (BACKEND, SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from starlette.responses import JSONResponse  # noqa: E402

import live_smoke  # noqa: E402  (reuse: env parsing, TLS modes, ledger totals; no duplicate)
from app.auth import InvalidToken, VerifiedIdentity  # noqa: E402
from app.composition import (  # noqa: E402
    GOOGLE_MAPS_KEY_VAR,
    OPENROUTER_KEY_VAR,
    ConfigurationError,
    Runtime,
    build_runtime,
)

DEV_TOKEN = "dev-only-public-token"  # not a credential: the web dev provider sends exactly this
DEV_UID = "dev-guest-0001"
DEFAULT_PORT = 8787
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
EXIT_OK, EXIT_USAGE = 0, 2
_CAPPED_PATH = "/v1/agent/turns"


class DevRefused(Exception):
    """A refusal to start; the message is static and never holds a key or a value."""


class DevVerifier:
    """DEV-ONLY IdentityVerifier: one fixed public token -> one anonymous (guest) identity."""

    def verify(self, token: str) -> VerifiedIdentity:
        if token != DEV_TOKEN:
            raise InvalidToken()
        return VerifiedIdentity(DEV_UID, "anonymous")


class SpendCap:
    """ASGI middleware: refuse a turn with a typed 429 once the ledger total is at the cap."""

    def __init__(self, app: Any, runtime_ref: Callable[[], Runtime | None], cap: Decimal) -> None:
        self.app, self._runtime, self._cap = app, runtime_ref, cap

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == _CAPPED_PATH and scope["method"] == "POST":
            runtime = self._runtime()
            if runtime is not None and live_smoke.ledger_totals(runtime)["cost"] >= self._cap:
                body = {"schema_version": 1, "kind": "error", "code": "QUOTA",
                        "message": "dev spend cap reached", "reason": "OVER_BUDGET"}
                await JSONResponse(body, status_code=429)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def dry_transports() -> tuple[Any, Any]:
    """Unlimited offline fakes: the model reads the user's message from the request body."""
    sys.path.insert(0, str(BACKEND / "tests"))
    from gateway_fakes import completion_response  # noqa: PLC0415
    from maps_simulator import FakeMcpTransport, mcp_ok, synthetic_payload  # noqa: PLC0415

    class DryModel:
        def send(self, request: Any, credential: Any) -> Any:
            message = str(request.body["messages"][-1]["content"])
            words = re.findall(r"[A-Za-z]+", message)
            search: dict[str, str] = {"text_query": " ".join(words[:6]) or "restaurant"}
            areas = re.findall(r"(?<=\s)[A-Z][a-z]+(?: [A-Z][a-z]+)*", message)
            if areas:  # no capitalised place: the NEEDS_AREA clarification (no Maps call)
                search["area"] = areas[0]
            answer = json.dumps({"intent": "DISCOVERY", "search": search})
            return completion_response(answer, prompt_tokens=250, completion_tokens=60,
                                       provider="DeepInfra")

    class DryMaps(FakeMcpTransport):
        def send(self, request: Any, credential: Any) -> Any:
            if not self._script:
                self._script.append(mcp_ok(synthetic_payload(places=3)))
            return super().send(request, credential)

    return DryModel(), DryMaps([])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python scripts/dev_server.py",
        description="NON-PRODUCTION local API server for the web client (loopback only).")
    p.add_argument("--i-understand-this-is-dev", action="store_true", dest="understood",
                   help="required: this server accepts a public dev token and keeps state in memory")
    p.add_argument("--dry-run", action="store_true",
                   help="fake model and Maps simulator; no network and no keys")
    p.add_argument("--env-file", default=None,
                   help=f"dotenv file; only {OPENROUTER_KEY_VAR} and {GOOGLE_MAPS_KEY_VAR} are read")
    p.add_argument("--ssl-mode", choices=("strict", "os-store-nonstrict"), default="strict")
    p.add_argument("--max-spend-usd", type=Decimal, default=Decimal("0.05"),
                   help="live mode: refuse turns once the ledger total reaches this")
    p.add_argument("--host", default="127.0.0.1", help="loopback addresses only")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    return p


def build_dev_app(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> tuple[Any, Runtime]:
    """The checks that decide whether to start, then the composed app. Raises ``DevRefused``."""
    if not args.understood:
        raise DevRefused("refusing to start: pass --i-understand-this-is-dev (NON-PRODUCTION server)")
    if args.host not in LOOPBACK_HOSTS:
        raise DevRefused("refusing to start: --host must be a loopback address")
    if not 1024 <= args.port <= 65535:
        raise DevRefused("refusing to start: --port must be between 1024 and 65535")
    if args.max_spend_usd <= 0:
        raise DevRefused("refusing to start: --max-spend-usd must be positive")
    names = (OPENROUTER_KEY_VAR, GOOGLE_MAPS_KEY_VAR)
    try:
        if args.dry_run:
            keys = dict(live_smoke._DRY_KEYS)
            model_transport, discovery_transport = dry_transports()
        else:
            try:
                keys = live_smoke.parse_env_file(args.env_file, names) if args.env_file else {}
                for name in names:  # fall back to the process environment for a missing variable
                    if not keys.get(name) and environ.get(name):
                        keys[name] = environ[name]
                model_transport, discovery_transport = live_smoke.live_transports(args.ssl_mode)
            except live_smoke.SmokeRefused as refusal:
                raise DevRefused(f"refusing to start: {refusal}") from None
        runtime = build_runtime(
            keys, identity_verifier=DevVerifier(), model_transport=model_transport,
            discovery_transport=discovery_transport, clock=lambda: datetime.now(UTC),
            allow_in_memory_state=True)
    except ConfigurationError as refusal:
        raise DevRefused(f"refusing to start: {refusal}") from None
    keys.clear()
    app: Any = runtime.app
    if not args.dry_run:  # the cap is the only guard on live spend; dry runs cost nothing
        app = SpendCap(app, lambda: runtime, args.max_spend_usd)
    return app, runtime


def main(
    argv: list[str] | None = None, *, emit: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None, serve: Callable[..., Any] | None = None,
) -> int:
    emit = emit or (lambda line: print(line, flush=True))
    args = build_parser().parse_args(argv)
    try:
        app, runtime = build_dev_app(args, os.environ if env is None else env)
    except DevRefused as refusal:
        emit(str(refusal))
        return EXIT_USAGE
    mode = "DRY RUN (fakes, no network)" if args.dry_run else f"LIVE ssl={args.ssl_mode}"
    emit("*** NON-PRODUCTION DEV SERVER: public dev token, in-memory state, loopback only ***")
    emit(f"{mode}; model={runtime.config.model_id}; listening on http://{args.host}:{args.port}")
    if not args.dry_run:
        emit(f"spend cap US${args.max_spend_usd} (grounding_lite counted as US$0)")
    if serve is None:
        import uvicorn  # noqa: PLC0415

        serve = uvicorn.run
    # access_log off: request lines must never reach a log; log_level warning keeps it quiet.
    serve(app, host=args.host, port=args.port, access_log=False, log_level="warning")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
