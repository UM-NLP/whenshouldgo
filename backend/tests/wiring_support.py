"""Shared builders for the orchestrator-wiring tests. Holds no tests.

Everything is synthetic: no network, no keys, invented Maps content. The scripted gateway
reads the workspace id and base version from the FRAME context the way a real model would
(it is data in the prompt), so a test can script a FRAME answer before the workspace exists.
"""
from __future__ import annotations

import enum
import json
import threading
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from dataclasses import fields, is_dataclass

from fastapi.testclient import TestClient
from pydantic import BaseModel
from gateway_fakes import FakeGateway, FakeTransport, completion_response
from maps_simulator import (
    NOW,
    FakeMcpTransport,
    SyntheticMapsClient,
    synthetic_payload,
)
from test_gateway_support import make_config, make_gateway, make_policy

from app import telemetry as t
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.costs import CostLedger
from app.discovery import DisplayCache, GroundingLiteMcpClient, KillSwitch, ToolRun
from app.domain import (
    ContentClass,
    Fact,
    FactSource,
    PrincipalKind,
    RuntimeConfig,
    Confirmation,
)
from app.entitlements import Allowance, EntitlementPolicy, Period, PolicyRegistry
from app.frame import FrameIntent, FrameProposal, ProposedFact, SearchArgs
from app.gateway import (
    CallRole,
    GenerateRequest,
    ResiliencePolicy,
    ResilientGateway,
    placeholder_price_table,
)
from app.gateway.errors import GatewayError
from app.gateway.types import FinishReason, ModelTurn, Usage
from app.main import create_app
from app.orchestrator import TurnOrchestrator
from app.quota import QuotaService

CANARY = "MAPSCANARY-7f3a91"  # appears only in the synthetic grounded summary
TITLE_CANARY = "Invented Café One"  # first synthetic attribution title
STATE_PREFIX = "WORKSPACE_STATE_JSON:"
TOKENS = {
    "tok-a": VerifiedIdentity("guest-a", "anonymous"),
    "tok-b": VerifiedIdentity("guest-b", "anonymous"),
}
TRAVEL = {"message": "Plan a trip to a quiet Toronto restaurant this evening"}
OUT_OF_SCOPE = {"message": "Write my python homework"}


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def registry(guest_turns: int = 5) -> PolicyRegistry:
    policy = EntitlementPolicy(
        version="test-wiring",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        grace_rule="unknown-version-uses-current",
        allowances={
            PrincipalKind.GUEST: Allowance(guest_turns, 10, Period.LIFETIME),
            PrincipalKind.REGISTERED_FREE: Allowance(guest_turns, 10, Period.ROLLING_30_DAYS),
        },
    )
    return PolicyRegistry((policy,), policy.version)


def canary_payload(places: int = 3, canary: str = CANARY) -> dict[str, Any]:
    text = f"Synthetic fixture: {canary} two invented quiet spots [0] and a third [1]."
    return synthetic_payload(text=text, places=places)


def workspace_state(request: GenerateRequest) -> dict[str, Any]:
    """The workspace view the model is shown: its facts only (no ids, no versions)."""
    for message in request.context:
        if message.content.startswith(STATE_PREFIX):
            return json.loads(message.content[len(STATE_PREFIX):])
    raise AssertionError("FRAME context carries no workspace state")


def wire_text(request: GenerateRequest) -> str:
    """Everything a gateway request would put on the wire, as one string (prompt text, tool
    specs and the output schema)."""
    return (
        request.model_dump_json(exclude={"output_schema"})
        + json.dumps(request.output_schema.json_schema)
        + "".join(m.content for m in request.context)
    )


def deep_text(obj: Any) -> str:
    """Every string-like value reachable from ``obj``, as one string: text, bytes, numbers and
    Decimals, enum values, exception args and their __cause__/__context__, dataclasses, pydantic
    models INCLUDING fields hidden from repr, containers and the __dict__ / __slots__ of plain
    objects. A repr can hide content; a real leak sits in the data. Cycle-safe."""
    parts: list[str] = []
    seen: set[int] = set()
    skip = (type, types.FunctionType, types.BuiltinFunctionType, types.MethodType,
            types.ModuleType)

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 60 or item is None or isinstance(item, (bool, skip)):
            return
        if isinstance(item, str):
            parts.append(item)
            return
        if isinstance(item, (bytes, bytearray)):
            parts.append(bytes(item).decode("utf-8", "replace"))
            return
        if isinstance(item, (int, float, Decimal)):
            parts.append(str(item))
            return
        if id(item) in seen:
            return
        seen.add(id(item))
        if isinstance(item, enum.Enum):
            walk(item.value, depth + 1)
        elif isinstance(item, BaseModel):
            walk(item.model_dump(mode="python"), depth + 1)
        elif is_dataclass(item):
            for f in fields(item):
                walk(getattr(item, f.name), depth + 1)
        elif isinstance(item, dict):
            for key, value in item.items():
                walk(key, depth + 1)
                walk(value, depth + 1)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for member in item:
                walk(member, depth + 1)
        else:
            if isinstance(item, BaseException):
                walk(item.args, depth + 1)
                walk(item.__cause__, depth + 1)
                walk(item.__context__, depth + 1)
            walk(getattr(item, "__dict__", None), depth + 1)
            for klass in type(item).__mro__:
                for name in getattr(klass, "__slots__", ()):
                    walk(getattr(item, name, None), depth + 1)

    walk(obj)
    return chr(10).join(parts)


def fact(fact_id: str = "party", value: str = "two adults", **overrides: Any) -> Fact:
    fields: dict[str, Any] = dict(
        id=fact_id, key=fact_id, value=value, source=FactSource.INFERENCE,
        confirmation=Confirmation.UNCONFIRMED, hard=False, scope="trip",
    )
    fields.update(overrides)
    return Fact(**fields)


def search(text_query: str = "quiet restaurant", **overrides: Any) -> SearchArgs:
    # Fix 9: a search without a usable area is never sent to Google (NEEDS_AREA), so the default
    # carries the place the standard TRAVEL message names. Pass area=None to test the no-area case.
    overrides.setdefault("area", "Toronto")
    return SearchArgs(text_query=text_query, **overrides)


def frame_turn(
    config: RuntimeConfig,
    *,
    intent: FrameIntent = FrameIntent.DISCOVERY,
    search_args: SearchArgs | None = ...,  # type: ignore[assignment]
    facts: Sequence[Fact | ProposedFact] = (),
    usage: Usage = Usage(input_tokens=400, output_tokens=60),
    output: Any = None,
) -> Callable[[GenerateRequest], ModelTurn]:
    """A scripted FRAME answer: a FrameProposal (intent, search, soft facts). The model owns no
    state field, so there is nothing else to script. A Fact is turned into the ProposedFact the
    model could actually have written (its hard/confirmed flags cannot be expressed)."""

    def build(request: GenerateRequest) -> ModelTurn:
        if output is not None:
            produced = output
        else:
            chosen = search() if search_args is ... else search_args
            produced = FrameProposal(
                intent=intent,
                search=chosen if intent == FrameIntent.DISCOVERY else None,
                facts=[
                    f if isinstance(f, ProposedFact)
                    else ProposedFact(key=f.key, value=f.value, scope=f.scope)
                    for f in facts
                ],
            )
        return ModelTurn(
            call_role=CallRole.FRAME,
            content_class=ContentClass.USER,
            output=produced,
            finish_reason=FinishReason.STOP,
            usage=usage,
            latency_ms=3.0,
            estimated_cost_usd=Decimal("0.0001"),
            price_table_version=config.price_table_version,
            price_is_placeholder=True,
            model_id=config.model_id,
            provider=config.provider,
            runtime_config_version=config.version,
        )

    return build


class ScriptedGateway(FakeGateway):
    """FakeGateway whose script items may also be ``request -> ModelTurn`` callables."""

    def __init__(self, script: Sequence[Any], config: RuntimeConfig | None = None) -> None:
        super().__init__([], make_policy())
        self.items = list(script)
        self.config = config or make_config()

    def generate(self, request: GenerateRequest) -> ModelTurn:
        from app.gateway.policy import check_request

        check_request(request, self._policy)
        self.requests.append(request)
        if not self.items:
            raise AssertionError("ScriptedGateway script exhausted")
        item = self.items.pop(0)
        if callable(item):
            item = item(request)
        if isinstance(item, GatewayError):
            raise item
        return item


class RecordingClient:
    """Wraps a GroundingLiteClient, remembering the ToolRun and every call it was given."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.runs: list[ToolRun] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[float | None] = []

    @property
    def tripped(self) -> bool:
        return bool(getattr(self.inner, "tripped", False))

    def call_tool(self, run: ToolRun, tool: str, arguments: Mapping[str, Any],
                  timeout_seconds: float | None = None) -> Any:
        self.runs.append(run)
        self.calls.append((tool, dict(arguments)))
        self.timeouts.append(timeout_seconds)
        return self.inner.call_tool(run, tool, arguments)


class RaisingClient:
    tripped = False

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[str] = []

    def call_tool(self, run: ToolRun, tool: str, arguments: Mapping[str, Any],
                  timeout_seconds: float | None = None) -> Any:
        self.calls.append(tool)
        raise self.error


def real_client(
    config: RuntimeConfig,
    transport: FakeMcpTransport,
    sink: t.TraceSink,
    clock: MutableClock,
    kill_switch: KillSwitch | None = None,
) -> GroundingLiteMcpClient:
    return GroundingLiteMcpClient(
        transport, lambda: "AIzaSyFAKE-NOT-A-REAL-KEY-0123456789", config=config,
        kill_switch=kill_switch, now=clock, sink=sink,
    )


class Env:
    """One wired application with every collaborator reachable from the test."""

    def __init__(
        self,
        *,
        gateway: Any = None,
        discovery: Any = None,
        config: RuntimeConfig | None = None,
        guest_turns: int = 5,
        with_cache: bool = True,
        policy_registry: PolicyRegistry | None = None,
        dev_scaffold: bool | None = None,
    ) -> None:
        self.config = config or make_config()
        self.sink = t.InMemorySink()
        self.clock = MutableClock()
        self.gateway = gateway
        # A callable without ``call_tool`` is a factory: it receives this Env (sink and clock
        # exist by then) so a real client can share the test's sink and clock.
        if callable(discovery) and not hasattr(discovery, "call_tool"):
            discovery = discovery(self)
        self.discovery = discovery
        self.cache = DisplayCache(self.clock) if with_cache else None
        self.costs = CostLedger(self.clock)  # the ledger shares the test clock
        self.quotas = QuotaService(policy_registry or registry(guest_turns))
        # The scaffold is explicit: tests without a gateway ask for it.
        extra: dict[str, Any] = {
            "dev_scaffold": gateway is None if dev_scaffold is None else dev_scaffold}
        application = create_app(
            verifier=StaticTokenVerifier(TOKENS),
            resolver=PrincipalResolver(policies=self.quotas.policies),
            trace_sink=self.sink,
            gateway=gateway,
            discovery_client=discovery,
            display_cache=self.cache,
            runtime_config=self.config,
            quotas=self.quotas,
            clock=self.clock,
            cost_ledger=self.costs,
            **extra,
        )
        self.app = application
        self.client = TestClient(application, raise_server_exceptions=False)
        self.orchestrator: TurnOrchestrator = application.state.orchestrator

    def post(self, body: Mapping[str, Any] = TRAVEL, *, key: str = "k1", token: str = "tok-a"):
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
        return self.client.post("/v1/agent/turns", json=dict(body), headers=headers)

    def turns(self, uid: str = "guest-a") -> int:
        return self.quotas.usage_for(uid).successful_turns

    def reserved(self, uid: str = "guest-a") -> set[str]:
        return self.quotas.usage_for(uid).reserved_keys

    def events(self, cls: type[t.TraceEvent]) -> list[Any]:
        return [e for e in self.sink.events if isinstance(e, cls)]


def happy_env(**kw: Any) -> tuple[Env, ScriptedGateway, RecordingClient]:
    config = make_config()
    gateway = ScriptedGateway([frame_turn(config) for _ in range(6)], config)
    maps = RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config, **kw)
    maps.inner._now = env.clock  # noqa: SLF001 - one clock for the cache, validator and results
    return env, gateway, maps


def threads(count: int, target: Callable[[int], None]) -> None:
    workers = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


__all__ = [name for name in dir() if not name.startswith("_")]


class FrameEchoTransport(FakeTransport):
    """A provider transport that answers FRAME like a model: a FrameProposal. ``before`` responses
    (errors, odd bodies) are served first; after that every call gets a valid answer."""

    def __init__(
        self, before: Sequence[Any] = (), *, intent: str = "OTHER", query: str = "quiet restaurant",
        prompt_tokens: int | None = 100, completion_tokens: int | None = 20,
        search: dict[str, Any] | None = None, facts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__([])
        self.search = search  # the exact search object the "model" returns (overrides query)
        self.facts = facts
        self.before = list(before)
        self.intent = intent
        self.query = query
        self.usage = (prompt_tokens, completion_tokens)

    def send(self, request: Any, credential: Any) -> Any:
        if self.before:
            self._script = [self.before.pop(0)]
        else:
            answer: dict[str, Any] = {"intent": self.intent}
            if self.intent == "DISCOVERY":
                answer["search"] = self.search or {"text_query": self.query, "area": "Toronto"}
            if self.facts:
                answer["facts"] = self.facts
            self._script = [completion_response(
                json.dumps(answer), prompt_tokens=self.usage[0], completion_tokens=self.usage[1])]
        return super().send(request, credential)


def real_stack(
    before: Sequence[Any] = (), *, attempts: int = 3, discovery: Any = None,
    config: RuntimeConfig | None = None, intent: str = "OTHER", query: str = "quiet restaurant",
    prompt_tokens: int | None = 100, completion_tokens: int | None = 20,
    breaker_threshold: int = 5, search: dict[str, Any] | None = None, facts: list[dict[str, Any]] | None = None,
    **env_kw: Any,
) -> tuple[Env, FrameEchoTransport, Any]:
    """An Env over the REAL ResilientGateway wrapping the REAL OpenRouterGateway (a fake
    transport underneath): the stack ScriptedGateway cannot exercise."""
    config = config or make_config()
    transport = FrameEchoTransport(before, intent=intent, query=query,
                                   prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                                   search=search, facts=facts)
    inner = make_gateway(transport, config=config)
    gateway = ResilientGateway(
        inner, ResiliencePolicy(max_attempts=attempts, breaker_failure_threshold=breaker_threshold),
        price_table=placeholder_price_table(config.model_id, config.provider),
        sleep=lambda seconds: None)
    maps = discovery or RecordingClient(SyntheticMapsClient(payload=canary_payload()))
    env = Env(gateway=gateway, discovery=maps, config=config, **env_kw)
    if hasattr(maps, "inner") and hasattr(maps.inner, "_now"):
        maps.inner._now = env.clock  # noqa: SLF001
    return env, transport, inner
