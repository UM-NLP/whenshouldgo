from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from time import perf_counter
from uuid import uuid4

from .costs import CostLedger
from .discovery import DisplayCache, GroundingLiteClient, render_grounded_block
from pydantic import ValidationError

from .domain import (
    Confirmation,
    DiscoveryBlock,
    DomainClass,
    Fact,
    FactSource,
    Principal,
    PrincipalKind,
    RuntimeConfig,
    ErrorCode,
    ErrorReason,
    TripWorkspace,
    MeterState,
    TurnResponse,
    UsagePeriod,
    UsageView,
    TurnWarning,
    WorkspaceStatus,
    evolve,
)
from .failures import MapsContentToModelNotImplemented, ResultExpired, TurnFailure
from .gateway import ModelGateway
from .guard import OUT_OF_SCOPE_REPLY, SAFETY_REPLY, classify, estimate_tokens, tools_for
from .location import RoundedLocation
from .pipeline import DiscoveryRef, TurnPipeline, to_api_block
from .quota import QuotaService
from .telemetry import (
    AllowanceOutcome,
    PolicyEvent,
    PolicyOutcome,
    QuotaEvent,
    QuotaOutcome,
    TraceSink,
    TurnFinished,
    TurnStarted,
    VERSION_PATTERN,
    current_trace_id,
    get_default_sink,
    trace,
)

# PLACEHOLDER (owner-tunable): how long a second turn for the same tenant waits for the first to
# finish before it fails with 409 TURN_IN_PROGRESS. A little above the 20 s run deadline, so a
# normal queued turn still gets its turn but a stuck one cannot hold a worker forever.
WORKSPACE_LOCK_TIMEOUT_SECONDS = 25.0
# PLACEHOLDER (owner-tunable): turns running at once in this process. Over the cap a turn is
# refused at once (503 with Retry-After) without holding a thread. A tenant never uses more than
# two of these (one running, one waiting), see ``_workspace_lock``.
MAX_INFLIGHT_TURNS = 32
BUSY_RETRY_AFTER_SECONDS = 5

EXHAUSTED_REPLY = (
    "Your free planning allowance is used. Create a free account to continue; "
    "existing trips remain available."
)
TOO_LONG_REPLY = "Please send a shorter trip-planning request (under 2,000 tokens)."


@dataclass(frozen=True)
class TurnResult:
    reply: str
    # None when the turn was rejected before classification (e.g. over the size limit).
    domain: DomainClass | None
    tools: tuple[str, ...]
    workspace: TripWorkspace | None
    allowance_exhausted: bool = False
    # Google's grounded block, verbatim. Never stored in a replay record (see ReplayRecord).
    discovery: DiscoveryBlock | None = None
    warnings: tuple[TurnWarning, ...] = ()


@dataclass(frozen=True)
class ReplayRecord:
    """What an idempotent replay keeps. ``result`` never carries Maps content: a discovery block
    is stored only as ``ref`` (cache key, framing, the two hashes recorded at validation and the
    expiry) and is re-hydrated from the display cache on replay, or the replay is a typed
    CONFLICT (``ResultExpired``) when the entry is gone."""

    result: TurnResult
    ref: DiscoveryRef | None = None


class IdempotencyKeyReused(Exception):
    """An idempotency key was replayed with a different request body."""


def _safe_version(version: str) -> str | None:
    """Policy versions are server-authored; drop anything outside the version pattern."""
    return version if re.fullmatch(VERSION_PATTERN, version) else None


def _fingerprint(message: str) -> str:
    return sha256(message.encode("utf-8")).hexdigest()


def discovery_cache_key(idempotency_key: str) -> str:
    """Display-cache key of a turn's block: derived from the idempotency key, bounded and safe
    for the cache's identifier pattern. The owner (principal id) is a separate cache dimension."""
    return "disc-" + sha256(idempotency_key.encode("utf-8")).hexdigest()[:40]


class TurnOrchestrator:
    """In-memory development orchestrator; workspace and replay stores move to Firestore."""

    def __init__(
        self,
        quotas: QuotaService | None = None,
        sink: TraceSink | None = None,
        runtime_config_version: str | None = None,
        *,
        gateway: ModelGateway | None = None,
        discovery_client: GroundingLiteClient | None = None,
        display_cache: DisplayCache | None = None,
        runtime_config: RuntimeConfig | None = None,
        cost_ledger: CostLedger | None = None,
        clock: Callable[[], datetime] | None = None,
        dev_scaffold: bool = False,
        max_inflight_turns: int = MAX_INFLIGHT_TURNS,
    ) -> None:
        self.quotas = quotas or QuotaService()
        self.sink: TraceSink = sink or get_default_sink()
        self._runtime_config = runtime_config
        self._runtime_config_version = runtime_config_version or (
            runtime_config.version if runtime_config is not None else None
        )
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.costs = cost_ledger or CostLedger(self._clock)
        self._dev_scaffold = dev_scaffold
        if discovery_client is not None and gateway is None:
            raise ValueError("a discovery client needs a gateway: FRAME formulates its arguments")
        self._discovery = discovery_client
        self.display_cache = display_cache
        if discovery_client is not None and display_cache is None:
            self.display_cache = DisplayCache(self._clock)
        # Without a gateway nothing interprets the message. The scaffold turn (below) then runs
        # ONLY when ``dev_scaffold`` is True; otherwise such a turn is a typed 503. It exists for
        # local development and tests, never as a silent production fallback.
        self._pipeline: TurnPipeline | None = None
        if runtime_config is not None and runtime_config.maps_content_to_model is not False:
            raise MapsContentToModelNotImplemented()
        if gateway is not None:
            if runtime_config is None:
                raise ValueError("a gateway needs a runtime config")
            self._pipeline = TurnPipeline(
                gateway=gateway, discovery=discovery_client, cache=self.display_cache,
                config=runtime_config, costs=self.costs, sink=self.sink, clock=self._clock)
        self.workspaces: dict[tuple[str, str], TripWorkspace] = {}
        # (uid, idempotency key) -> (request fingerprint, replay record) for replays.
        self._committed: dict[tuple[str, str], tuple[str, ReplayRecord]] = {}
        # key -> [lock, users]. An entry exists only while a turn holds or waits for it, so the
        # dict cannot grow without bound as tenants come and go.
        self._workspace_locks: dict[tuple[str, str], list] = {}
        self._locks_guard = Lock()
        # Record keys of turns that are stored but not yet committed: invisible to replay.
        self._pending: set[tuple[str, str]] = set()
        self._max_inflight = max_inflight_turns
        self._inflight = 0

    @contextmanager
    def _inflight_slot(self) -> Iterator[None]:
        """A global in-flight cap: over it, refuse at once (503 + Retry-After), holding no thread."""
        with self._locks_guard:
            if self._inflight >= self._max_inflight:
                raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_UNAVAILABLE,
                                  retry_after=BUSY_RETRY_AFTER_SECONDS)
            self._inflight += 1
        try:
            yield
        finally:
            with self._locks_guard:
                self._inflight -= 1

    @contextmanager
    def _workspace_lock(self, key: tuple[str, str]) -> Iterator[None]:
        """The per-tenant turn lock. At most ONE turn runs and ONE waits per tenant: a further
        concurrent turn is refused at once with a typed 409 TURN_IN_PROGRESS (its caller releases
        the reservation), so one tenant cannot park many threads on its own lock. The one waiter
        gives up after WORKSPACE_LOCK_TIMEOUT_SECONDS (kept above the run deadline)."""
        with self._locks_guard:
            existing = self._workspace_locks.get(key)
            if existing is not None and existing[1] >= 2:
                raise TurnFailure(409, ErrorCode.CONFLICT, ErrorReason.TURN_IN_PROGRESS)
            entry = self._workspace_locks.setdefault(key, [Lock(), 0])
            entry[1] += 1
        acquired = False
        try:
            acquired = entry[0].acquire(timeout=WORKSPACE_LOCK_TIMEOUT_SECONDS)
            if not acquired:
                raise TurnFailure(409, ErrorCode.CONFLICT, ErrorReason.TURN_IN_PROGRESS)
            yield
        finally:
            if acquired:
                entry[0].release()
            with self._locks_guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._workspace_locks.pop(key, None)

    def _replay(self, principal: Principal, idempotency_key: str, message: str) -> TurnResult | None:
        if (principal.uid, idempotency_key) in self._pending:
            return None  # stored but not committed yet: the reservation still answers 409
        stored = self._committed.get((principal.uid, idempotency_key))
        if stored is None:
            return None
        fingerprint, record = stored
        if fingerprint != _fingerprint(message):
            raise IdempotencyKeyReused("idempotency key reused with a different request")
        return self._rehydrate(principal, record)

    def _rehydrate(self, principal: Principal, record: ReplayRecord) -> TurnResult:
        """Put a stored discovery block back from the display cache, owner-scoped, and verify the
        hashes recorded at validation. Expired, evicted or altered: typed CONFLICT, never a
        silent re-run (that would spend provider calls under a used key)."""
        ref = record.ref
        if ref is None:
            return record.result
        validated = (
            self.display_cache.get(principal.uid, ref.cache_key)
            if self.display_cache is not None else None
        )
        if (
            validated is None
            or validated.principal_id != principal.uid
            or validated.content_hash != ref.content_hash
            or validated.display_hash != ref.display_hash
        ):
            raise ResultExpired()
        block = None
        try:
            block = to_api_block(render_grounded_block(validated, ref.framing),
                                 ref.cache_expires_at, validated.answer.retrieved_at)
        except ValueError:  # an entry that no longer renders is as good as gone
            block = None
        if block is None:
            raise ResultExpired()
        return replace(record.result, discovery=block)

    def _trace(self, event_cls: type, **fields: object) -> None:
        """Telemetry never raises into the turn: failures are counted in telemetry.health()."""
        trace(event_cls, self.sink, **fields)

    def run_turn(
        self, principal: Principal, message: str, idempotency_key: str, *,
        location: RoundedLocation | None = None,
    ) -> TurnResult:
        """Run one turn and emit typed trace events (ids, enums and counts only). ``location`` is
        an already consented, already rounded foreground location (see app/location.py). It is
        used only by the pipeline for a discovery request with no usable area, is not part of the
        replay fingerprint (a replay under the same key returns the stored result whatever the
        location now is) and is stored nowhere."""
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a server-derived Principal")
        if principal.kind == PrincipalKind.DISABLED:
            raise PermissionError("principal disabled")
        trace_id = current_trace_id()
        common = {"trace_id": trace_id, "runtime_config_version": self._runtime_config_version}
        started = perf_counter()
        self._trace(
            TurnStarted,
            **common,
            principal_kind=principal.kind,
            policy_version=_safe_version(principal.policy_version),
            input_token_estimate=estimate_tokens(message),
        )
        replayed: list[bool] = []
        result = self._run_turn(principal, message, idempotency_key, common, replayed, location)
        if result.domain is None:
            outcome = PolicyOutcome.BLOCKED_TOO_LONG
        elif result.domain == DomainClass.SAFETY:
            outcome = PolicyOutcome.BLOCKED_SAFETY
        elif result.domain == DomainClass.OUT_OF_SCOPE:
            outcome = PolicyOutcome.BLOCKED_OUT_OF_SCOPE
        elif result.domain == DomainClass.MIXED:
            outcome = PolicyOutcome.BLOCKED_MIXED
        else:
            outcome = PolicyOutcome.ALLOWED
        self._trace(PolicyEvent, **common, outcome=outcome, domain=result.domain)
        if result.allowance_exhausted:
            allowance = AllowanceOutcome.EXHAUSTED
        elif result.workspace is not None:
            allowance = AllowanceOutcome.ALLOWED
        else:
            allowance = AllowanceOutcome.NOT_CHECKED
        self._trace(
            TurnFinished,
            **common,
            principal_kind=principal.kind,
            domain=result.domain,
            allowance_outcome=allowance,
            replay=bool(replayed),
            tool_count=len(result.tools),
            latency_ms=int((perf_counter() - started) * 1000),
        )
        return result

    def _run_turn(
        self,
        principal: Principal,
        message: str,
        idempotency_key: str,
        common: dict[str, str | None],
        replayed: list[bool],
        location: RoundedLocation | None = None,
    ) -> TurnResult:
        def quota_event(outcome: QuotaOutcome) -> None:
            self._trace(QuotaEvent, **common, outcome=outcome, principal_kind=principal.kind)

        replay = self._replay(principal, idempotency_key, message)
        if replay is not None:
            replayed.append(True)
            return replay

        tokens = self.quotas.policies.get(principal.policy_version).tokens
        if estimate_tokens(message) > tokens.max_user_message_tokens:
            return TurnResult(TOO_LONG_REPLY, None, (), None)

        domain = classify(message)
        if domain == DomainClass.SAFETY:
            return TurnResult(SAFETY_REPLY, domain, (), None)
        if domain == DomainClass.OUT_OF_SCOPE:
            return TurnResult(OUT_OF_SCOPE_REPLY, domain, (), None)
        if domain == DomainClass.MIXED:
            return TurnResult(
                "I can help with the trip-planning part. " + OUT_OF_SCOPE_REPLY, domain, (), None
            )
        if self._pipeline is None and not self._dev_scaffold:
            # No model gateway and no explicit dev scaffold: fail closed, before any allowance
            # is reserved or any workspace is touched.
            raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.SERVER_NOT_CONFIGURED)
        exhausted = TurnResult(EXHAUSTED_REPLY, domain, (), None, allowance_exhausted=True)
        if not self.quotas.allowed(principal):
            quota_event(QuotaOutcome.DENIED)
            return exhausted

        # Reserve atomically before touching any state. A denial here (a concurrent
        # request took the last unit) leaves the workspace untouched.
        try:
            reservation = self.quotas.reserve(principal, idempotency_key)
        except PermissionError:
            quota_event(QuotaOutcome.DENIED)
            return exhausted
        key = (principal.uid, "default")
        committed = False
        free = False  # a served turn that gave the user no result does not consume the allowance
        try:
            quota_event(QuotaOutcome.DUPLICATE if reservation.duplicate else QuotaOutcome.RESERVED)
            if reservation.duplicate:
                # A concurrent request with this key committed between our replay check and
                # reserve; it stored its result while holding the workspace lock. A committed
                # key whose replay record is gone is NEVER re-run (that would pay for the
                # pipeline again under a counted key): it is a typed RESULT_EXPIRED.
                with self._workspace_lock(key):
                    replay = self._replay(principal, idempotency_key, message)
                if replay is None:
                    raise ResultExpired()
                replayed.append(True)
                return replay
            with self._inflight_slot(), self._workspace_lock(key):
                current = self.workspaces.get(key)
                base = current or self._new_workspace(principal)
                tools = tools_for(domain, principal.kind)
                if self._pipeline is not None and domain == DomainClass.TRAVEL:
                    # Held across the model and tool calls: one in-flight turn per tenant, so
                    # the patch always applies to the version FRAME saw.
                    daily = self.quotas.policies.get(principal.policy_version).daily_tokens.get(
                        principal.kind)
                    outcome = self._pipeline.run(
                        principal, message, base, tools, discovery_cache_key(idempotency_key),
                        daily_ceiling=daily, location=location)
                    workspace = outcome.workspace
                    free = outcome.free
                    reply, block, ref, warnings = (
                        outcome.reply, outcome.discovery, outcome.ref, outcome.warnings)
                elif self._pipeline is not None:
                    # The fixed APP_HELP responder: a canned reply, no model and no tool call.
                    workspace = self._app_help_workspace(base)
                    reply, block, ref, warnings = self._reply(domain), None, None, ()
                else:
                    workspace = self._scaffold_workspace(base, message)
                    reply, block, ref, warnings = self._reply(domain), None, None, ()
                result = TurnResult(reply, domain, tools, workspace, False, block, warnings)
                self._check_contract(principal, result)  # a failure here is BEFORE any commit
                # Store the state and the replay record BEFORE the commit, then count the turn.
                # While uncommitted the record is "pending" and invisible to replay, so a
                # concurrent duplicate never gets the replay of a turn that may roll back. If the
                # commit fails without counting, the state is rolled back and the reservation is
                # released; if it counted before faulting, the turn IS committed (never
                # released): the state stays and the fault is not turned into a failure.
                record_key = (principal.uid, idempotency_key)
                stored = replace(result, discovery=None)  # never keep Maps content in a record
                self._pending.add(record_key)
                self.workspaces[key] = workspace
                self._committed[record_key] = (_fingerprint(message), ReplayRecord(stored, ref))
                try:
                    if free:
                        self.quotas.release(reservation)  # served, but not counted
                    else:
                        self.quotas.commit(reservation)
                except BaseException as fault:
                    counted = idempotency_key in self.quotas.usage_for(principal.uid).committed_keys
                    if not counted:
                        if current is None:
                            self.workspaces.pop(key, None)
                        else:
                            self.workspaces[key] = current
                        self._committed.pop(record_key, None)
                        self._pending.discard(record_key)
                        raise
                    committed = True
                    if not isinstance(fault, Exception):
                        self._pending.discard(record_key)
                        raise
                committed = True
                self._pending.discard(record_key)
        except BaseException:
            if not committed:
                self.quotas.release(reservation)
                quota_event(QuotaOutcome.RELEASED)
            raise
        # Emitted after the reservation try block: a telemetry fault cannot release a
        # committed unit or lose the replay record.
        quota_event(QuotaOutcome.RELEASED if free else QuotaOutcome.COMMITTED)
        return result

    @staticmethod
    def _new_workspace(principal: Principal) -> TripWorkspace:
        """A fresh workspace for this principal; an id the contract refuses is a typed server
        fault before any paid work (not a bare validation error)."""
        try:
            return TripWorkspace(id=str(uuid4()), owner_id=principal.uid)
        except ValidationError:
            raise TurnFailure(500, ErrorCode.PROVIDER, None) from None

    @staticmethod
    def _check_contract(principal: Principal, result: TurnResult) -> None:
        """Build the API response BEFORE committing: a result that cannot be shown is a failure
        now (released, nothing stored), never a committed record replayed as a 500 forever."""
        try:
            TurnResponse(
                reply=result.reply, domain=result.domain, allowed_tools=result.tools,
                workspace_id=result.workspace.id if result.workspace else None,
                allowance_exhausted=result.allowance_exhausted,
                policy_version=principal.policy_version, discovery=result.discovery,
                warnings=result.warnings,
                # Shape check only: the real, post-commit meter is attached by the route.
                usage=UsageView(
                    period=UsagePeriod.LIFETIME, user_turns_used=0, user_turns_limit=0,
                    activated_trips_used=0, activated_trips_limit=0, meter_state=MeterState.NONE,
                ),
            )
        except ValidationError:
            raise TurnFailure(500, ErrorCode.PROVIDER, None) from None

    @staticmethod
    def _app_help_workspace(base: TripWorkspace) -> TripWorkspace:
        """The workspace after the fixed APP_HELP responder: it makes no paid call (no model, no
        tool), stores no goal and no facts, and only advances the version."""
        return evolve(base, status=WorkspaceStatus.EXPLORING, version=base.version + 1)

    @staticmethod
    def _scaffold_workspace(base: TripWorkspace, message: str) -> TripWorkspace:
        """The development scaffold turn (no model interprets the message; only with
        ``dev_scaffold=True``)."""
        return evolve(
            base,
            # DEV-ONLY: the scaffold stores the raw user message as the goal. No non-scaffold
            # path ever does (the patch path leaves the goal alone).
            goal=message,
            status=WorkspaceStatus.EXPLORING,
            version=base.version + 1,
        )

    @staticmethod
    def _reply(domain: DomainClass) -> str:
        if domain == DomainClass.APP_HELP:
            return (
                "WhenShouldGo plans same-day local outings. Guest planning starts without a "
                "visible login and shows assumptions before it uses them."
            )
        return (
            "I can help plan same-day outings. Tell me what you'd like to do and where, for "
            'example "quiet vegetarian dinner near Union Station". What area are you starting from?'
        )
