"""FRAME model bake-off harness: run candidate model(s) through OUR real gateway stack.

Composes RuntimeConfig (provider pinned, privacy settings), a non-placeholder PriceTable from the
CLI prices, OpenRouterGateway over HttpxTransport and ResilientGateway(max_attempts=1), then for
each (fixture, repeat) builds the request with ``frame.build_frame_request`` on a fresh
TripWorkspace and records ONE JSON line of metrics. Synthetic fixtures only; the model never
sees Maps content. Run it from ``backend/``:

    python -m evals.frame_bakeoff.run --dry-run --out evals/frame_bakeoff/results/dry.jsonl

The API key comes from OPENROUTER_API_KEY or --env-file (only that variable is read) and is
never printed or written. See README.md for cost math and safety rules.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import ssl
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

if __package__ in (None, ""):  # `python evals/frame_bakeoff/run.py` from backend/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.domain import (  # noqa: E402
    PhaseFlags,
    RuntimeConfig,
    RuntimeTTLs,
    TokenCeilings,
    TripWorkspace,
)
from app.frame import (  # noqa: E402
    ArgumentRejected,
    FrameProposal,
    build_frame_request,
    build_patch,
    plan_search,
    prompt_version_hashes,
)
from app.gateway import (  # noqa: E402
    GatewayPolicy,
    HttpxTransport,
    OpenRouterGateway,
    ResilientGateway,
    RunBudget,
)
from app.gateway.errors import (  # noqa: E402
    ContentFilterError,
    GatewayError,
    MalformedOutputError,
    TruncationError,
)
from app.gateway.policy import ResiliencePolicy  # noqa: E402
from app.gateway.pricing import ModelPrice, PriceTable  # noqa: E402
from app.gateway.types import FinishReason, Usage  # noqa: E402
from app.patching import PatchRejected, apply_patch_checked  # noqa: E402
from evals.frame_bakeoff import fixtures as fixture_lib  # noqa: E402
from evals.frame_bakeoff import score as score_lib  # noqa: E402
from evals.frame_bakeoff.dryrun import DryRunModel  # noqa: E402

KEY_VAR = "OPENROUTER_API_KEY"
DRY_RUN_KEY = "sk-dryrun-not-a-real-key-0000"  # never a credential; the mock transport ignores it
DRY_MODEL, DRY_PROVIDER = "dryrun/scripted-model-1", "dryrun"
DRY_PRICE_IN, DRY_PRICE_OUT = Decimal("0.10"), Decimal("0.40")
_KEY_SHAPE = re.compile(r"^[\x21-\x7e]+$")
EXIT_OK, EXIT_USAGE, EXIT_SPEND_CAP, EXIT_MAX_CALLS, EXIT_INTERRUPTED = 0, 2, 3, 4, 130
WORKSPACE_ID, OWNER_ID = "ws-bakeoff-0001", "bakeoff-user-0001"


class RunRefused(Exception):
    """A configuration problem. The message is static and never holds a key or a message."""


# --- key and TLS ------------------------------------------------------------------------
def parse_env_file(path: str | Path, name: str = KEY_VAR) -> str | None:
    """Value of ``name`` in a dotenv-style file; every other line is ignored and never kept."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise RunRefused("cannot read the --env-file") from None
    value: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, rest = line.partition("=")
        if sep and key.strip() == name:
            rest = rest.strip()
            if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
                rest = rest[1:-1]
            value = rest.strip()
    return value


def load_key(env: dict[str, str] | None = None, env_file: str | None = None) -> str:
    """The API key, or ``RunRefused`` (static text). Never printed by this module."""
    env = os.environ if env is None else env
    key = parse_env_file(env_file) if env_file else None
    if not key:
        key = env.get(KEY_VAR, "").strip()
    if not key:
        raise RunRefused(f"{KEY_VAR} is not set (environment or --env-file); refusing to run")
    if not _KEY_SHAPE.fullmatch(key):
        raise RunRefused(f"{KEY_VAR} is malformed; refusing to run")
    return key


def build_ssl_context(mode: str) -> ssl.SSLContext:
    """``strict``: the platform default. ``os-store-nonstrict``: the same OS trust store with the
    X509 strict flag relaxed, for a corporate TLS inspection proxy. Both keep chain and hostname
    verification (CERT_REQUIRED, check_hostname)."""
    if mode not in ("strict", "os-store-nonstrict"):
        raise RunRefused("unknown --ssl-mode")
    ctx = ssl.create_default_context()
    if mode == "os-store-nonstrict":
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise RunRefused("TLS verification must stay on")
    return ctx


def build_transport(mode: str) -> HttpxTransport:
    """strict uses the gateway's own locked-down client. The non-strict context is injected ONLY
    through ``HttpxTransport.for_tests`` (the single sanctioned seam)."""
    ctx = build_ssl_context(mode)
    if mode == "strict":
        return HttpxTransport()
    return HttpxTransport.for_tests(httpx.HTTPTransport(verify=ctx))


# --- stack ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Stack:
    config: RuntimeConfig
    prices: PriceTable
    gateway: ResilientGateway


def build_stack(
    model: str, provider: str, price_in: Decimal, price_out: Decimal, key: str,
    transport: HttpxTransport,
) -> Stack:
    """The real stack, exactly as the live probe composes it."""
    prices = PriceTable(version="bakeoff-prices-1", placeholder=False, model_prices=(
        ModelPrice(model_id=model, provider=provider, input_usd_per_million=price_in,
                   output_usd_per_million=price_out),))
    config = RuntimeConfig(
        version="bakeoff-cfg", model_id=model, provider=provider, provider_allowlist=(provider,),
        price_table_version=prices.version, prompt_version_hashes=prompt_version_hashes(),
        tool_schema_versions=(), classifier_version="c1", policy_version="p1",
        entitlement_version="e1", token_ceilings=TokenCeilings(), ttls=RuntimeTTLs(),
        flags=PhaseFlags())
    policy = GatewayPolicy(tool_allowlist=("search_places",))
    inner = OpenRouterGateway(transport, lambda: key, config, policy, prices)
    gateway = ResilientGateway(inner, ResiliencePolicy(max_attempts=1), price_table=prices)
    return Stack(config, prices, gateway)


# --- totals and reporting ---------------------------------------------------------------
@dataclass
class Totals:
    records: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal(0))

    def add(self, record: dict[str, Any]) -> None:
        self.records += 1
        self.model_calls += int(record.get("model_calls") or 0)
        self.input_tokens += int(record.get("input_tokens") or 0)
        self.output_tokens += int(record.get("output_tokens") or 0)
        self.cost_usd += Decimal(str(record.get("cost_usd") or "0"))


def cost_line(totals: Totals, cad_rate: float, done: int, planned: int) -> str:
    cad = totals.cost_usd * Decimal(str(cad_rate))
    return (f"[{done}/{planned}] model_calls={totals.model_calls} "
            f"tokens in={totals.input_tokens} out={totals.output_tokens} "
            f"cost US${totals.cost_usd:.6f} = CA${cad:.6f}")


# --- one call ---------------------------------------------------------------------------
TURN_OUTCOMES = ("DISCOVERY_SEARCH", "CLARIFY_OTHER", "CLARIFY_QUERY_REJECTED",
                 "CLARIFY_NEEDS_AREA", "PATCH_REJECTED", "MALFORMED", "PROVIDER_ERROR")
_CONTENT_FAILURES = (MalformedOutputError, TruncationError, ContentFilterError)


def replay_production(proposal: FrameProposal, message: str, fixture: dict[str, Any]) -> dict[str, Any]:
    """What production does with a proposal, offline and with the real functions, in the
    pipeline's order: plan_search (discovery only; an OTHER search is ignored; an accepted plan
    with no usable area is the NEEDS_AREA clarification, no search), then build_patch and
    apply_patch_checked on a fresh workspace. Returns plan, patch, requirements and the
    derived ``turn_outcome``. Fact VALUES are never recorded, only keys."""
    plan: dict[str, Any] = {"outcome": "none", "area_dropped": None, "arguments": None,
                            "has_area": None}
    plan_ok = False
    searches = False  # a search would actually run: an accepted plan WITH a usable area
    if proposal.intent.value == "DISCOVERY":
        if proposal.search is None:
            plan["outcome"] = "rejected"
        else:
            try:
                made = plan_search(proposal.search, message)
                plan = {"outcome": "accepted", "area_dropped": made.area_dropped,
                        "arguments": made.arguments, "has_area": made.has_area}
                plan_ok = True
                searches = made.has_area
            except ArgumentRejected:
                plan["outcome"] = "rejected"
    elif proposal.search is not None:
        plan["outcome"] = "ignored"
    base = TripWorkspace(id=WORKSPACE_ID, owner_id=OWNER_ID)
    patch: dict[str, Any] = {"outcome": "applied", "reason": None, "facts_dropped": 0,
                             "fact_keys": [f.key for f in proposal.facts]}
    try:
        built = build_patch(base, proposal, search_ok=searches)
        _, dropped = apply_patch_checked(base, built)
        patch["facts_dropped"] = dropped
    except PatchRejected as rejected:
        patch.update(outcome="rejected", reason=str(getattr(rejected.reason, "value", rejected.reason)))
    except Exception:  # noqa: BLE001 - a Fact the server cannot build is a rejected patch
        patch.update(outcome="rejected", reason="SCHEMA")
    if patch["outcome"] == "rejected":
        turn = "PATCH_REJECTED"
    elif proposal.intent.value == "OTHER":
        turn = "CLARIFY_OTHER"
    elif searches:
        turn = "DISCOVERY_SEARCH"
    else:
        turn = "CLARIFY_NEEDS_AREA" if plan_ok else "CLARIFY_QUERY_REJECTED"
    requirements: dict[str, Any] | None = None
    if turn == "DISCOVERY_SEARCH":
        query = plan["arguments"]["text_query"]
        raw_area = proposal.search.area if proposal.search else None
        requirements = {
            "required_words": all(any(score_lib.has_word(query, w) for w in g)
                                  for g in fixture["required_query_words"]),
            "forbidden_words": not any(score_lib.has_word(query, w)
                                       for w in fixture["forbidden_query_words"]),
            "area_in_message": None if not raw_area
            else score_lib.area_in_message(raw_area, message),
            "area_expected": None if fixture["expected_area"] is None
            else score_lib.area_matches(raw_area, fixture["expected_area"]),
            "area_absent_ok": not (("any_area" in fixture["must_not"]) and raw_area),
        }
    return {"plan": plan, "patch": patch, "requirements": requirements, "turn_outcome": turn}


def run_one(stack: Stack, fixture: dict[str, Any], repeat: int) -> dict[str, Any]:
    """One FRAME call on a fresh workspace -> one result record (synthetic data only; the prompt
    text is never stored, only the fixture id)."""
    message = fixture["message"]
    config = stack.config
    workspace = TripWorkspace(id=WORKSPACE_ID, owner_id=OWNER_ID)
    budget = RunBudget.from_ceilings(config.token_ceilings)
    record: dict[str, Any] = {
        "fixture_id": fixture["id"], "category": fixture["category"], "repeat": repeat,
        "model": config.model_id, "provider": config.provider,
        "prompt_sha256": prompt_version_hashes()[0].sha256[:16],
        "outcome": None, "error_kind": None, "error_code": None, "model_calls": 0, "repairs": 0,
        "latency_ms": 0.0, "input_tokens": 0, "output_tokens": 0, "cost_usd": "0",
        "cost_basis": "none", "finish_reason": None, "intent": None,
        "search": None, "plan": None, "patch": None, "requirements": None,
        "turn_outcome": None, "served_provider": None,
    }
    try:
        request = build_frame_request(message, workspace, config, budget)
    except Exception:  # noqa: BLE001
        record.update(outcome="REQUEST_REJECTED", turn_outcome="PROVIDER_ERROR")
        return record
    started = time.perf_counter()
    try:
        turn = stack.gateway.generate(request)
    except GatewayError as error:
        record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        usage: Usage | None = error.usage
        basis = "reported"
        if usage is None and error.calls > 0 and not error.rejected_before_processing:
            usage, basis = request.worst_case_usage, "worst_case"  # never lose a charge
        if usage is None:
            usage, basis = Usage(input_tokens=0, output_tokens=0), "none"
        cost = stack.prices.estimate(config.model_id, config.provider, usage)
        outcome = ("VALIDATION_FAILED" if isinstance(error, MalformedOutputError)
                   else "TRUNCATED" if isinstance(error, TruncationError) else "GATEWAY_ERROR")
        record.update(
            outcome=outcome, error_kind=error.kind.value, error_code=str(error.code.value),
            model_calls=error.calls, input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens, cost_usd=str(cost), cost_basis=basis,
            finish_reason=FinishReason.LENGTH.value if isinstance(error, TruncationError) else None,
            turn_outcome="MALFORMED" if isinstance(error, _CONTENT_FAILURES) else "PROVIDER_ERROR")
        return record
    except Exception as error:  # noqa: BLE001 - class name only; never its text
        record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record.update(outcome="INTERNAL_ERROR", error_kind=type(error).__name__,
                      turn_outcome="PROVIDER_ERROR")
        return record
    record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    out = turn.output
    record.update(
        outcome="OK", model_calls=turn.model_calls, repairs=turn.repairs,
        input_tokens=turn.usage.input_tokens, output_tokens=turn.usage.output_tokens,
        cost_usd=str(turn.estimated_cost_usd), cost_basis="reported",
        finish_reason=turn.finish_reason.value, served_provider=turn.provider)
    if isinstance(out, FrameProposal):
        record.update(
            intent=out.intent.value,
            search=None if out.search is None else {
                "text_query": out.search.text_query, "area": out.search.area,
                "language_code": out.search.language_code,
                "page_size": out.search.page_size})
        record.update(replay_production(out, message, fixture))
    else:
        record.update(turn_outcome="MALFORMED")
    return record


# --- orchestration ----------------------------------------------------------------------
def read_existing(path: Path) -> list[dict[str, Any]]:
    """Records already in a results file (a torn last line is ignored)."""
    records: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and "fixture_id" in item and "repeat" in item:
                records.append(item)
    return records


def plan_tasks(
    cases: list[dict[str, Any]], repeats: int, *, categories: set[str] | None = None,
    limit: int | None = None, seed: int = 0,
) -> list[tuple[dict[str, Any], int]]:
    """Seeded selection (shuffle, then ``limit``), ordered repeat-major so an interrupted run
    still covers every selected fixture at least once before repeating any."""
    chosen = [c for c in cases if categories is None or c["category"] in categories]
    random.Random(seed).shuffle(chosen)
    if limit is not None:
        chosen = chosen[:limit]
    return [(case, r) for r in range(1, repeats + 1) for case in chosen]


class Runner:
    def __init__(
        self, stack: Stack, out_path: Path, totals: Totals, *, max_spend_usd: Decimal | None,
        max_calls: int | None, cad_rate: float, report_every: int, planned: int,
        emit: Callable[[str], None], call: Callable[..., dict[str, Any]] = run_one,
    ) -> None:
        self.stack, self.out_path, self.totals = stack, out_path, totals
        self.max_spend, self.max_calls = max_spend_usd, max_calls
        self.cad_rate, self.report_every, self.planned = cad_rate, report_every, planned
        self.emit, self._call = emit, call
        self.abort_reason: str | None = None
        self._lock = threading.Lock()
        self._file: Any = None
        self._done = 0

    def _admit(self) -> bool:
        """Checked BEFORE each call (under the lock): cap reached means no further call."""
        with self._lock:
            if self.abort_reason:
                return False
            if self.max_spend is not None and self.totals.cost_usd >= self.max_spend:
                self.abort_reason = "SPEND_CAP"
                return False
            if self.max_calls is not None and self.totals.model_calls >= self.max_calls:
                self.abort_reason = "MAX_CALLS"
                return False
            return True

    def _record(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
            self.totals.add(record)
            self._done += 1
            if self.report_every and self._done % self.report_every == 0:
                self.emit(cost_line(self.totals, self.cad_rate, self._done, self.planned))

    def _work(self, task: tuple[dict[str, Any], int]) -> None:
        if not self._admit():
            return
        self._record(self._call(self.stack, task[0], task[1]))

    def run(self, tasks: list[tuple[dict[str, Any], int]], concurrency: int) -> str:
        """Returns COMPLETE, SPEND_CAP, MAX_CALLS or INTERRUPTED. Results are flushed per line."""
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        needs_newline = self.out_path.exists() and self.out_path.stat().st_size > 0 and \
            not self.out_path.read_bytes().endswith(b"\n")
        with open(self.out_path, "a", encoding="utf-8", newline="\n") as handle:
            self._file = handle
            if needs_newline:
                handle.write("\n")
            try:
                if concurrency <= 1:
                    for task in tasks:
                        self._work(task)
                        if self.abort_reason:
                            break
                else:
                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        try:
                            list(pool.map(self._work, tasks))
                        except KeyboardInterrupt:
                            self.abort_reason = "INTERRUPTED"
                            pool.shutdown(wait=True, cancel_futures=True)
                            raise
            except KeyboardInterrupt:
                self.abort_reason = "INTERRUPTED"
            finally:
                handle.flush()
        return self.abort_reason or "COMPLETE"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.frame_bakeoff.run",
        description="Run FRAME bake-off fixtures through the real gateway stack.")
    p.add_argument("--model", help="pinned OpenRouter model id, e.g. vendor/model-name")
    p.add_argument("--provider", help="pinned provider slug, lower case")
    p.add_argument("--price-in", type=Decimal, help="USD per million input tokens")
    p.add_argument("--price-out", type=Decimal, help="USD per million output tokens")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--limit", type=int, default=None, help="use only N fixtures (seeded selection)")
    p.add_argument("--categories", default=None, help="comma-separated category names")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-spend-usd", type=Decimal, default=None,
                   help="HARD abort once cumulative cost reaches this (checked before each call)")
    p.add_argument("--max-calls", type=int, default=None,
                   help="HARD abort once this many provider calls (repairs included) were made")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--out", required=True, help="results JSONL path (keep it out of Git)")
    p.add_argument("--ssl-mode", choices=("strict", "os-store-nonstrict"), default="strict")
    p.add_argument("--dry-run", action="store_true",
                   help="scripted fake transport; no network and no key")
    p.add_argument("--env-file", default=None, help=f"dotenv file; only {KEY_VAR} is read")
    p.add_argument("--resume", action="store_true", help="skip (fixture, repeat) pairs in --out")
    p.add_argument("--cad-rate", type=float, default=1.43, help="USD to CAD for the cost line")
    p.add_argument("--report-every", type=int, default=25, help="cost line every N calls")
    p.add_argument("--fixtures", default=None, help="fixtures JSONL (default: the shipped file)")
    return p


def main(
    argv: list[str] | None = None, *, emit: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None, call: Callable[..., dict[str, Any]] = run_one,
    transport_factory: Callable[[str], HttpxTransport] | None = None,
) -> int:
    if emit is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    emit = emit or (lambda line: print(line, flush=True))
    args = build_parser().parse_args(argv)
    try:
        return _main(args, emit, env, call, transport_factory)
    except RunRefused as refusal:
        emit(f"refused: {refusal}")
        return EXIT_USAGE
    except GatewayError as error:  # e.g. an unpinned model id; static text only
        emit(f"refused: gateway configuration error ({error.kind.value})")
        return EXIT_USAGE


def _main(
    args: argparse.Namespace, emit: Callable[[str], None], env: dict[str, str] | None,
    call: Callable[..., dict[str, Any]], transport_factory: Callable[[str], HttpxTransport] | None,
) -> int:
    if args.repeats < 1 or args.concurrency < 1 or (args.limit is not None and args.limit < 1):
        raise RunRefused("--repeats, --concurrency and --limit must be at least 1")
    cases = fixture_lib.load_fixtures(args.fixtures)
    categories = None
    if args.categories:
        categories = {c.strip() for c in args.categories.split(",") if c.strip()}
        if not categories <= set(fixture_lib.CATEGORIES):
            raise RunRefused("unknown category in --categories")
    if args.dry_run:
        model, provider = args.model or DRY_MODEL, args.provider or DRY_PROVIDER
        price_in = args.price_in if args.price_in is not None else DRY_PRICE_IN
        price_out = args.price_out if args.price_out is not None else DRY_PRICE_OUT
        key = DRY_RUN_KEY
        transport = HttpxTransport.for_tests(DryRunModel(cases, provider).transport())
    else:
        if not (args.model and args.provider and args.price_in is not None
                and args.price_out is not None):
            raise RunRefused("--model, --provider, --price-in and --price-out are required")
        model, provider, price_in, price_out = args.model, args.provider, args.price_in, args.price_out
        key = load_key(env, args.env_file)  # refuses before anything is built or sent
        transport = (transport_factory or build_transport)(args.ssl_mode)
    out_path = Path(args.out)
    existing = read_existing(out_path)
    if out_path.exists() and out_path.stat().st_size > 0 and not args.resume:
        raise RunRefused("--out already exists; use --resume or choose a new path")
    done = {(r["fixture_id"], r["repeat"]) for r in existing}
    stack = build_stack(model, provider, price_in, price_out, key, transport)
    tasks = plan_tasks(cases, args.repeats, categories=categories, limit=args.limit, seed=args.seed)
    pending = [t for t in tasks if (t[0]["id"], t[1]) not in done]
    totals = Totals()
    for record in existing:
        totals.add(record)
    mode = "DRY RUN (scripted fake transport, no network)" if args.dry_run else f"LIVE ssl={args.ssl_mode}"
    emit(f"{mode}; model={model} provider={provider}; planned={len(tasks)} "
         f"pending={len(pending)} resumed={len(existing)}; "
         f"cap=US${args.max_spend_usd if args.max_spend_usd is not None else 'none'}")
    runner = Runner(stack, out_path, totals, max_spend_usd=args.max_spend_usd,
                    max_calls=args.max_calls, cad_rate=args.cad_rate,
                    report_every=args.report_every, planned=len(tasks), emit=emit, call=call)
    result = runner.run(pending, args.concurrency)
    emit(f"{result}: " + cost_line(totals, args.cad_rate, totals.records, len(tasks)))
    emit(f"results: {out_path}")
    return {"COMPLETE": EXIT_OK, "SPEND_CAP": EXIT_SPEND_CAP, "MAX_CALLS": EXIT_MAX_CALLS,
            "INTERRUPTED": EXIT_INTERRUPTED}[result]


if __name__ == "__main__":
    raise SystemExit(main())
