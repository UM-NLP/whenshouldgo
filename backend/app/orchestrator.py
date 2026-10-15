from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
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


def _fingerprint(message: str) -> str:
    return sha256(message.encode("utf-8")).hexdigest()


class TurnOrchestrator:
    """In-memory development orchestrator; workspace and replay stores move to Firestore."""

    def __init__(self, quotas: QuotaService | None = None) -> None:
        self.quotas = quotas or QuotaService()
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

    def run_turn(self, principal: Principal, message: str, idempotency_key: str) -> TurnResult:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a server-derived Principal")
        if principal.kind == PrincipalKind.DISABLED:
            raise PermissionError("principal disabled")
        replay = self._replay(principal, idempotency_key, message)
        if replay is not None:
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
            return exhausted

        # Reserve atomically before touching any state. A denial here (a concurrent
        # request took the last unit) leaves the workspace untouched.
        try:
            reservation = self.quotas.reserve(principal, idempotency_key)
        except PermissionError:
            return exhausted
        key = (principal.uid, "default")
        if reservation.duplicate:
            # A concurrent request with this key committed between our replay check and
            # reserve; it stored its result while holding the workspace lock.
            with self._workspace_lock(key):
                replay = self._replay(principal, idempotency_key, message)
            if replay is not None:
                return replay
        try:
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
        except BaseException:
            self.quotas.release(reservation)
            raise
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
