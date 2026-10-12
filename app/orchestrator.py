from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Lock
from uuid import uuid4

from .domain import Fact, Intent, Principal, PrincipalKind, TripWorkspace, WorkspaceStatus
from .policy import OUT_OF_SCOPE_REPLY, SAFETY_REPLY, Budget, classify, estimate_tokens, tools_for
from .quota import QuotaService


@dataclass(frozen=True)
class TurnResult:
    reply: str
    intent: Intent
    tools: tuple[str, ...]
    workspace: TripWorkspace | None
    allowance_exhausted: bool = False


class TurnOrchestrator:
    def __init__(self, quotas: QuotaService | None = None, budget: Budget = Budget()) -> None:
        self.quotas = quotas or QuotaService()
        self.budget = budget
        self.workspaces: dict[tuple[str, str], TripWorkspace] = {}
        self._workspace_locks: dict[tuple[str, str], Lock] = {}
        self._locks_guard = Lock()

    def _workspace_lock(self, key: tuple[str, str]) -> Lock:
        with self._locks_guard:
            return self._workspace_locks.setdefault(key, Lock())

    def run_turn(self, principal: Principal, message: str, idempotency_key: str) -> TurnResult:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a server-derived Principal")
        if principal.kind == PrincipalKind.DISABLED:
            raise PermissionError("principal disabled")
        if estimate_tokens(message) > self.budget.message_tokens:
            return TurnResult("Please send a shorter trip-planning request (under 2,000 tokens).", Intent.OUT_OF_SCOPE, (), None)

        intent = classify(message)
        if intent == Intent.SAFETY:
            return TurnResult(SAFETY_REPLY, intent, (), None)
        if intent == Intent.OUT_OF_SCOPE:
            return TurnResult(OUT_OF_SCOPE_REPLY, intent, (), None)
        if intent == Intent.MIXED:
            return TurnResult("I can help with the trip-planning part. " + OUT_OF_SCOPE_REPLY, intent, (), None)
        exhausted = TurnResult(
            "Your free planning allowance is used. Create a free account to continue; existing trips remain available.",
            intent, (), None, True,
        )
        if not self.quotas.allowed(principal):
            return exhausted

        # Reserve atomically before touching any state. A denial here (a concurrent
        # request took the last unit) leaves the workspace untouched.
        reservation = self.quotas.reserve(principal, idempotency_key)
        key = (principal.uid, "default")
        try:
            with self._workspace_lock(key):
                current = self.workspaces.get(key)
                base = current or TripWorkspace(id=str(uuid4()), owner_id=principal.uid)
                # Build the next state on a copy; swap it in only after the commit.
                workspace = replace(
                    base,
                    goal=message,
                    status=WorkspaceStatus.EXPLORING,
                    assumptions=[Fact("planning_area", "Toronto/GTA", "server_default", confirmed=False)],
                )
                self.quotas.commit(reservation)
                self.workspaces[key] = workspace
        except BaseException:
            self.quotas.release(reservation)
            raise

        if intent == Intent.APP_HELP:
            reply = "WhenShouldGo plans same-day local outings. Guest planning starts without a visible login and shows assumptions before it uses them."
        else:
            reply = (
                "I’ll look for same-day Toronto/GTA options and keep Toronto/GTA as an editable assumption. "
                "What area are you starting from?"
            )
        return TurnResult(reply, intent, tools_for(intent, principal.kind), workspace)
