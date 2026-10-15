from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import perf_counter
from uuid import uuid4

from .domain import (
    Confirmation,
    DomainClass,
    Fact,
    FactSource,
    Principal,
    PrincipalKind,
    TripWorkspace,
    WorkspaceStatus,
    evolve,
)
from .guard import OUT_OF_SCOPE_REPLY, SAFETY_REPLY, classify, estimate_tokens, tools_for
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


class IdempotencyKeyReused(Exception):
    """An idempotency key was replayed with a different request body."""


def _safe_version(version: str) -> str | None:
    """Policy versions are server-authored; drop anything outside the version pattern."""
    return version if re.fullmatch(VERSION_PATTERN, version) else None


def _fingerprint(message: str) -> str:
    return sha256(message.encode("utf-8")).hexdigest()


class TurnOrchestrator:
    """In-memory development orchestrator; workspace and replay stores move to Firestore."""

    def __init__(
        self,
        quotas: QuotaService | None = None,
        sink: TraceSink | None = None,
        runtime_config_version: str | None = None,
    ) -> None:
        self.quotas = quotas or QuotaService()
        self.sink: TraceSink = sink or get_default_sink()
        self._runtime_config_version = runtime_config_version
        self.workspaces: dict[tuple[str, str], TripWorkspace] = {}
        # (uid, idempotency key) -> (request fingerprint, committed result) for replays.
        self._committed: dict[tuple[str, str], tuple[str, TurnResult]] = {}
        self._workspace_locks: dict[tuple[str, str], Lock] = {}
        self._locks_guard = Lock()

    def _workspace_lock(self, key: tuple[str, str]) -> Lock:
        with self._locks_guard:
            return self._workspace_locks.setdefault(key, Lock())

    def _replay(self, principal: Principal, idempotency_key: str, message: str) -> TurnResult | None:
        stored = self._committed.get((principal.uid, idempotency_key))
        if stored is None:
            return None
        fingerprint, result = stored
        if fingerprint != _fingerprint(message):
            raise IdempotencyKeyReused("idempotency key reused with a different request")
        return result

    def _trace(self, event_cls: type, **fields: object) -> None:
        """Telemetry never raises into the turn: failures are counted in telemetry.health()."""
        trace(event_cls, self.sink, **fields)

    def run_turn(self, principal: Principal, message: str, idempotency_key: str) -> TurnResult:
        """Run one turn and emit typed trace events (ids, enums and counts only)."""
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
        result = self._run_turn(principal, message, idempotency_key, common, replayed)
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
        try:
            quota_event(QuotaOutcome.DUPLICATE if reservation.duplicate else QuotaOutcome.RESERVED)
            if reservation.duplicate:
                # A concurrent request with this key committed between our replay check and
                # reserve; it stored its result while holding the workspace lock.
                with self._workspace_lock(key):
                    replay = self._replay(principal, idempotency_key, message)
                if replay is not None:
                    replayed.append(True)
                    return replay
            with self._workspace_lock(key):
                current = self.workspaces.get(key)
                base = current or TripWorkspace(id=str(uuid4()), owner_id=principal.uid)
                # Build the next state on a copy; swap it in only after the commit.
                workspace = evolve(
                    base,
                    goal=message,
                    status=WorkspaceStatus.EXPLORING,
                    version=base.version + 1,
                    facts=[
                        Fact(
                            id="planning_area",
                            key="planning_area",
                            value="Toronto/GTA",
                            source=FactSource.INFERENCE,
                            confirmation=Confirmation.UNCONFIRMED,
                            hard=False,
                            scope="trip",
                        )
                    ],
                )
                result = TurnResult(
                    self._reply(domain), domain, tools_for(domain, principal.kind), workspace
                )
                self.quotas.commit(reservation)
                self.workspaces[key] = workspace
                self._committed[(principal.uid, idempotency_key)] = (_fingerprint(message), result)
                committed = True
        except BaseException:
            if not committed:
                self.quotas.release(reservation)
                quota_event(QuotaOutcome.RELEASED)
            raise
        # Emitted after the reservation try block: a telemetry fault cannot release a
        # committed unit or lose the replay record.
        quota_event(QuotaOutcome.COMMITTED)
        return result

    @staticmethod
    def _reply(domain: DomainClass) -> str:
        if domain == DomainClass.APP_HELP:
            return (
                "WhenShouldGo plans same-day local outings. Guest planning starts without a "
                "visible login and shows assumptions before it uses them."
            )
        return (
            "I'll look for same-day Toronto/GTA options and keep Toronto/GTA as an editable "
            "assumption. What area are you starting from?"
        )
