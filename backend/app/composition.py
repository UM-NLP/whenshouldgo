"""Production composition root: wires the real gateway, Grounding Lite client, cache, ledger and
quota service into the FastAPI app (docs/model-hosting-decision.md, docs/decisions.md 6, 16-18).

Rules this module keeps:

* It NEVER reads files or ``os.environ``. The caller passes an ``env`` mapping.
* Secrets (``OPENROUTER_API_KEY``, ``GOOGLE_MAPS_API_KEY``) come only from that mapping, are
  validated here (present, non-empty, printable ASCII, no whitespace or control characters) and
  live only inside secret-provider closures. They are not fields of ``Runtime`` and appear in no
  repr, str or error text. A refusal is a ``ConfigurationError`` with static text.
* ``identity_verifier`` is REQUIRED. ``server.create_production_app`` builds the Firebase verifier
  from ``FIREBASE_PROJECT_ID`` (``firebase_identity_from_env``: lazy import of the optional ``auth``
  extra) and FAILS CLOSED without it. ``dev_scaffold`` is never enabled.
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

SERVICES built here and attached to ``app.state`` (each route answers a typed 503 until its service
is attached): the Trip Board ``workspace_service`` (same ``WorkspaceStore`` and per-tenant lock as the
orchestrator), ``action_service`` (PendingAction protocol), ``account_service`` (link, merge, export,
delete; the Firebase Admin protocol is None until the owner supplies credentials, so link completion
is a typed 503 and the login itself is not deleted) and ``places_service`` (off unless a REVIEWED
price table is passed: the unreviewed default keeps Places Details and Routes disabled). Every store's
deleter and export contributor is registered with the account service, and a deleted account is
refused by the orchestrator and the late writers (``account_service.assert_active``).

PHASE 1, BUILT DARK (decision 25): ``history_service`` and ``saved_place_service`` are always built
and attached, over the same backend as the other stores, but nothing in them runs unless
``history_active(config)``: the ``history_enabled`` flag (``build_runtime(history_enabled=...)``,
default False) AND a ``history_notice_version`` on the owner-approved list
(``domain.APPROVED_HISTORY_NOTICE_VERSIONS``, empty until the signed legal checklist and Phase 0
exit). The flag alone is never enough. Their deleters, export contributors and the consent-withdrawal
cascade hooks are registered here.

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

from .account import (
    AccountService,
    build_account_service,
    register_history_state,
    register_plan_state,
)
from .actions import ActionService
from .auth import IdentityVerifier, PrincipalResolver
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
    ConsentPurpose,
    PhaseFlags,
    RuntimeConfig,
    RuntimeTTLs,
    TokenCeilings,
    ToolSchemaVersion,
    evolve,
)
from .entitlements import PILOT_POLICY
from .frame import prompt_version_hashes
from .history import HistoryService
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
from .places import UNREVIEWED_PRICE_TABLE, PlacesPriceTable, build_places_service
from .quota import QuotaService
from .saved_places import SavedPlaceActionSink, SavedPlaceService
from .stores import SpendBudget, StateStores, StoreKillSwitch, firestore_stores, memory_stores
from .stores import firestore as firestore_adapters
from .stores.actions_store import (
    FirestorePendingActionStore,
    MemoryPendingActionStore,
    PendingActionStore,
)
from .stores.history_store import firestore_history_store, memory_history_store
from .stores.plans_store import PlanStores, firestore_plan_stores, memory_plan_stores
from .stores.saved_places_store import firestore_saved_place_stores, memory_saved_place_stores
from .telemetry import TraceSink
from .workspaces import WorkspaceService, build_workspace_service

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

FIREBASE_PROJECT_VAR = "FIREBASE_PROJECT_ID"
FIREBASE_PROJECT_NUMBER_VAR = "FIREBASE_PROJECT_NUMBER"
APP_CHECK_ENFORCE_VAR = "APP_CHECK_ENFORCE"
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


def firebase_identity_from_env(env: Mapping[str, str]) -> tuple[IdentityVerifier, set[str]]:
    """The production identity verifier and the ``disabled_uids`` set it shares with the
    ``PrincipalResolver`` (the SAME set object, so a revoked uid becomes DISABLED).

    Fails closed with a static ``ConfigurationError``: without ``FIREBASE_PROJECT_ID``, or when the
    optional ``auth`` extra (PyJWT, cryptography) is not installed. Nothing here calls Google; the
    signing keys are fetched on the first verification. No revocation checker is wired (it needs
    Firebase Admin credentials, an owner TODO), so ``disabled_uids`` stays empty until one exists."""
    project = env.get(FIREBASE_PROJECT_VAR)
    if not isinstance(project, str) or not project.strip():
        raise ConfigurationError(f"{FIREBASE_PROJECT_VAR} is missing or empty")
    try:
        from .auth_firebase import FirebaseConfigError, FirebaseIdentityVerifier
    except ImportError:
        raise ConfigurationError(
            "firebase auth support is not installed (install the auth extra)") from None
    disabled: set[str] = set()
    try:
        verifier = FirebaseIdentityVerifier.from_environment(env, disabled_uids=disabled)
    except FirebaseConfigError:
        raise ConfigurationError("the firebase verifier configuration is invalid") from None
    return verifier, disabled


def _app_check_policy(env: Mapping[str, str], verifier: Any) -> Any:
    """The App Check policy (None when nothing is configured: no gate at all). Enforcement is
    ``APP_CHECK_ENFORCE=true`` only (default off, monitor mode records the signal and never blocks);
    enforcing without a verifier, or a value that is neither empty, true nor false, is refused."""
    raw = env.get(APP_CHECK_ENFORCE_VAR, "")
    if raw not in ("", "true", "false"):
        raise ConfigurationError(f"{APP_CHECK_ENFORCE_VAR} must be true or false")
    enforce = raw == "true"
    number = env.get(FIREBASE_PROJECT_NUMBER_VAR)
    if verifier is None and number:
        try:
            from .appcheck import AppCheckVerifier, default_fetcher
            from .auth_firebase import FirebaseConfigError
        except ImportError:
            raise ConfigurationError(
                "app check support is not installed (install the auth extra)") from None
        try:
            verifier = AppCheckVerifier(number, default_fetcher())
        except FirebaseConfigError:
            raise ConfigurationError("the app check configuration is invalid") from None
    if verifier is None:
        if enforce:
            raise ConfigurationError(
                f"{APP_CHECK_ENFORCE_VAR} needs {FIREBASE_PROJECT_NUMBER_VAR}")
        return None
    try:
        from .appcheck import AppCheckPolicy
    except ImportError:
        raise ConfigurationError(
            "app check support is not installed (install the auth extra)") from None
    return AppCheckPolicy(enforce=enforce, verifier=verifier)


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
    action_service: ActionService | None = None
    workspace_service: WorkspaceService | None = None
    account_service: AccountService | None = None
    plan_stores: PlanStores | None = None
    history_service: HistoryService | None = None
    saved_place_service: SavedPlaceService | None = None

    def __repr__(self) -> str:
        return f"Runtime(model={self.config.model_id!r}, provider={self.config.provider!r})"

    __str__ = __repr__

    def purge_expired(self) -> dict[str, int]:
        """The retention sweep: expired pending actions and display-cache entries. NO scheduler
        calls this yet (Cloud Scheduler or Tasks, an owner TODO); Firestore TTL policies are the
        primary mechanism and this deletes the same records early. Counts only."""
        return {
            "pending_actions": self.action_service.purge() if self.action_service else 0,
            "display_cache": self.display_cache.purge(),
            "history": self.history_service.purge() if self.history_service else 0,
        }


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
    places_price_table: PlacesPriceTable | None = None,
    places_transport: Any = None,
    app_check_verifier: Any = None,
    disabled_uids: set[str] | None = None,
    firebase_admin: Any = None,
    history_enabled: bool = False,
    history_notice_version: str | None = None,
) -> Runtime:
    """Build the production app from ``env`` (a mapping the caller supplies).

    ``identity_verifier`` is required (None raises): the app must fail closed. The Firebase
    verifier comes from ``firebase_identity_from_env`` (``server.create_production_app``), together
    with ``disabled_uids``: the resolver gets that SAME set object. ``firebase_admin`` (the
    ``account.FirebaseAdmin`` protocol) is optional and None until the owner supplies credentials.
    App Check comes from ``env`` (``APP_CHECK_ENFORCE``, ``FIREBASE_PROJECT_NUMBER``) or a passed
    verifier. ``places_service`` overrides the built one (tests); by default it is built from the
    Maps key and ``places_price_table`` and stays OFF while that table is unreviewed. Durable state
    is chosen as the module docstring says. Raises ``ConfigurationError`` (static text, no values)
    for any refusal.

    ``history_enabled`` and ``history_notice_version`` set the Phase 1 config (decision 25); both are
    inert until ``domain.APPROVED_HISTORY_NOTICE_VERSIONS`` lists the notice version."""
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
    app_check_policy = _app_check_policy(env, app_check_verifier)

    config = pinned_runtime_config()
    if history_enabled or history_notice_version is not None:
        config = evolve(config, history_enabled=history_enabled,
                        history_notice_version=history_notice_version)
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
    consents = InMemoryConsentStore(now, ledger=stores.consents, config=config)
    if places_service is None:
        places_service = build_places_service(
            _provider(maps_key), ledger=ledger,
            price_table=places_price_table or UNREVIEWED_PRICE_TABLE, config=config,
            kill_switch=kill_switch, transport=places_transport, now=now, sink=sink)

    # State beside the turn stores: plan versions, outcomes, edit receipts and pending actions use
    # the same backend (and the same Firestore client) as the quota and workspace stores.
    plan_stores = (firestore_plan_stores(stores.client) if stores.kind == "firestore"
                   else memory_plan_stores())
    action_store: PendingActionStore = (
        FirestorePendingActionStore(stores.client) if stores.kind == "firestore"
        else MemoryPendingActionStore())
    account_service = build_account_service(
        stores=stores, quotas=quotas, consents=consents, firebase_admin=firebase_admin,
        clock=now, sink=sink, runtime_config=config)
    register_plan_state(account_service, stores=stores, plan_stores=plan_stores,
                        action_store=action_store, clock=now)
    guard = account_service.assert_active  # an in-flight request writes nothing for a deleted uid
    if stores.kind == "firestore":
        history_store = firestore_history_store(stores.client)
        places_store, asks_store = firestore_saved_place_stores(stores.client)
    else:
        history_store = memory_history_store()
        places_store, asks_store = memory_saved_place_stores()
    history_service = HistoryService(
        history_store, consents, runtime_config=config, workspaces=stores.workspaces, clock=now,
        sink=sink, account_guard=guard)
    saved_place_service = SavedPlaceService(
        places_store, asks_store, consents, runtime_config=config, clock=now, sink=sink,
        account_guard=guard)
    register_history_state(account_service, history=history_service,
                           saved_places=saved_place_service)

    app = create_app(
        verifier=identity_verifier,
        resolver=PrincipalResolver(
            policies=quotas.policies, disabled_uids=disabled_uids if disabled_uids is not None
            else set()),
        trace_sink=sink, gateway=gateway, discovery_client=discovery,
        display_cache=cache, runtime_config=config, quotas=quotas, clock=now,
        cost_ledger=ledger, dev_scaffold=False, workspace_store=stores.workspaces,
        idempotency_store=stores.idempotency, consent_store=consents, account_guard=guard)
    orchestrator = app.state.orchestrator
    action_service = ActionService(
        action_store, quotas, workspaces=stores.workspaces, clock=now, runtime_config=config,
        trace_sink=sink, account_guard=guard,
        saved_place_sink=SavedPlaceActionSink(saved_place_service))
    workspace_service = build_workspace_service(
        stores.workspaces, plan_stores, policies=quotas.policies, consents=consents,
        runtime_config=config, clock=now, sink=sink, lock=orchestrator.workspace_lock,
        account_guard=guard, history=history_service)
    app.state.places_service = places_service
    app.state.app_check_verifier = app_check_verifier
    app.state.app_check_policy = app_check_policy
    app.state.action_service = action_service
    app.state.workspace_service = workspace_service
    app.state.account_service = account_service
    app.state.history_service = history_service
    app.state.saved_place_service = saved_place_service
    # Withdrawing a history consent deletes what it covered (the cascade also runs on retry).
    app.state.consent_withdrawal_hooks.update({
        ConsentPurpose.HISTORY_STORE: [history_service.delete_all, plan_stores.outcomes.delete_all],
        ConsentPurpose.SAVED_PLACE: [saved_place_service.delete_all, action_service.delete_save_place_records],
    })
    return Runtime(
        app=app, gateway=gateway, discovery_client=discovery, display_cache=cache,
        orchestrator=orchestrator, costs=ledger, quotas=quotas, kill_switch=kill_switch,
        quota_latch=latch, config=config, price_table=prices, stores=stores,
        places_service=places_service, action_service=action_service,
        workspace_service=workspace_service, account_service=account_service,
        plan_stores=plan_stores, history_service=history_service,
        saved_place_service=saved_place_service)
