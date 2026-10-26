"""Account linking, export and deletion (docs/access-tiers.md, decisions 18, 19, 26, 27).

``AccountService`` is the one object the routes call. It is built by ``build_account_service`` and
attached as ``app.state.account_service``; while that is ``None`` every account route answers a
typed 503 SERVER_NOT_CONFIGURED and ``Capabilities.can_export`` / ``can_delete`` stay false.

What it owns, all server-side (nothing here reads a uid, a tier or an owner from a request body):

* LINK. ``start_link`` issues a random one-time intent bound to the verified guest uid, expiring in
  15 minutes, idempotent by key (a replay returns the same intent id with a fresh nonce).
  ``complete_link`` asks the injected ``FirebaseAdmin`` whether the SAME uid now holds a permanent
  provider (google.com, password, emailLink), then, in one atomic store step, consumes the intent
  and records the registered account. Usage and workspaces are keyed by the unchanged uid, so they
  are preserved; no allowance is added. A credential that already belongs to another account is a
  typed COLLISION that only the explicit merge flow (``migration.py``) can resolve.
* EXPORT. Everything the server holds for the principal, user data and ids only: workspace facts
  (user text), plan-version references, outcomes, the consent ledger, usage counters, and saved
  places and outings ONLY while history is active. Never Maps content, never a coordinate other
  than a user's rounded saved position, never a token or an idempotency key.
* DELETE. A tombstone (hashed uid and timestamps, no personal data) is written FIRST so a late job
  cannot recreate the user, then every registered ``Deleter`` runs (a failure of one never stops
  the others and makes the receipt ``completed=False``; calling delete again retries), then the
  Firebase account is deleted through the admin protocol. Other stores register their deleter with
  ``service.deleters.register`` without editing this file.
* Export and delete never touch the quota ledger, so they work while the user is GATED.

Dedicated rate limiters (per uid, sliding window) guard link, export, delete and merge. Telemetry
events carry enums only.
"""
from __future__ import annotations

import hmac
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


from .consent import ConsentStore, OperationLimiter
from .domain import (
    MAX_LINK_INTENT_TTL,
    AccountLinkIntent,
    AccountLinkResult,
    DeleteReceipt,
    ExportBundle,
    ExportUsage,
    ExportWorkspace,
    Fact,
    FactSource,
    Frozen,
    LinkProvider,
    OutcomeCheckin,
    OutingRecord,
    PlanVersion,
    Principal,
    PrincipalKind,
    RuntimeConfig,
    SavedPlace,
    Tier,
    TripWorkspace,
    history_active,
)
from .migration import (
    DEFAULT_WORKSPACE,
    MigrationService,
    MigrationSource,
    WorkspaceSource,
)
from .quota import QuotaService
from .stores import StateStores
from .stores.account_store import (
    AccountRecord,
    AccountStore,
    FirestoreAccountStore,
    IntentState,
    LinkCompletion,
    LinkIntentRecord,
    MemoryAccountStore,
    nonce_digest,
    key_digest,
)
from .telemetry import (
    AccountLinkCompleted,
    AccountLinkStarted,
    DeleteRequested,
    ExportRequested,
    LinkOutcome,
    TraceSink,
    current_trace_id,
    trace,
)

PERMANENT_PROVIDERS = frozenset({"google.com", "password", "emailLink"})
_PROVIDER_IDS: dict[LinkProvider, frozenset[str]] = {
    LinkProvider.GOOGLE: frozenset({"google.com"}),
    LinkProvider.EMAIL_LINK: frozenset({"password", "emailLink"}),
}
MAX_TRACKED_DELETED = 100_000


# --- errors ------------------------------------------------------------------------------------
class AccountErrorKind(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"  # no FirebaseAdmin: link verification fails closed
    NOT_FOUND = "NOT_FOUND"  # unknown id, wrong nonce or another tenant's intent
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    WRONG_UID = "WRONG_UID"  # the refreshed token belongs to a different, non-registered uid
    ANONYMOUS_PROVIDER = "ANONYMOUS_PROVIDER"  # still anonymous: refresh the token first
    PROVIDER_MISMATCH = "PROVIDER_MISMATCH"
    COLLISION = "COLLISION"  # the credential already owns an account: explicit merge required
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    RATE_LIMITED = "RATE_LIMITED"


class AccountError(Exception):
    """Typed and static: never carries a token, a uid or user text."""

    def __init__(self, kind: AccountErrorKind, retry_after: int | None = None) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.retry_after = retry_after


# --- Firebase admin (owner TODO: real credentials) ---------------------------------------------
@dataclass(frozen=True)
class AdminAccount:
    uid: str
    provider_ids: frozenset[str]  # Firebase Auth ``provider_data`` provider ids
    disabled: bool = False


class FirebaseAdmin(Protocol):
    """The two Firebase Admin SDK calls the account flows need. No implementation ships here
    (credentials are an owner TODO, docs/runbooks/firebase-setup.md); tests use a fake and an
    unconfigured service fails closed. Implementations must not log tokens or provider data."""

    def get_account(self, uid: str) -> AdminAccount | None:
        """``auth.get_user(uid)``: None when the account does not exist."""
        ...

    def delete_user(self, uid: str) -> None:
        """``auth.delete_user(uid)``; deleting an account that no longer exists is a no-op."""
        ...


# --- deleters and export contributors ----------------------------------------------------------
Deleter = Callable[[str], int | None]


@dataclass(frozen=True)
class DeleteReport:
    results: Mapping[str, int]
    failures: tuple[str, ...]  # deleter NAMES only


class DeleterRegistry:
    """Named cascade callbacks. A store registers one with ``register(name, fn)``; ``fn(uid)``
    deletes everything it holds for ``uid`` (idempotent) and may return a count. Registering a
    name twice is refused so a replaced deleter is never silent."""

    def __init__(self) -> None:
        self._deleters: dict[str, Deleter] = {}
        self._lock = threading.Lock()

    def register(self, name: str, fn: Deleter) -> None:
        with self._lock:
            if name in self._deleters:
                raise ValueError("deleter already registered")
            self._deleters[name] = fn

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._deleters)

    def run(self, uid: str, *, exclude: frozenset[str] = frozenset()) -> DeleteReport:
        with self._lock:
            deleters = [(n, f) for n, f in self._deleters.items() if n not in exclude]
        results: dict[str, int] = {}
        failures: list[str] = []
        for name, fn in deleters:
            try:
                results[name] = int(fn(uid) or 0)
            except Exception:  # noqa: BLE001 - one failing store never stops the others
                failures.append(name)
        return DeleteReport(results, tuple(failures))


class ExportPart(Frozen):
    """What a store contributes to an export (user data and ids only)."""

    workspaces: tuple[ExportWorkspace, ...] = ()
    plan_versions: tuple[PlanVersion, ...] = ()
    outcomes: tuple[OutcomeCheckin, ...] = ()
    saved_places: tuple[SavedPlace, ...] = ()
    outings: tuple[OutingRecord, ...] = ()


ExportContributor = Callable[[str], "ExportPart"]


@dataclass(frozen=True)
class _Contributor:
    name: str
    fn: ExportContributor
    requires_history: bool


# The export contract lives in domain.py (ExportBundle, ExportWorkspace, ExportUsage); the old
# name stays importable.
AccountExportBundle = ExportBundle

ExportPart.model_rebuild()


def export_workspace_of(workspace: TripWorkspace) -> ExportWorkspace:
    facts = tuple(
        Fact.model_validate({**f.model_dump(mode="json"), "evidence_ids": [], "depends_on": []})
        for f in workspace.facts if f.source != FactSource.TOOL)
    return ExportWorkspace(
        id=workspace.id, status=workspace.status, timezone=workspace.timezone, goal=workspace.goal,
        facts=facts, active_plan_version=workspace.active_plan_version)


# --- limits ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountLimits:
    """Operations per principal per hour. OWNER-TUNABLE PLACEHOLDERS."""

    link: int = 10
    complete: int = 20
    export: int = 5
    delete: int = 5
    migration: int = 30
    window: timedelta = timedelta(hours=1)


class AccountOp(StrEnum):
    LINK = "link"
    COMPLETE = "complete"
    EXPORT = "export"
    DELETE = "delete"
    MIGRATION = "migration"


# --- the service -------------------------------------------------------------------------------
class AccountService:
    def __init__(
        self, *, store: AccountStore, quotas: QuotaService, consents: ConsentStore,
        workspaces: Any, migrations: MigrationService | None = None,
        deleters: DeleterRegistry | None = None, admin: FirebaseAdmin | None = None,
        clock: Callable[[], datetime] | None = None, sink: TraceSink | None = None,
        history_gate: Callable[[], bool] | None = None, limits: AccountLimits | None = None,
    ) -> None:
        self.store = store
        self.quotas = quotas
        self.consents = consents
        self.workspaces = workspaces
        self.deleters = deleters or DeleterRegistry()
        self.admin = admin
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sink = sink
        self._history_gate = history_gate or (lambda: False)
        self._contributors: list[_Contributor] = []
        self._deleted: set[str] = set()
        self._lock = threading.Lock()
        limits = limits or AccountLimits()
        self._limiters = {
            AccountOp.LINK: OperationLimiter(clock, max_operations=limits.link, window=limits.window),
            AccountOp.COMPLETE: OperationLimiter(
                clock, max_operations=limits.complete, window=limits.window),
            AccountOp.EXPORT: OperationLimiter(
                clock, max_operations=limits.export, window=limits.window),
            AccountOp.DELETE: OperationLimiter(
                clock, max_operations=limits.delete, window=limits.window),
            AccountOp.MIGRATION: OperationLimiter(
                clock, max_operations=limits.migration, window=limits.window),
        }
        self.migrations = migrations or MigrationService(
            store, [WorkspaceSource(workspaces)], self._erase_guest, clock)

    # -- plumbing
    def throttle(self, op: AccountOp, uid: str) -> None:
        wait = self._limiters[op].check(uid)
        if wait is not None:
            raise AccountError(AccountErrorKind.RATE_LIMITED, wait)

    def register_export(self, name: str, fn: ExportContributor, *,
                        requires_history: bool = False) -> None:
        """Let another store add its user data to the export (history stores pass
        ``requires_history=True``: they are only read while history is active)."""
        if any(c.name == name for c in self._contributors):
            raise ValueError("export contributor already registered")
        self._contributors.append(_Contributor(name, fn, requires_history))

    def register_migration_source(self, source: MigrationSource) -> None:
        self.migrations.add_source(source)

    def _emit(self, event_cls: type, **fields: object) -> None:
        trace(event_cls, self._sink, trace_id=current_trace_id(), **fields)

    # -- tombstones: a deleted uid is never served again
    def is_deleted(self, uid: str) -> bool:
        """True once deletion was requested (even if it is still being retried). FAILS CLOSED: an
        unreadable tombstone store reads as deleted."""
        with self._lock:
            if uid in self._deleted:
                return True
        try:
            tombstone = self.store.get_tombstone(uid)
        except Exception:  # noqa: BLE001
            return True
        if tombstone is not None:
            self._remember_deleted(uid)
            return True
        return False

    def assert_active(self, uid: str) -> None:
        """For late jobs (a reminder, a sweeper, a retry) before they write anything for ``uid``."""
        if self.is_deleted(uid):
            raise AccountError(AccountErrorKind.NOT_FOUND)

    def _remember_deleted(self, uid: str) -> None:
        with self._lock:
            if len(self._deleted) < MAX_TRACKED_DELETED:
                self._deleted.add(uid)

    # -- link
    def start_link(
        self, principal: Principal, provider: LinkProvider, idempotency_key: str | None
    ) -> AccountLinkIntent:
        if principal.kind != PrincipalKind.GUEST:
            raise AccountError(AccountErrorKind.ALREADY_REGISTERED)
        now = self._clock()
        intent_id = f"link-{secrets.token_urlsafe(18)}"
        nonce = secrets.token_urlsafe(24)
        candidate = LinkIntentRecord(
            id=intent_id, owner_uid=principal.uid, provider=provider,
            nonce_hash=nonce_digest(nonce), created_at=now, expires_at=now + MAX_LINK_INTENT_TTL,
            key_hash=key_digest(idempotency_key) if idempotency_key else None)
        record, _replayed = self.store.issue_intent(candidate, now)
        self._emit(AccountLinkStarted, provider=provider)
        return AccountLinkIntent(
            id=record.id, provider=record.provider, nonce=nonce, created_at=record.created_at,
            expires_at=record.expires_at)

    def complete_link(self, principal: Principal, intent_id: str, nonce: str) -> AccountLinkResult:
        intent_provider = LinkProvider.GOOGLE
        try:
            intent_provider, result = self._complete(principal, intent_id, nonce)
        except AccountError as error:
            outcome = {
                AccountErrorKind.EXPIRED: LinkOutcome.EXPIRED,
                AccountErrorKind.COLLISION: LinkOutcome.ALREADY_LINKED,
                AccountErrorKind.CONSUMED: LinkOutcome.ALREADY_LINKED,
            }.get(error.kind, LinkOutcome.REJECTED)
            known = self.store.get_intent(intent_id)
            self._emit(AccountLinkCompleted,
                       provider=known.provider if known else intent_provider, outcome=outcome)
            raise
        self._emit(AccountLinkCompleted, provider=intent_provider, outcome=LinkOutcome.LINKED)
        return result

    def _complete(
        self, principal: Principal, intent_id: str, nonce: str
    ) -> tuple[LinkProvider, AccountLinkResult]:
        if self.admin is None:
            raise AccountError(AccountErrorKind.NOT_CONFIGURED)
        now = self._clock()
        digest = nonce_digest(nonce)
        intent = self.store.get_intent(intent_id)
        if intent is None or not hmac.compare_digest(intent.nonce_hash, digest):
            raise AccountError(AccountErrorKind.NOT_FOUND)
        if intent.state == IntentState.CONSUMED:
            raise AccountError(AccountErrorKind.CONSUMED)
        if now >= intent.expires_at:
            raise AccountError(AccountErrorKind.EXPIRED)
        account = self._admin_account(principal.uid)
        permanent = account.provider_ids & PERMANENT_PROVIDERS if account else frozenset()
        if principal.uid != intent.owner_uid:
            # Another uid presents a valid intent (id AND nonce): either the credential belongs to
            # an existing registered account (explicit merge) or this is simply the wrong uid.
            if (principal.kind == PrincipalKind.REGISTERED_FREE and account is not None
                    and not account.disabled and permanent):
                self.store.park_collision(intent_id, digest, principal.uid, now)
                raise AccountError(AccountErrorKind.COLLISION)
            raise AccountError(AccountErrorKind.WRONG_UID)
        if principal.kind != PrincipalKind.REGISTERED_FREE or account is None or account.disabled:
            raise AccountError(AccountErrorKind.ANONYMOUS_PROVIDER)
        if not permanent:
            raise AccountError(AccountErrorKind.ANONYMOUS_PROVIDER)
        if not (permanent & _PROVIDER_IDS[intent.provider]):
            raise AccountError(AccountErrorKind.PROVIDER_MISMATCH)
        outcome, _record = self.store.complete_link(
            intent_id, digest, AccountRecord(principal.uid, intent.provider, now), now)
        if outcome != LinkCompletion.LINKED:
            raise AccountError({
                LinkCompletion.NOT_FOUND: AccountErrorKind.NOT_FOUND,
                LinkCompletion.EXPIRED: AccountErrorKind.EXPIRED,
                LinkCompletion.CONSUMED: AccountErrorKind.CONSUMED,
                LinkCompletion.COLLISION: AccountErrorKind.COLLISION,
            }[outcome])
        # The ledger is keyed by the unchanged uid: counts and workspaces are preserved; this only
        # starts the registered rolling window. No allowance is added.
        self.quotas.ensure_record(principal)
        return intent.provider, AccountLinkResult(linked=True, tier=Tier.REGISTERED)

    def reconcile(self, principal: Principal) -> bool:
        """Bootstrap hook: a principal whose token already shows a permanent provider but has no
        account record (a link whose completion was lost) is recorded now. True when it created
        the record. Idempotent; never raises for an unreachable admin."""
        if principal.kind != PrincipalKind.REGISTERED_FREE or self.admin is None:
            return False
        try:
            if self.store.get_account(principal.uid) is not None:
                return False
            account = self.admin.get_account(principal.uid)
            if account is None or account.disabled:
                return False
            for provider, ids in _PROVIDER_IDS.items():
                if account.provider_ids & ids:
                    _record, created = self.store.ensure_account(
                        AccountRecord(principal.uid, provider, self._clock()))
                    self.quotas.ensure_record(principal)
                    return created
        except Exception:  # noqa: BLE001
            return False
        return False

    def _admin_account(self, uid: str) -> AdminAccount | None:
        assert self.admin is not None
        try:
            account = self.admin.get_account(uid)
        except Exception:  # noqa: BLE001 - an unreachable admin fails closed
            raise AccountError(AccountErrorKind.NOT_CONFIGURED) from None
        if account is not None and account.uid != uid:
            return None
        return account

    # -- export
    def export(self, principal: Principal) -> AccountExportBundle:
        """Everything the server holds for ``principal``. Reads only: no allowance is consumed, so
        it works while the user is gated."""
        uid = principal.uid
        now = self._clock()
        self._emit(ExportRequested, principal_kind=principal.kind)
        history = bool(self._history_gate())
        workspaces: list[ExportWorkspace] = []
        seen: set[str] = set()
        names = (DEFAULT_WORKSPACE, *self.migrations.copied_targets(uid))
        for name in names:
            workspace = self.workspaces.get(uid, name, now)
            if workspace is not None and workspace.owner_id == uid and name not in seen:
                seen.add(name)
                workspaces.append(export_workspace_of(workspace))
        parts = [self._contribution(c, uid) for c in self._contributors
                 if history or not c.requires_history]
        usage = self.quotas.usage_for(uid)
        saved = tuple(p for part in parts for p in part.saved_places) if history else ()
        outings = tuple(o for part in parts for o in part.outings) if history else ()
        return AccountExportBundle(
            generated_at=now, tier=Tier.GUEST if principal.kind == PrincipalKind.GUEST
            else Tier.REGISTERED, history_active=history,
            consents=tuple(self.consents.ledger(uid)), saved_places=saved, outings=outings,
            workspaces=(*workspaces, *(w for part in parts for w in part.workspaces)),
            plan_versions=tuple(p for part in parts for p in part.plan_versions),
            outcomes=tuple(o for part in parts for o in part.outcomes),
            usage=ExportUsage(
                successful_turns=usage.successful_turns, activated_trips=usage.activated_trips,
                window_started_at=usage.window_started_at))

    @staticmethod
    def _contribution(contributor: _Contributor, uid: str) -> ExportPart:
        part = contributor.fn(uid)
        for record in (*part.outings,):
            if record.owner_id != uid:
                raise ValueError("an export contributor returned another tenant's record")
        return part

    # -- delete
    def delete(self, principal: Principal) -> DeleteReceipt:
        """Cascade-delete everything for ``principal``. Idempotent and retryable."""
        self._emit(DeleteRequested, principal_kind=principal.kind)
        now = self._clock()
        tombstone = self.store.begin_tombstone(principal.uid, now)  # first: no late recreation
        self._remember_deleted(principal.uid)
        if tombstone.completed_at is None:
            completed = self._erase(principal.uid, frozenset())
        else:
            completed = True
        return DeleteReceipt(requested_at=tombstone.requested_at, completed=completed)

    def _erase(self, uid: str, exclude: frozenset[str]) -> bool:
        report = self.deleters.run(uid, exclude=exclude)
        if report.failures:
            return False
        if self.admin is not None:
            try:
                self.admin.delete_user(uid)
            except Exception:  # noqa: BLE001 - the data is gone; a retry deletes the login
                return False
        self.store.complete_tombstone(uid, self._clock())
        return True

    def _erase_guest(self, uid: str) -> bool:
        """The merge's cleanup: the guest's whole cascade, but not the merge journal itself."""
        self.store.begin_tombstone(uid, self._clock())
        self._remember_deleted(uid)
        report = self.deleters.run(uid, exclude=frozenset({"account_migrations"}))
        if report.failures:
            return False
        if self.admin is not None:
            try:
                self.admin.delete_user(uid)
            except Exception:  # noqa: BLE001 - best effort: the tombstone already blocks the uid
                pass
        self.store.complete_tombstone(uid, self._clock())
        return True


# --- store deleters ----------------------------------------------------------------------------
def _sweep_owner(client: Any, collection: str) -> Deleter:
    def delete(uid: str) -> int:
        removed = 0
        while True:
            rows = list(client.collection(collection).where("owner_id", "==", uid).limit(200).stream())
            if not rows:
                return removed
            for snap in rows:
                snap.reference.delete()
                removed += 1

    return delete


def register_owner_collection(registry: DeleterRegistry, client: Any, name: str,
                              collection: str) -> None:
    """Generic Firestore hook: delete every document of ``collection`` whose ``owner_id`` is the
    uid (the rule every store document follows). A future store registers itself with one call."""
    registry.register(name, _sweep_owner(client, collection))


def register_state_deleters(
    registry: DeleterRegistry, stores: StateStores, account_store: AccountStore,
    client: Any = None,
) -> None:
    """Deleters for quota, consents, workspaces, idempotency records and the display cache, for
    either backend, plus the account store's own records."""
    from .stores import firestore as fs
    from .stores import memory as mem

    if client is not None:
        for name, collection in (
            ("quota", fs.QUOTA), ("consents", fs.CONSENT_LEDGERS),
            ("workspaces", fs.WORKSPACES), ("idempotency", fs.IDEMPOTENCY),
        ):
            register_owner_collection(registry, client, name, collection)
    else:
        quota, ledger = stores.quota, stores.consents
        workspaces, idempotency = stores.workspaces, stores.idempotency
        if not (isinstance(quota, mem.MemoryQuotaStore) and isinstance(ledger, mem.MemoryConsentLedger)
                and isinstance(workspaces, mem.MemoryWorkspaceStore)
                and isinstance(idempotency, mem.MemoryIdempotencyStore)):
            raise ValueError("a non-memory state store needs its Firestore client for deletion")

        def delete_quota(uid: str) -> int:
            with quota._lock:  # noqa: SLF001 - same package, one atomic removal
                return 1 if quota.usage.pop(uid, None) is not None else 0

        def delete_consents(uid: str) -> int:
            with ledger._lock:  # noqa: SLF001
                return len(ledger._ledger.pop(uid, ()))  # noqa: SLF001

        def delete_workspaces(uid: str) -> int:
            names = [n for (owner, n) in list(workspaces.items) if owner == uid]
            for name in names:
                workspaces.delete(uid, name)
            return len(names)

        def delete_idempotency(uid: str) -> int:
            keys = [k for (owner, k) in list(idempotency.items) if owner == uid]
            for key in keys:
                idempotency.rollback(uid, key)
            return len(keys)

        registry.register("quota", delete_quota)
        registry.register("consents", delete_consents)
        registry.register("workspaces", delete_workspaces)
        registry.register("idempotency", delete_idempotency)
    registry.register("display_cache", stores.display_cache.delete_owner)
    registry.register("account_records", account_store.delete_accounts)
    registry.register("account_migrations", account_store.delete_migrations)


def register_plan_state(
    service: AccountService, *, stores: StateStores, plan_stores: Any, action_store: Any,
    clock: Callable[[], datetime],
) -> None:
    """Deleters for plan versions, outcomes, edit receipts and pending actions, plus the export
    contributor for plan versions and outcomes (both backends: each store knows its own delete)."""
    service.deleters.register("plans", plan_stores.plans.delete_owner)
    service.deleters.register("outcomes", plan_stores.outcomes.delete_all)
    service.deleters.register("edit_receipts", plan_stores.receipts.delete_owner)
    service.deleters.register("pending_actions", action_store.delete_owner)

    def contribute(uid: str) -> ExportPart:
        now = clock()
        versions: list[PlanVersion] = []
        seen: set[str] = set()
        for name in (DEFAULT_WORKSPACE, *service.migrations.copied_targets(uid)):
            workspace = stores.workspaces.get(uid, name, now)
            if workspace is not None and workspace.owner_id == uid and workspace.id not in seen:
                seen.add(workspace.id)
                versions.extend(plan_stores.plans.history(uid, workspace.id, now))
        outcomes = tuple(
            OutcomeCheckin(place_id=e.place_id, outcome=e.outcome, note=e.note)
            for e in plan_stores.outcomes.entries(uid, now))
        return ExportPart(plan_versions=tuple(versions), outcomes=outcomes)

    # Not gated by history: outcomes exist only because history was once active and consented to,
    # and the user can export them whatever the flag says now.
    service.register_export("plans_outcomes", contribute)


# --- composition -------------------------------------------------------------------------------
def build_account_service(
    *, stores: StateStores, quotas: QuotaService, consents: ConsentStore,
    firebase_client: Any = None, firebase_admin: FirebaseAdmin | None = None,
    clock: Callable[[], datetime] | None = None, sink: TraceSink | None = None,
    runtime_config: Callable[[], RuntimeConfig | None] | RuntimeConfig | None = None,
    limits: AccountLimits | None = None, account_store: AccountStore | None = None,
) -> AccountService:
    """Compose the service over the same ``StateStores`` the app uses.

    ``firebase_client`` is the Firestore client for ``stores.kind == "firestore"`` (when omitted it
    is taken from the quota adapter, which holds it); the in-memory backend needs none.
    ``runtime_config`` (a value or a zero-argument callable) decides, through ``history_active``,
    whether history data is exported. ``firebase_admin`` is optional: without it the service still
    exports, deletes server data and migrates, but ``complete_link`` fails closed (503) and the
    Firebase login itself is not deleted (owner TODO)."""
    client = None
    if stores.kind == "firestore":
        client = firebase_client or getattr(stores.quota, "_client", None)
        if client is None:
            raise ValueError("the Firestore client is required to build the account service")
    if account_store is None:
        account_store = FirestoreAccountStore(client) if client is not None else MemoryAccountStore()
    registry = DeleterRegistry()
    register_state_deleters(registry, stores, account_store, client)
    config_of = runtime_config if callable(runtime_config) else (lambda: runtime_config)
    return AccountService(
        store=account_store, quotas=quotas, consents=consents, workspaces=stores.workspaces,
        deleters=registry, admin=firebase_admin, clock=clock, sink=sink,
        history_gate=lambda: history_active(config_of()), limits=limits)


__all__ = [
    "AccountError", "AccountErrorKind", "AccountExportBundle", "AccountLimits", "AccountOp",
    "AccountService", "AdminAccount", "DeleteReport", "Deleter", "DeleterRegistry",
    "ExportPart", "ExportUsage", "ExportWorkspace", "FirebaseAdmin", "build_account_service",
    "export_workspace_of", "register_owner_collection", "register_plan_state",
    "register_state_deleters",
]
