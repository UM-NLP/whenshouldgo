"""Live smoke test of the production composition root (a script, not part of the app package).

Run from ``backend/``. Offline (no network, no keys):

    python scripts/live_smoke.py --dry-run

Live (a few cents at most; reads ONLY the two key variables from the env file):

    python scripts/live_smoke.py --env-file C:\\path\\to\\.env --turns 1 --max-spend-usd 0.02
    python scripts/live_smoke.py --env-file C:\\path\\to\\.env --ssl-mode os-store-nonstrict

SMOKE-ONLY identity: a trivial verifier accepts one fixed fake token and returns a guest
principal. It is not a production verifier (there is none yet) and must never leave this script.

Safety: keys are parsed into a dict handed to ``build_runtime`` and never printed, logged or put
into ``os.environ``. The script prints no Authorization header, key, raw model reply or raw Maps
text. It prints place TITLES (their attribution) to the terminal only; ``--json-out`` is
sanitized (no titles, no Maps text, the message only as a hash). TLS verification is never
disabled: ``os-store-nonstrict`` keeps CERT_REQUIRED and check_hostname and only clears OpenSSL's
VERIFY_X509_STRICT flag, for networks with TLS inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import InvalidToken, VerifiedIdentity  # noqa: E402
from app.composition import (  # noqa: E402
    GOOGLE_MAPS_KEY_VAR,
    OPENROUTER_KEY_VAR,
    ConfigurationError,
    Runtime,
    build_runtime,
)
from app.discovery import HttpxMcpTransport  # noqa: E402
from app.gateway import HttpxTransport  # noqa: E402

DEFAULT_MESSAGE = "I want a quiet vegetarian place for dinner near Union Station tonight."
MAX_TURNS = 3
SMOKE_TOKEN = "smoke-only-fixed-fake-token"  # not a credential: accepted only by this script
EXIT_OK, EXIT_FAILED_TURN, EXIT_USAGE, EXIT_SPEND_CAP = 0, 1, 2, 3
_DRY_KEYS = {OPENROUTER_KEY_VAR: "sk-dryrun-not-a-real-key-0000",
             GOOGLE_MAPS_KEY_VAR: "AIzaDryRunNotARealKey0000000000"}


class SmokeRefused(Exception):
    """A configuration problem; the message is static and never holds a key or the message."""


class SmokeVerifier:
    """Smoke-only IdentityVerifier: one fixed fake token -> one anonymous (guest) identity."""

    def verify(self, token: str) -> VerifiedIdentity:
        if token != SMOKE_TOKEN:
            raise InvalidToken()
        return VerifiedIdentity("smoke-guest-0001", "anonymous")


# --- keys and TLS --------------------------------------------------------------------------
def parse_env_file(path: str | Path, names: tuple[str, ...]) -> dict[str, str]:
    """Only the named variables from a dotenv-style file; every other line is ignored."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise SmokeRefused("cannot read the --env-file") from None
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, rest = line.partition("=")
        key = key.strip()
        if sep and key in names:
            rest = rest.strip()
            if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
                rest = rest[1:-1]
            found[key] = rest.strip()
    return found


def build_ssl_context(mode: str) -> ssl.SSLContext:
    if mode not in ("strict", "os-store-nonstrict"):
        raise SmokeRefused("unknown --ssl-mode")
    ctx = ssl.create_default_context()
    if mode == "os-store-nonstrict":
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise SmokeRefused("TLS verification must stay on")
    return ctx


def live_transports(mode: str) -> tuple[Any, Any]:
    """(model transport, discovery transport); None means the locked-down default. The relaxed
    context is injected ONLY through the two ``for_tests`` seams."""
    ctx = build_ssl_context(mode)
    if mode == "strict":
        return None, None
    return (HttpxTransport.for_tests(httpx.HTTPTransport(verify=ctx)),
            HttpxMcpTransport.for_tests(httpx.HTTPTransport(verify=ctx)))


# --- dry run (offline fakes from the test support modules) -----------------------------------
def dry_transports(message: str, turns: int) -> tuple[Any, Any]:
    sys.path.insert(0, str(BACKEND / "tests"))
    from gateway_fakes import completion_response  # noqa: PLC0415
    from maps_simulator import FakeMcpTransport, mcp_ok, synthetic_payload  # noqa: PLC0415

    words = re.findall(r"[A-Za-z]+", message)
    answer = json.dumps({"intent": "DISCOVERY",
                         "search": {"text_query": " ".join(words[:6]) or "restaurant"}})

    class DryModel:
        def send(self, request: Any, credential: Any) -> Any:
            return completion_response(answer, prompt_tokens=250, completion_tokens=60,
                                       provider="DeepInfra")

    maps = FakeMcpTransport([mcp_ok(synthetic_payload(places=3)) for _ in range(turns)])
    return DryModel(), maps


# --- measuring -------------------------------------------------------------------------------
def ledger_totals(runtime: Runtime) -> dict[str, Any]:
    entries = runtime.costs.entries()
    cost = sum((e.estimated_cost_usd or Decimal(0) for e in entries if e.kind == "gateway"),
               Decimal(0))
    return {
        "cost": cost,
        "input": sum(e.input_tokens for e in entries),
        "output": sum(e.output_tokens for e in entries),
        "tool_calls": sum(e.tool_calls for e in entries if e.kind == "tool"),
        "unpriced": sum(1 for e in entries if e.kind == "gateway" and e.estimated_cost_usd is None),
    }


def _json_body(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python scripts/live_smoke.py",
        description="Run a few turns through the production composition root.")
    p.add_argument("--env-file", default=None,
                   help=f"dotenv file; only {OPENROUTER_KEY_VAR} and {GOOGLE_MAPS_KEY_VAR} are read")
    p.add_argument("--message", default=DEFAULT_MESSAGE)
    p.add_argument("--turns", type=int, default=1, help=f"1 to {MAX_TURNS} (hard maximum)")
    p.add_argument("--ssl-mode", choices=("strict", "os-store-nonstrict"), default="strict")
    p.add_argument("--max-spend-usd", type=Decimal, default=Decimal("0.02"),
                   help="abort before a turn when the ledger total is at or above this")
    p.add_argument("--json-out", default=None, help="sanitized summary path (keep out of Git)")
    p.add_argument("--cad-rate", type=Decimal, default=Decimal("1.43"))
    p.add_argument("--dry-run", action="store_true",
                   help="fake model transport and Maps simulator; no network and no keys")
    return p


def main(
    argv: list[str] | None = None, *, emit: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    if emit is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    emit = emit or (lambda line: print(line, flush=True))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.turns <= MAX_TURNS:
        parser.error(f"--turns must be between 1 and {MAX_TURNS}")
    if args.max_spend_usd <= 0:
        parser.error("--max-spend-usd must be positive")
    try:
        return _run(args, emit, os.environ if env is None else env)
    except (SmokeRefused, ConfigurationError) as refusal:
        emit(f"refused: {refusal}")
        return EXIT_USAGE


def _run(args: argparse.Namespace, emit: Callable[[str], None], environ: Mapping[str, str]) -> int:
    names = (OPENROUTER_KEY_VAR, GOOGLE_MAPS_KEY_VAR)
    if args.dry_run:
        keys = dict(_DRY_KEYS)
        model_transport, discovery_transport = dry_transports(args.message, args.turns)
    else:
        keys = parse_env_file(args.env_file, names) if args.env_file else {}
        for name in names:  # fall back to the process environment for a missing variable only
            if not keys.get(name) and environ.get(name):
                keys[name] = environ[name]
        model_transport, discovery_transport = live_transports(args.ssl_mode)
    runtime = build_runtime(
        keys, identity_verifier=SmokeVerifier(), model_transport=model_transport,
        discovery_transport=discovery_transport, clock=lambda: datetime.now(UTC),
        allow_in_memory_state=True)
    keys.clear()
    client = TestClient(runtime.app, raise_server_exceptions=False)
    cap = args.max_spend_usd
    mode = "DRY RUN (fakes, no network)" if args.dry_run else f"LIVE ssl={args.ssl_mode}"
    emit(f"{mode}; model={runtime.config.model_id} provider={runtime.config.provider} "
         f"prices={runtime.price_table.version}; turns={args.turns}; cap=US${cap}")
    emit("grounding_lite unit price is unconfirmed and counted as US$0 (model cost only)")
    message_hash = hashlib.sha256(args.message.encode("utf-8")).hexdigest()[:16]
    summary: dict[str, Any] = {"mode": "dry-run" if args.dry_run else "live",
                               "ssl_mode": None if args.dry_run else args.ssl_mode,
                               "model": runtime.config.model_id,
                               "provider": runtime.config.provider,
                               "price_table_version": runtime.price_table.version,
                               "message_sha256_16": message_hash, "turns": []}
    exit_code = EXIT_OK
    for number in range(1, args.turns + 1):
        before = ledger_totals(runtime)
        if before["cost"] >= cap:
            emit(f"turn {number}: aborted before sending, total US${before['cost']:.6f} "
                 f"is at or above the cap US${cap}")
            summary["aborted"] = "SPEND_CAP"
            exit_code = EXIT_SPEND_CAP
            break
        started = time.perf_counter()
        try:
            response = client.post(
                "/v1/agent/turns", json={"message": args.message},
                headers={"Authorization": f"Bearer {SMOKE_TOKEN}",
                         "Idempotency-Key": f"smoke-{os.getpid()}-{number}-{int(time.time())}"})
            status, body = response.status_code, _json_body(response)
        except Exception as error:  # noqa: BLE001 - class name only, never its text
            status, body = 0, {"code": type(error).__name__}
        elapsed = time.perf_counter() - started
        after = ledger_totals(runtime)
        d_cost = after["cost"] - before["cost"]
        record: dict[str, Any] = {
            "turn": number, "http_status": status, "elapsed_seconds": round(elapsed, 2),
            "warnings": list(body.get("warnings") or []),
            "error_code": body.get("code") if status != 200 else None,
            "error_reason": body.get("reason") if status != 200 else None,
            "input_tokens": after["input"] - before["input"],
            "output_tokens": after["output"] - before["output"],
            "cost_usd": str(d_cost), "cost_cad": str(d_cost * args.cad_rate),
            "tool_calls": after["tool_calls"] - before["tool_calls"],
            "unpriced_gateway_entries": after["unpriced"] - before["unpriced"],
            "kill_switch_tripped": runtime.kill_switch.tripped,
            "quota_latch_tripped": runtime.quota_latch.tripped,
            "discovery": None,
        }
        titles: list[str] = []
        block = body.get("discovery") if status == 200 else None
        if isinstance(block, dict):
            places = block.get("places") or []
            record["discovery"] = {
                "place_count": len(places), "text_chars": len(block.get("text") or ""),
                "framing": block.get("framing"), "cache_expires_at": block.get("cache_expires_at")}
            titles = [str(p.get("title", "")) for p in places if isinstance(p, dict)]
        emit(f"turn {number}: HTTP {status}; {elapsed:.2f}s; warnings={record['warnings']}")
        if status != 200:
            emit(f"  error code={record['error_code']} reason={record['error_reason']}")
            exit_code = EXIT_FAILED_TURN
        if record["discovery"]:
            d = record["discovery"]
            emit(f"  discovery: places={d['place_count']} text_chars={d['text_chars']} "
                 f"framing={d['framing']} cache_expires_at={d['cache_expires_at']}")
            for title in titles:  # terminal only: attribution, never written to disk
                emit(f"    - {title}")
        emit(f"  model: in={record['input_tokens']} out={record['output_tokens']} "
             f"cost US${d_cost:.6f} = CA${d_cost * args.cad_rate:.6f}"
             + (f" ({record['unpriced_gateway_entries']} unpriced)"
                if record["unpriced_gateway_entries"] else ""))
        emit(f"  grounding_lite_calls={record['tool_calls']} "
             f"kill_switch_tripped={record['kill_switch_tripped']} "
             f"quota_latch_tripped={record['quota_latch_tripped']}")
        emit(f"  running total US${after['cost']:.6f} of cap US${cap}")
        summary["turns"].append(record)
        if status != 200:
            break
    total = ledger_totals(runtime)
    summary["total_cost_usd"] = str(total["cost"])
    summary["exit_code"] = exit_code
    emit(f"done: total US${total['cost']:.6f} = CA${total['cost'] * args.cad_rate:.6f}; "
         f"exit {exit_code}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        emit(f"summary: {args.json_out}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
