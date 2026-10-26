"""Production composition root: wires the real gateway, Grounding Lite client, cache, ledger and
quota service into the FastAPI app (docs/model-hosting-decision.md, docs/decisions.md 6, 16-18).

Rules this module keeps:

* It NEVER reads files or ``os.environ``. The caller passes an ``env`` mapping.
* Secrets (``OPENROUTER_API_KEY``, ``GOOGLE_MAPS_API_KEY``) come only from that mapping, are
  validated here (present, non-empty, printable ASCII, no whitespace or control characters) and
  live only inside secret-provider closures. They are not fields of ``Runtime`` and appear in no
  repr, str or error text. A refusal is a ``ConfigurationError`` with static text.
* ``identity_verifier`` is REQUIRED. There is NO production Firebase verifier yet, so a real
  deployment cannot be built: ``server.create_production_app`` deliberately fails closed until
  one exists. ``dev_scaffold`` is never enabled.
* The model and discovery transports default to the real locked-down classes
  (``HttpxTransport()``, ``HttpxMcpTransport()``: verify on, no redirects, no proxy env). They can
  be replaced ONLY through the ``model_transport`` and ``discovery_transport`` parameters (the
  live smoke script uses that to supply a corporate-proxy trust store). This module exposes no
  TLS option and never weakens verification.
* The gateway is built with ``production=True``: placeholder prices are refused.

STATE (ACCESS-02 / #35, decision 26: code only, no cloud resources). Durable state sits behind
the protocols in ``app/stores``: quota ledger, consent ledger, workspaces, idempotent replay
records, display cache, and the whole-pilot spend counter plus the kill switch. ``build_runtime``
chooses the implementation from its arguments, never silently:

* a Firestore client (``firestore_client``), or a project id (``firestore_project_id`` or
  ``FIRESTORE_PROJECT_ID`` in ``env``) -> the Firestore adapters. A project id needs the optional
  ``gcp`` extra (``google-cloud-firestore``); without it the build fails closed. Durable state also
  REQUIRES a whole-pilot spend budget (``spend_budget``, or ``PILOT_SPEND_MAX_TOKENS`` /
  ``PILOT_SPEND_MAX_USD`` in ``env``);
* otherwise ``allow_in_memory_state=True`` (development, tests, the smoke script) -> in-memory;
* otherwise ``ConfigurationError('shared state not implemented ...')``: production fails closed.

Still per process whatever the choice (tracked under #35): the per-principal cost windows
(daily ceiling, paid-failure and free-turn limiters), the operation limiters, the quota latch
and the circuit breakers.

Escalation or fallback: a second model would be a second ``OpenRouterGateway`` (bound to its own
audited ``RuntimeConfig``, see ``second_model_config``) wrapped in its own ``ResilientGateway``.
It is NOT wired: a failover policy (when to escalate, how it is charged) is future work, and
``ResilientGateway`` deliberately never changes the model or provider.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import InvalidOperation
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI

from .auth import IdentityVerifier
from .consent import InMemoryConsentStore
from .costs import CostLedger
from .discovery import (
    TOOL_SPECS,
    DisplayCacheStore,
    GroundingLiteMcpClient,
    HttpxMcpTransport,
    KillSwitchProbe,
    McpTransport,
)
from .domain import (
    PhaseFlags,
    RuntimeConfig,
    RuntimeTTLs,
    TokenCeilings,
    ToolSchemaVersion,
)
from .entitlements import PILOT_POLICY
from .frame import prompt_version_hashes
from .gateway import (
    GatewayPolicy,
    HttpxTransport,
    OpenRouterGateway,
    QuotaLatch,
    ResiliencePolicy,
    ResilientGateway,
)
from .gateway.openrouter import Transport
from .gateway.pricing import ModelPrice, PriceTable, UnitPrice
from .main import create_app
from .orchestrator import TurnOrchestrator
from .quota import QuotaService
from .stores import SpendBudget, StateStores, StoreKillSwitch, firestore_stores, memory_stores
from .stores import firestore as firestore_adapters
from .telemetry import TraceSink

OPENROUTER_KEY_VAR = "OPENROUTER_API_KEY"
GOOGLE_MAPS_KEY_VAR = "GOOGLE_MAPS_API_KEY"

PRIMARY_MODEL_ID = "google/gemma-4-31b-it"
PROVIDER_SLUG = "deepinfra"
PRICE_TABLE_VERSION = "live-2026-10-17"
RUNTIME_CONFIG_VERSION = "phase0-gemma-4-31b-1"
CLASSIFIER_VERSION = "keyword-guard-1"
POLICY_VERSION = PILOT_POLICY.version
ENTITLEMENT_VERSION = PILOT_POLICY.version
# Listing price at DeepInfra, USD per million tokens (verified in the 2026-10-17 bake-off:
# US$0.0641 per 1,000 FRAME turns).
PRIMARY_INPUT_USD_PER_MILLION = Decimal("0.20")
PRIMARY_OUTPUT_USD_PER_MILLION = Decimal("0.40")
GROUNDING_LITE_UNIT_NAME = "grounding_lite_search"
# TODO(pricing): the Grounding Lite search unit price is UNCONFIRMED and is set to 0 here.
# Google's pricing page was reported as US$7 per 1,000 events after a free monthly allowance;
# do not assume it. Nothing reads this value yet (the ledger records model cost only); confirm
# against the billing console before pilot traffic.
GROUNDING_LITE_USD_PER_REQUEST = Decimal("0")

FIRESTORE_PROJECT_VAR = "FIRESTORE_PROJECT_ID"
SPEND_MAX_TOKENS_VAR = "PILOT_SPEND_MAX_TOKENS"
SPEND_MAX_USD_VAR = "PILOT_SPEND_MAX_USD"
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

SECOND_MODEL_ID = "qwen/qwen3-next-80b-a3b-instruct"
SECOND_MODEL_PRICE = ModelPrice(
    model_id=SECOND_MODEL_ID, provider=PROVIDER_SLUG,
    input_usd_per_million=Decimal("0.09"), output_usd_per_million=Decimal("1.10"))
FRAME_TOOL_ALLOWLIST = ("search_places",)

_SECRET_SHAPE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII: no whitespace, no controls


class ConfigurationError(Exception):
    """The runtime cannot be built safely. The message is static: it names a setting, never a
    value, so it is safe to log or show."""


def _secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{name} is missing or empty")
    if not _SECRET_SHAPE.fullmatch(value):
        raise ConfigurationError(f"{name} is malformed (whitespace or control characters)")
    return value


def _spend_budget(env: Mapping[str, str]) -> SpendBudget | None:
    """The whole-pilot cap from ``env`` (None when neither variable is set). Malformed, zero or
    negative values are refused rather than ignored."""
    tokens, usd = env.get(SPEND_MAX_TOKENS_VAR), env.get(SPEND_MAX_USD_VAR)
    if tokens is None and usd is None:
        return None
    try:
        return SpendBudget(
            max_tokens=int(tokens) if tokens is not None else None,
            max_usd=Decimal(usd) if usd is not None else None)
    except (ValueError, InvalidOperation):
        raise ConfigurationError("the pilot spend budget variables are malformed") from None


def _choose_state(
    env: Mapping[str, str], *, firestore_client: Any, firestore_project_id: str | None,
    allow_in_memory_state: bool, now: Callable[[], datetime],
) -> StateStores:
    """Pick the durable-state implementation (see the module docstring); fails closed."""
    project = firestore_project_id if firestore_project_id is not None else env.get(
        FIRESTORE_PROJECT_VAR)
    if firestore_client is None and project is None:
        if allow_in_memory_state is not True:
            raise ConfigurationError(
                "shared state not implemented: configure Firestore or allow in-memory state (dev)")
        return memory_stores(now)
    client = firestore_client
    if client is None:
        if not isinstance(project, str) or not _PROJECT_ID.fullmatch(project):
            raise ConfigurationError("the firestore project id is malformed")
        try:
            client = firestore_adapters.make_client(project)
        except ImportError:
            raise ConfigurationError(
                "firestore support is not installed (install the gcp extra)") from None
        except Exception:  # noqa: BLE001 - static text; credentials errors name secrets
            raise ConfigurationError("the firestore client could not be created") from None
    return firestore_stores(client, now)


def _provider(value: str) -> Callable[[], str]:
    """A secret provider. The value lives only in this closure (not in any repr)."""

    def provide() -> str:
        return value

    return provide


def _tool_schema_versions() -> tuple[ToolSchemaVersion, ...]:
    versions = []
    for tool_id, spec in TOOL_SPECS.items():
        canonical = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        versions.append(ToolSchemaVersion(name=tool_id.value, version=f"sha256-{digest}"))
    return tuple(sorted(versions, key=lambda item: item.name))


def _config(version: str, model_id: str) -> RuntimeConfig:
    return RuntimeConfig(
        version=version,
        model_id=model_id,
        provider=PROVIDER_SLUG,
        provider_allowlist=(PROVIDER_SLUG,),
        price_table_version=PRICE_TABLE_VERSION,
        prompt_version_hashes=prompt_version_hashes(),
        tool_schema_versions=_tool_schema_versions(),
        classifier_version=CLASSIFIER_VERSION,
        policy_version=POLICY_VERSION,
        entitlement_version=ENTITLEMENT_VERSION,
        token_ceilings=TokenCeilings(),
        ttls=RuntimeTTLs(),
        flags=PhaseFlags(),
        maps_content_to_model=False,
    )


def pinned_runtime_config() -> RuntimeConfig:
    """The audited Phase 0 bundle. Privacy settings (zdr, data_collection=deny,
    allow_fallbacks=False) are forced by the ``RuntimeConfig`` types; the provider allowlist is
    exactly ('deepinfra',). ``maps_content_to_model`` stays False (decision 17)."""
    return _config(RUNTIME_CONFIG_VERSION, PRIMARY_MODEL_ID)


def pinned_price_table() -> PriceTable:
    return PriceTable(
        version=PRICE_TABLE_VERSION,
        placeholder=False,
        model_prices=(ModelPrice(
            model_id=PRIMARY_MODEL_ID, provider=PROVIDER_SLUG,
            input_usd_per_million=PRIMARY_INPUT_USD_PER_MILLION,
            output_usd_per_million=PRIMARY_OUTPUT_USD_PER_MILLION),),
        unit_prices=(UnitPrice(
            name=GROUNDING_LITE_UNIT_NAME, usd_per_request=GROUNDING_LITE_USD_PER_REQUEST),),
    )


def second_model_config() -> RuntimeConfig:
    """The secondary candidate (Qwen3-Next 80B at DeepInfra; price ``SECOND_MODEL_PRICE``).
    Description only: NOT wired. A second gateway built from this config (and a price table that
    lists ``SECOND_MODEL_PRICE``) is how an escalation or fallback would be added; the failover
    policy is future work."""
    return _config("phase0-qwen3-next-80b-1", SECOND_MODEL_ID)


@dataclass(frozen=True, repr=False)
class Runtime:
    """The composed parts, for inspection (ledger, quotas, kill switch, latch). Holds no secret:
    keys exist only inside the provider closures of the gateway and the client."""

    app: FastAPI
    gateway: ResilientGateway
    discovery_client: GroundingLiteMcpClient
    display_cache: DisplayCacheStore
    orchestrator: TurnOrchestrator
    costs: CostLedger
    quotas: QuotaService
    kill_switch: KillSwitchProbe
    quota_latch: QuotaLatch
    config: RuntimeConfig
    price_table: PriceTable
    stores: StateStores | None = None
    places_service: Any = None

    def __repr__(self) -> str:
        return f"Runtime(model={self.config.model_id!r}, provider={self.config.provider!r})"

    __str__ = __repr__


def build_runtime(
    env: Mapping[str, str],
    *,
    identity_verifier: IdentityVerifier | None,
    model_transport: Transport | None = None,
    discovery_transport: McpTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    sink: TraceSink | None = None,
    allow_in_memory_state: bool = False,
    firestore_client: Any = None,
    firestore_project_id: str | None = None,
    spend_budget: SpendBudget | None = None,
    places_service: Any = None,
    app_check_verifier: Any = None,
) -> Runtime:
    """Build the production app from ``env`` (a mapping the caller supplies).

    ``identity_verifier`` is required (None raises): the app must fail closed. The Firebase
    verifier and the App Check verifier are built elsewhere and passed in (``app_check_verifier``
    is only attached to ``app.state`` here; ``places_service`` likewise, for the places routes).
    Durable state is chosen as the module docstring says. Raises ``ConfigurationError`` (static
    text, no values) for any refusal."""
    if identity_verifier is None:
        raise ConfigurationError("an identity verifier is required (no production verifier yet)")
    now: Callable[[], datetime] = clock if clock is not None else (lambda: datetime.now(UTC))
    stores = _choose_state(
        env, firestore_client=firestore_client, firestore_project_id=firestore_project_id,
        allow_in_memory_state=allow_in_memory_state, now=now)
    budget = spend_budget if spend_budget is not None else _spend_budget(env)
    if stores.kind != "memory" and budget is None:
        # Durable, shared state means real traffic: the whole-pilot cap is mandatory (ACCESS-02).
        raise ConfigurationError("a pilot spend budget is required with durable state")
    openrouter_key = _secret(env, OPENROUTER_KEY_VAR)
    maps_key = _secret(env, GOOGLE_MAPS_KEY_VAR)

    config = pinned_runtime_config()
    prices = pinned_price_table()
    latch = QuotaLatch()
    kill_switch = StoreKillSwitch(stores.costs, now)
    try:
        inner = OpenRouterGateway(
            model_transport if model_transport is not None else HttpxTransport(),
            _provider(openrouter_key), config, GatewayPolicy(tool_allowlist=FRAME_TOOL_ALLOWLIST),
            prices, production=True, quota_latch=latch)
    except Exception:  # noqa: BLE001 - static text; never chain a provider's message
        raise ConfigurationError("model gateway configuration refused") from None
    gateway = ResilientGateway(inner, ResiliencePolicy(), price_table=prices)

    client_kwargs: dict[str, Any] = {
        "config": config, "kill_switch": kill_switch, "sink": sink, "now": now}
    try:
        discovery = GroundingLiteMcpClient(
            discovery_transport if discovery_transport is not None else HttpxMcpTransport(),
            _provider(maps_key), **client_kwargs)
    except Exception:  # noqa: BLE001
        raise ConfigurationError("discovery client configuration refused") from None

    ledger = CostLedger(now, store=stores.costs, budget=budget)
    cache = stores.display_cache
    quotas = QuotaService(store=stores.quota, clock=now)
    app = create_app(
        verifier=identity_verifier, trace_sink=sink, gateway=gateway, discovery_client=discovery,
        display_cache=cache, runtime_config=config, quotas=quotas, clock=now,
        cost_ledger=ledger, dev_scaffold=False, workspace_store=stores.workspaces,
        idempotency_store=stores.idempotency,
        consent_store=InMemoryConsentStore(now, ledger=stores.consents))
    # Hooks other modules fill in (Firebase/App Check verifiers, the places service): None until
    # the caller passes an object; routes read them from app.state and fail closed on None.
    app.state.places_service = places_service
    app.state.app_check_verifier = app_check_verifier
    return Runtime(
        app=app, gateway=gateway, discovery_client=discovery, display_cache=cache,
        orchestrator=app.state.orchestrator, costs=ledger, quotas=quotas, kill_switch=kill_switch,
        quota_latch=latch, config=config, price_table=prices, stores=stores,
        places_service=places_service)
