"""Trip Board server: workspace read, validated user edit, immutable plan versions, outcomes.

The tenant is always the verified principal. Nothing here reads, stores or returns Maps content:
candidates are place IDs plus the user's own labels; grounded evidence is a pointer into the
display cache that may expire (decisions 18 and 23). Errors carry static, server-authored text.

* ``read`` returns the principal's current workspace, creating an empty one on first access
  (version 0, written once; a read never refreshes a guest's inactivity timer).
* ``edit`` applies a user edit (``patching.apply_user_edit``) under the per-tenant lock: replay by
  idempotency key, optimistic concurrency by the workspace version, then a NEW immutable plan
  version when the edit selects another candidate place or changes what the plan depends on.
* ``create_plan_version`` is the one way a version is made (the turn pipeline calls it later).
* ``record_outcome`` stores a check-in server-side only for a REGISTERED principal while history
  is active and the HISTORY_STORE consent is held; otherwise it stores nothing and the ack tells
  the client to keep it on the device (decision 19).
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .consent import ConsentStore
from .domain import (
    Confirmation,
    ConsentPurpose,
    ErrorCode,
    ErrorReason,
    Fact,
    FactSource,
    OutcomeAck,
    OutcomeCheckin,
    PlanDestination,
    PlanVersion,
    PlanWarning,
    Principal,
    PrincipalKind,
    ReplacedReason,
    RuntimeConfig,
    TripWorkspace,
    WorkspaceEditRequest,
    WorkspaceStatus,
    WorkspaceView,
    evolve,
    history_active,
)
from .entitlements import PolicyRegistry
from .patching import (
    SELECTED_LABEL_KEY,
    SELECTED_PLACE_KEY,
    SELECTION_KEYS,
    PatchRejected,
    StaleEdit,
    apply_user_edit,
)
from .stores.plans_store import EditReceipt, PlanConflict, PlanStores
from .stores.protocols import WorkspaceStore
from .telemetry import (
    EditOutcome,
    OutcomeCheckinEvent,
    TraceSink,
    WorkspaceEdit,
    current_trace_id,
    get_default_sink,
    trace,
)
from .textsafety import plain_text_problem

WORKSPACE_NAME = "default"
MAX_NOTE_CHARS = 280  # mirrors OutcomeCheckin.note
_EARLY_OR_LIVE = frozenset({
    WorkspaceStatus.CAPTURED, WorkspaceStatus.FRAMING, WorkspaceStatus.EXPLORING,
    WorkspaceStatus.PROPOSED, WorkspaceStatus.CONFIRMED, WorkspaceStatus.ARMED,
    WorkspaceStatus.ACTIVE, WorkspaceStatus.ADAPTING,
})
_UTC = timezone.utc


class WorkspaceError(Exception):
    """A typed refusal. ``message`` is static (plus a version number); never user content."""

    status = 500
    code = ErrorCode.PROVIDER
    reason: ErrorReason | None = None
    default_message = "workspace unavailable"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class WorkspaceNotConfigured(WorkspaceError):
    status = 503
    reason = ErrorReason.SERVER_NOT_CONFIGURED
    default_message = "workspace service is not configured"


class WorkspaceConflict(WorkspaceError):
    status = 409
    code = ErrorCode.CONFLICT

    def __init__(self, current_version: int) -> None:
        super().__init__(f"workspace version conflict; current version {current_version}")
        self.current_version = current_version


class IdempotencyKeyReused(WorkspaceError):
    status = 409
    code = ErrorCode.CONFLICT
    default_message = "idempotency key reused with a different request"


class EditRejected(WorkspaceError):
    status = 422
    code = ErrorCode.VALIDATION
    default_message = "edit not accepted"

    def __init__(self, validation_reason: object = None) -> None:
        super().__init__()
        self.validation_reason = validation_reason  # a telemetry enum, never text


class NoteRejected(WorkspaceError):
    status = 422
    code = ErrorCode.VALIDATION
    default_message = "note not accepted"


class WorkspaceBusy(WorkspaceError):
    status = 409
    code = ErrorCode.CONFLICT
    default_message = "workspace is busy"


# --- result types ----------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace: TripWorkspace
    latest_plan: PlanVersion | None
    plan_versions: tuple[int, ...]
    replayed: bool = False

    @property
    def warnings(self) -> tuple[PlanWarning, ...]:
        return self.latest_plan.warnings if self.latest_plan is not None else ()


_EXTRA_VIEW_FIELDS = ("latest_plan", "warnings", "plan_versions")


def to_view(snapshot: WorkspaceSnapshot) -> WorkspaceView:
    """The client-facing view. Fields ``WorkspaceView`` does not declare yet (``latest_plan``,
    ``warnings``, ``plan_versions``) are included as soon as the contract gains them."""
    ws = snapshot.workspace
    fields: dict[str, Any] = {
        "id": ws.id, "status": ws.status, "version": ws.version, "goal": ws.goal,
        "facts": tuple(ws.facts), "pending_actions": tuple(ws.pending_actions)}
    extra = {"latest_plan": snapshot.latest_plan, "warnings": snapshot.warnings,
             "plan_versions": snapshot.plan_versions}
    declared = WorkspaceView.model_fields
    fields.update({k: v for k, v in extra.items() if k in declared})
    return WorkspaceView(**fields)


# --- per-tenant lock -------------------------------------------------------------------------
class _KeyedLocks:
    """Fallback lock when none is injected: one lock per tenant, per process."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], list[Any]] = {}

    @contextmanager
    def __call__(self, key: tuple[str, str]):  # noqa: ANN204
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        acquired = entry[0].acquire(timeout=30)
        try:
            if not acquired:
                raise WorkspaceBusy()
            yield
        finally:
            if acquired:
                entry[0].release()
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._locks.pop(key, None)


LockFactory = Callable[[tuple[str, str]], AbstractContextManager[None]]


# --- helpers ---------------------------------------------------------------------------------
def _fact_value(workspace: TripWorkspace, key: str) -> object | None:
    for fact in workspace.facts:
        if fact.key.casefold() == key:
            return fact.value
    return None


def _selection(workspace: TripWorkspace) -> tuple[str | None, str | None]:
    place, label = _fact_value(workspace, SELECTED_PLACE_KEY), _fact_value(workspace, SELECTED_LABEL_KEY)
    return (place if isinstance(place, str) else None, label if isinstance(label, str) else None)


def _constraint_ids(workspace: TripWorkspace) -> tuple[str, ...]:
    """Facts the user confirmed as hard needs (the selection itself is not a constraint)."""
    return tuple(
        f.id for f in workspace.facts
        if f.hard and f.confirmation == Confirmation.CONFIRMED
        and f.source != FactSource.INFERENCE and f.key.casefold() not in SELECTION_KEYS)


def _assumption_ids(workspace: TripWorkspace) -> tuple[str, ...]:
    return tuple(f.id for f in workspace.assumptions)


def _fingerprint(request: WorkspaceEditRequest) -> str:
    return hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()


def _origin_ref(workspace_id: str, version: int) -> str:
    # An opaque, non-location token: Phase 0 captures no origin for a user-edit version.
    return "o" + hashlib.sha256(f"{workspace_id}:{version}".encode()).hexdigest()[:23]


# --- the service -----------------------------------------------------------------------------
class WorkspaceService:
    def __init__(
        self, workspaces: WorkspaceStore, stores: PlanStores, *, policies: PolicyRegistry,
        consents: ConsentStore | None = None,
        runtime_config: RuntimeConfig | Callable[[], RuntimeConfig | None] | None = None,
        clock: Callable[[], datetime] | None = None, sink: TraceSink | None = None,
        lock: LockFactory | None = None,
    ) -> None:
        self._workspaces = workspaces
        self._stores = stores
        self._policies = policies
        self._consents = consents
        self._config = runtime_config
        self._clock = clock or (lambda: datetime.now(_UTC))
        self._sink = sink
        self._lock: LockFactory = lock or _KeyedLocks()

    # -- internals --
    def _ttl(self, principal: Principal) -> timedelta | None:
        """A guest's workspace (and its plan versions) expire after N inactive days (decision 27)."""
        if principal.kind != PrincipalKind.GUEST:
            return None
        return timedelta(days=self._policies.get(principal.policy_version)
                         .guest_workspace_retention_days)

    def _trace(self, event_cls: type, **fields: object) -> None:
        trace(event_cls, self._sink or get_default_sink(), trace_id=current_trace_id(), **fields)

    def _runtime_config(self) -> RuntimeConfig | None:
        return self._config() if callable(self._config) else self._config

    def _snapshot(self, principal: Principal, workspace: TripWorkspace, now: datetime,
                  replayed: bool = False) -> WorkspaceSnapshot:
        plans = self._stores.plans.history(principal.uid, workspace.id, now)
        return WorkspaceSnapshot(workspace, plans[-1] if plans else None,
                                 tuple(p.version for p in plans), replayed)

    def _current(self, principal: Principal, now: datetime) -> tuple[TripWorkspace, bool]:
        existing = self._workspaces.get(principal.uid, WORKSPACE_NAME, now)
        if existing is not None:
            return existing, False
        return TripWorkspace(id=str(uuid4()), owner_id=principal.uid), True

    # -- reads --
    def read(self, principal: Principal) -> WorkspaceSnapshot:
        now = self._clock()
        with self._lock((principal.uid, WORKSPACE_NAME)):
            workspace, created = self._current(principal, now)
            if created:
                self._workspaces.put(principal.uid, WORKSPACE_NAME, workspace, now,
                                     self._ttl(principal))
            return self._snapshot(principal, workspace, now)

    def plan_version(self, principal: Principal, version: int) -> PlanVersion | None:
        """A stored version stays readable after it is replaced. Tenant from the principal."""
        now = self._clock()
        workspace = self._workspaces.get(principal.uid, WORKSPACE_NAME, now)
        if workspace is None:
            return None
        return self._stores.plans.get(principal.uid, workspace.id, version, now)

    # -- plan versions --
    def create_plan_version(
        self, principal: Principal, workspace: TripWorkspace, *, destination: PlanDestination,
        reason: ReplacedReason | None = ReplacedReason.USER_EDIT, origin_ref: str | None = None,
        grounded_evidence_id: str | None = None, route_evidence_id: str | None = None,
        warnings: tuple[PlanWarning, ...] = (),
    ) -> PlanVersion:
        """Append a NEW immutable version for ``workspace`` and return it. The first version has no
        parent or reason; a later one needs the reason it replaced its parent. The workspace is
        not changed here (see ``with_plan``); a stored version is never edited."""
        if workspace.owner_id != principal.uid:
            raise WorkspaceError("workspace belongs to another tenant")
        now = self._clock()
        latest = self._stores.plans.latest(principal.uid, workspace.id, now)
        version = latest.version + 1 if latest else 1
        assumptions = _assumption_ids(workspace)
        constraints = _constraint_ids(workspace)
        notes = set(warnings)
        if assumptions:
            notes.add(PlanWarning.ASSUMPTIONS_UNCONFIRMED)
        if constraints:
            notes.add(PlanWarning.HARD_NEED_UNVERIFIED)  # Phase 0 verifies no hard need
        try:
            plan = PlanVersion(
                id=f"{workspace.id}.v{version}", workspace_id=workspace.id, version=version,
                parent_version=latest.version if latest else None,
                replaced_reason=(reason or ReplacedReason.USER_EDIT) if latest else None,
                destination=destination,
                origin_ref=origin_ref or _origin_ref(workspace.id, version),
                user_constraint_fact_ids=constraints, assumption_fact_ids=assumptions,
                grounded_evidence_id=grounded_evidence_id, route_evidence_id=route_evidence_id,
                warnings=tuple(sorted(notes)), created_at=now)
        except ValidationError:
            raise EditRejected() from None
        try:
            self._stores.plans.append(principal.uid, plan, now, self._ttl(principal))
        except PlanConflict:
            raise WorkspaceConflict(workspace.version) from None
        return plan

    @staticmethod
    def with_plan(workspace: TripWorkspace, plan: PlanVersion) -> TripWorkspace:
        """The workspace pointing at ``plan``: PROPOSED again (a changed plan needs a fresh
        confirmation), evidence dropped when the destination changed, and pending actions bound to
        an older version dropped (they would act on a plan that is gone)."""
        old = workspace.active_plan_version
        return evolve(
            workspace, active_plan_version=plan.version, status=WorkspaceStatus.PROPOSED,
            pending_actions=[a for a in workspace.pending_actions
                             if a.plan_version == plan.version],
            evidence=[] if old != plan.version else workspace.evidence)

    # -- user edit --
    def edit(self, principal: Principal, request: WorkspaceEditRequest,
             idempotency_key: str) -> WorkspaceSnapshot:
        now = self._clock()
        fingerprint = _fingerprint(request)
        with self._lock((principal.uid, WORKSPACE_NAME)):
            receipt = self._stores.receipts.get(principal.uid, idempotency_key, now)
            if receipt is not None:
                if receipt.fingerprint != fingerprint:
                    raise IdempotencyKeyReused()
                workspace, _ = self._current(principal, now)
                return self._snapshot(principal, workspace, now, replayed=True)
            workspace, _created = self._current(principal, now)
            try:
                result = apply_user_edit(workspace, request)
            except StaleEdit as stale:
                self._trace(WorkspaceEdit, upserts=len(request.upsert_facts),
                            removals=len(request.remove_fact_ids), outcome=EditOutcome.CONFLICT)
                raise WorkspaceConflict(stale.current) from None
            except PatchRejected as rejected:
                self._trace(WorkspaceEdit, upserts=len(request.upsert_facts),
                            removals=len(request.remove_fact_ids), outcome=EditOutcome.REJECTED)
                raise EditRejected(rejected.reason) from None
            updated = result.workspace
            plan = self._plan_for_edit(principal, workspace, updated, result.changed_ids)
            if plan is not None:
                updated = self.with_plan(updated, plan)
            self._workspaces.put(principal.uid, WORKSPACE_NAME, updated, now, self._ttl(principal))
            self._stores.receipts.put(principal.uid, idempotency_key,
                                      EditReceipt(fingerprint, updated.version), now)
            self._trace(WorkspaceEdit, upserts=result.upserts, removals=result.removals,
                        outcome=EditOutcome.APPLIED)
            return self._snapshot(principal, updated, now)

    def _plan_for_edit(self, principal: Principal, before: TripWorkspace, after: TripWorkspace,
                       changed_ids: frozenset[str]) -> PlanVersion | None:
        """A new version only when the user selected another candidate (or relabelled it), or
        changed a fact the live plan depends on. Otherwise the plan is untouched."""
        now = self._clock()
        latest = self._stores.plans.latest(principal.uid, after.id, now)
        place, label = _selection(after)
        if place is not None and (place, label) != _selection(before):
            destination = self._destination(place, label)
            return self.create_plan_version(
                principal, after, destination=destination, reason=ReplacedReason.USER_EDIT)
        if latest is None or after.status not in _EARLY_OR_LIVE:
            return None
        previous = set(latest.user_constraint_fact_ids) | set(latest.assumption_fact_ids)
        now_ids = set(_constraint_ids(after)) | set(_assumption_ids(after))
        touched = {i.casefold() for i in previous} & changed_ids
        if now_ids != previous or touched:
            return self.create_plan_version(
                principal, after, destination=latest.destination,
                reason=ReplacedReason.CONSTRAINT_CHANGED,
                grounded_evidence_id=None, route_evidence_id=None)
        return None

    @staticmethod
    def _destination(place_id: str, label: str | None) -> PlanDestination:
        if label is not None and plain_text_problem(label):
            raise EditRejected()
        try:
            return PlanDestination(place_id=place_id, user_label=label)
        except ValidationError:
            raise EditRejected() from None

    # -- outcomes --
    def record_outcome(self, principal: Principal, checkin: OutcomeCheckin,
                       idempotency_key: str | None = None) -> OutcomeAck:
        """Validate the note, then store server-side only when the principal is REGISTERED, history
        is active and HISTORY_STORE consent is held. Otherwise nothing durable is written and
        ``recorded`` is False: the client keeps the check-in on the device."""
        if checkin.note is not None:
            if len(checkin.note) > MAX_NOTE_CHARS or plain_text_problem(
                    checkin.note, allow_joiners=True):
                raise NoteRejected()
        stored = self._may_store(principal)
        if stored:
            key = idempotency_key or hashlib.sha256(
                f"{checkin.place_id}\x1f{checkin.outcome.value}\x1f{checkin.note or ''}"
                .encode()).hexdigest()
            self._stores.outcomes.record(principal.uid, key, checkin, self._clock())
        self._trace(OutcomeCheckinEvent, outcome=checkin.outcome, has_note=checkin.note is not None)
        return OutcomeAck(recorded=stored)

    def _may_store(self, principal: Principal) -> bool:
        if principal.kind != PrincipalKind.REGISTERED_FREE:
            return False
        if not history_active(self._runtime_config()):
            return False
        return self._consents is not None and self._consents.has_active(
            principal.uid, ConsentPurpose.HISTORY_STORE)


def build_workspace_service(
    workspaces: WorkspaceStore, stores: PlanStores, *, policies: PolicyRegistry,
    consents: ConsentStore | None = None,
    runtime_config: RuntimeConfig | Callable[[], RuntimeConfig | None] | None = None,
    clock: Callable[[], datetime] | None = None, sink: TraceSink | None = None,
    lock: LockFactory | None = None,
) -> WorkspaceService:
    """The service over the SAME ``WorkspaceStore`` the orchestrator uses (one workspace per
    tenant) and the plan/outcome/receipt stores. Pass ``lock`` = the orchestrator's per-tenant
    workspace lock so a user edit and a turn never interleave."""
    return WorkspaceService(workspaces, stores, policies=policies, consents=consents,
                            runtime_config=runtime_config, clock=clock, sink=sink, lock=lock)


__all__ = [
    "EditRejected", "IdempotencyKeyReused", "NoteRejected", "WorkspaceBusy", "WorkspaceConflict",
    "WorkspaceError", "WorkspaceNotConfigured", "WorkspaceService", "WorkspaceSnapshot",
    "build_workspace_service", "to_view",
]
