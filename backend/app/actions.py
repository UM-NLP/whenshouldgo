"""PendingAction service: the only way a side effect happens (AGENTS.md, decisions 15, 24, 27).

Propose creates a server-authored record (preview from typed fields, one-time random nonce, 15
minute expiry, idempotency key). Confirm needs an authenticated user request that carries the
nonce; it is one atomic store transition, so the nonce is single use and a double confirm yields
exactly one effect (the second call replays the same typed result). Nothing in the model, tool or
pipeline path imports this module: a model cannot propose-and-confirm (tested).

Effects (Phase 0 only; nothing else is accepted):

* ``SET_REMINDER``: returns a typed ``ReminderItem`` the client stores LOCALLY. The server keeps
  only the confirmed-action record (place ID, the user's label, the due time).
* ``LAUNCH_HANDOFF``: returns a Maps URL built NOW from the place ID only (``handoff.py``); the URL
  is never stored or logged.
* ``SAVE_PLACE``: created here, executable only while ``history_active``; the write goes through
  the injected ``SavedPlaceSink`` (default: typed DISABLED). Saved-place storage is not here. The
  proposal carries the user's consented position (rounded on parse); it is held in the pending
  record only until confirm, cancel or expiry and is then scrubbed (``ActionRecord.position``).

Confirming a reminder or a handoff activates the workspace trip once (``QuotaService``); a user
who has used their trips can still confirm (the gate never blocks these two effects).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from . import telemetry
from .domain import (
    MAX_PENDING_ACTION_TTL,
    ConfirmActionResponse,
    PendingAction,
    PendingActionEffect,
    Principal,
    ProposeActionRequest,
    ReminderItem,
    RoundedPosition,
    RuntimeConfig,
    SavedPlaceKind,
    WorkspaceStatus,
    evolve,
    history_active,
)
from .handoff import HandoffError, build_maps_url
from .quota import QuotaService
from .stores.actions_store import (
    ActionRecord,
    ActionStatus,
    FirestorePendingActionStore,
    MemoryPendingActionStore,
    PendingActionStore,
)
from .stores.protocols import WorkspaceStore
from .telemetry import ActionRejectReason, TraceSink

WORKSPACE_NAME = "default"  # Phase 0: one workspace per tenant
# Phase 0 is same-day: a reminder falls within a day of the proposal.
MAX_REMINDER_HORIZON = timedelta(hours=24)
MAX_NONCE_ATTEMPTS = 5  # wrong nonces tolerated per action before it is locked
_PLACE_ID = re.compile(r"[A-Za-z0-9_-]{1,300}")
_ACTION_EFFECTS = {PendingActionEffect.SET_REMINDER, PendingActionEffect.LAUNCH_HANDOFF,
                   PendingActionEffect.SAVE_PLACE}
_ACTIVATING = {PendingActionEffect.SET_REMINDER, PendingActionEffect.LAUNCH_HANDOFF}


# --- errors ---------------------------------------------------------------------------------------
class ActionFailure(StrEnum):
    NOT_FOUND = "NOT_FOUND"  # unknown, expired-and-purged or another tenant's: indistinguishable
    EXPIRED = "EXPIRED"
    NONCE_MISMATCH = "NONCE_MISMATCH"
    CANCELLED = "CANCELLED"
    ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
    DISABLED = "DISABLED"
    NOT_ALLOWED = "NOT_ALLOWED"  # a guest: saved places stay on the device (decision 19)
    CONSENT_REQUIRED = "CONSENT_REQUIRED"  # SAVED_PLACE consent is not held
    STALE_PLAN = "STALE_PLAN"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID = "INVALID"


class ActionError(Exception):
    """A typed refusal. The message is static: never a label, place ID or nonce."""

    def __init__(self, failure: ActionFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


_TELEMETRY_REASON = {
    ActionFailure.NOT_FOUND: ActionRejectReason.NOT_FOUND,
    ActionFailure.EXPIRED: ActionRejectReason.EXPIRED,
    ActionFailure.NONCE_MISMATCH: ActionRejectReason.NONCE_MISMATCH,
    ActionFailure.CANCELLED: ActionRejectReason.CANCELLED,
    ActionFailure.ALREADY_CONFIRMED: ActionRejectReason.ALREADY_CONFIRMED,
    ActionFailure.DISABLED: ActionRejectReason.EFFECT_NOT_ALLOWED,
    ActionFailure.NOT_ALLOWED: ActionRejectReason.EFFECT_NOT_ALLOWED,
    ActionFailure.CONSENT_REQUIRED: ActionRejectReason.CONSENT_REQUIRED,
    ActionFailure.STALE_PLAN: ActionRejectReason.EXPIRED,  # approval no longer current
    ActionFailure.IDEMPOTENCY_CONFLICT: ActionRejectReason.EFFECT_NOT_ALLOWED,
    ActionFailure.INVALID: ActionRejectReason.EFFECT_NOT_ALLOWED,
}


class SavedPlaceSink(Protocol):
    """Writes a confirmed SAVE_PLACE (``saved_places.SavedPlaceActionSink``). ``precheck`` runs at
    propose time (flag, registered tier, SAVED_PLACE consent) and ``save_place`` again at commit,
    so a consent withdrawn in between saves nothing. ``save_place`` must be idempotent on
    ``action_id``. The position is the user's own, rounded: it is never logged."""

    def precheck(self, principal: Principal) -> None: ...

    def save_place(self, principal: Principal, *, place_id: str, user_label: str,
                   label_kind: SavedPlaceKind, action_id: str,
                   position: RoundedPosition) -> None: ...


class DisabledSavedPlaceSink:
    def precheck(self, principal: Principal) -> None:
        raise ActionError(ActionFailure.DISABLED)

    def save_place(self, principal: Principal, *, place_id: str, user_label: str,
                   label_kind: SavedPlaceKind, action_id: str,
                   position: RoundedPosition) -> None:
        raise ActionError(ActionFailure.DISABLED)


@dataclass(frozen=True)
class ActivationOutcome:
    workspace_id: str
    status: WorkspaceStatus
    counted: bool  # this call counted the trip (False: already counted, or gated)
    gated: bool  # the trip allowance is used up and this workspace was not yet counted


# --- helpers --------------------------------------------------------------------------------------
def _fingerprint(owner: str, body: ProposeActionRequest) -> str:
    due = body.due_at.astimezone(UTC).isoformat() if body.due_at else ""
    parts = (owner, body.effect.value, body.target, body.workspace_id, str(body.plan_version),
             body.user_label or "", body.label_kind.value if body.label_kind else "", due,
             f"{body.position.latitude:.3f},{body.position.longitude:.3f}" if body.position else "")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _action_id(owner: str, key: str) -> str:
    return "act_" + hashlib.sha256(f"{owner}\x1f{key}".encode()).hexdigest()[:32]


def _preview(body: ProposeActionRequest) -> str:
    """Server-authored from typed fields only: the user's own label, the due time, the effect."""
    label = f'"{body.user_label}"' if body.user_label else "this place"
    if body.effect == PendingActionEffect.SET_REMINDER:
        assert body.due_at is not None
        due = body.due_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return f"Set an in-app reminder for {label} at {due}"
    if body.effect == PendingActionEffect.LAUNCH_HANDOFF:
        return f"Open {label} in Google Maps"
    assert body.label_kind is not None
    return f"Save {label} as your {body.label_kind.value.lower()} place"


class ActionService:
    def __init__(
        self, store: PendingActionStore, quotas: QuotaService, *,
        workspaces: WorkspaceStore | None = None, clock: Callable[[], datetime] | None = None,
        runtime_config: RuntimeConfig | None = None, saved_place_sink: SavedPlaceSink | None = None,
        trace_sink: TraceSink | None = None, token_factory: Callable[[], str] | None = None,
        account_guard: Callable[[str], None] | None = None,
    ) -> None:
        self._account_guard = account_guard  # raises once the account is deleted
        self._store = store
        self.quotas = quotas
        self._workspaces = workspaces
        self._clock = clock or (lambda: datetime.now(UTC))
        self.runtime_config = runtime_config
        self._sink: SavedPlaceSink = saved_place_sink or DisabledSavedPlaceSink()
        self._trace_sink = trace_sink
        self._token = token_factory or (lambda: secrets.token_urlsafe(24))

    # -- telemetry (enums, counts and booleans only) ---------------------------------------------
    def _trace(self, event_cls: type, **fields: object) -> None:
        telemetry.trace(event_cls, self._trace_sink, trace_id=telemetry.current_trace_id(), **fields)

    def _reject(self, failure: ActionFailure, effect: PendingActionEffect | None) -> ActionError:
        self._trace(telemetry.ActionRejected, effect=effect, reason=_TELEMETRY_REASON[failure])
        return ActionError(failure)

    def _assert_active(self, uid: str, effect: PendingActionEffect | None) -> None:
        """Before a write: a request in flight when the account is deleted records nothing."""
        if self._account_guard is None:
            return
        try:
            self._account_guard(uid)
        except Exception:  # noqa: BLE001 - deleted, or the tombstone is unreadable: fail closed
            inactive = True
        else:
            inactive = False
        if inactive:
            raise self._reject(ActionFailure.NOT_FOUND, effect)

    # -- workspace ---------------------------------------------------------------------------------
    def _workspace(self, principal: Principal, now: datetime):  # noqa: ANN202
        if self._workspaces is None:
            return None
        return self._workspaces.get(principal.uid, WORKSPACE_NAME, now)

    @staticmethod
    def _plan_ok(workspace, plan_version: int) -> bool:  # noqa: ANN001
        return workspace is None or workspace.active_plan_version in (None, plan_version)

    # -- propose -----------------------------------------------------------------------------------
    def propose(self, principal: Principal, body: ProposeActionRequest, idempotency_key: str) -> PendingAction:
        effect = body.effect
        if effect not in _ACTION_EFFECTS:
            raise self._reject(ActionFailure.INVALID, None)
        if effect == PendingActionEffect.SAVE_PLACE:
            if not history_active(self.runtime_config):
                raise self._reject(ActionFailure.DISABLED, effect)  # nothing to save into
            try:
                self._sink.precheck(principal)  # registered tier and SAVED_PLACE consent
            except ActionError as exc:
                raise self._reject(exc.failure, effect) from None
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise self._reject(ActionFailure.INVALID, effect)
        if _PLACE_ID.fullmatch(body.target) is None:
            raise self._reject(ActionFailure.INVALID, effect)
        now = self._clock()
        if effect == PendingActionEffect.SET_REMINDER:
            if body.due_at is None or not now < body.due_at <= now + MAX_REMINDER_HORIZON:
                raise self._reject(ActionFailure.INVALID, effect)
        elif body.due_at is not None:
            raise self._reject(ActionFailure.INVALID, effect)
        if effect == PendingActionEffect.SAVE_PLACE:
            if not body.user_label or body.label_kind is None or body.position is None:
                raise self._reject(ActionFailure.INVALID, effect)
        elif body.position is not None:
            raise self._reject(ActionFailure.INVALID, effect)  # a position is for SAVE_PLACE only
        elif effect == PendingActionEffect.LAUNCH_HANDOFF and body.label_kind is not None:
            raise self._reject(ActionFailure.INVALID, effect)
        elif effect == PendingActionEffect.SET_REMINDER and body.label_kind is not None:
            raise self._reject(ActionFailure.INVALID, effect)

        fingerprint = _fingerprint(principal.uid, body)
        action_id = _action_id(principal.uid, idempotency_key)
        existing = self._store.get(principal.uid, action_id)
        if existing is not None:  # the same key: the same action, or a typed conflict
            if existing.fingerprint != fingerprint:
                raise self._reject(ActionFailure.IDEMPOTENCY_CONFLICT, effect)
            return existing.action
        workspace = self._workspace(principal, now)
        if self._workspaces is not None and (workspace is None or workspace.id != body.workspace_id):
            raise self._reject(ActionFailure.NOT_FOUND, effect)
        if not self._plan_ok(workspace, body.plan_version):
            raise self._reject(ActionFailure.STALE_PLAN, effect)

        self._assert_active(principal.uid, effect)
        action = PendingAction(
            id=action_id, workspace_id=body.workspace_id, plan_version=body.plan_version,
            effect=effect, target=body.target, preview=_preview(body), nonce=self._token(),
            idempotency_key=idempotency_key, created_at=now,
            expires_at=now + MAX_PENDING_ACTION_TTL)
        record = ActionRecord(
            owner_id=principal.uid, action=action, fingerprint=fingerprint,
            user_label=body.user_label, label_kind=body.label_kind, due_at=body.due_at,
            position=body.position)
        stored = self._store.update(
            principal.uid, action_id, lambda current: (current or record, current or record))
        if stored.fingerprint != fingerprint:  # lost a race to a different request on this key
            raise self._reject(ActionFailure.IDEMPOTENCY_CONFLICT, effect)
        if stored is record:
            self._trace(telemetry.ActionProposed, effect=effect)
        return stored.action

    # -- confirm -----------------------------------------------------------------------------------
    def confirm(
        self, principal: Principal, action_id: str, nonce: str, idempotency_key: str | None = None,
    ) -> ConfirmActionResponse:
        """Only an authenticated user request carrying the nonce reaches this (route layer)."""
        now = self._clock()
        history = history_active(self.runtime_config)
        workspace = None
        self._assert_active(principal.uid, None)
        known = self._store.get(principal.uid, action_id)
        if known is not None:
            workspace = self._workspace(principal, now)

        def decide(rec: ActionRecord | None) -> tuple[ActionRecord | None, tuple[str, ActionRecord | None]]:
            if rec is None:
                return None, (ActionFailure.NOT_FOUND, None)
            action = rec.action
            if rec.status == ActionStatus.CANCELLED:
                return None, (ActionFailure.CANCELLED, rec)
            if rec.failed_attempts >= MAX_NONCE_ATTEMPTS:
                return None, (ActionFailure.NONCE_MISMATCH, rec)
            if not hmac.compare_digest(nonce.encode(), action.nonce.encode()):
                return replace(rec, failed_attempts=rec.failed_attempts + 1), (
                    ActionFailure.NONCE_MISMATCH, rec)
            if idempotency_key is not None and idempotency_key != action.idempotency_key:
                return None, (ActionFailure.IDEMPOTENCY_CONFLICT, rec)
            if rec.status == ActionStatus.CONFIRMED:
                return None, ("replay", rec)
            if now > action.expires_at:  # an expired proposal keeps no position
                return (replace(rec, position=None) if rec.position is not None else None,
                        (ActionFailure.EXPIRED, rec))
            if action.effect == PendingActionEffect.SAVE_PLACE and not history:
                return None, (ActionFailure.DISABLED, rec)
            if not self._plan_ok(workspace, action.plan_version):
                return None, (ActionFailure.STALE_PLAN, rec)
            confirmed = replace(rec, status=ActionStatus.CONFIRMED,
                                action=evolve(action, confirmed_at=now))
            return confirmed, ("confirmed", confirmed)

        outcome, rec = self._store.update(principal.uid, action_id, decide)
        effect = rec.action.effect if rec is not None else None
        if isinstance(outcome, ActionFailure):
            raise self._reject(outcome, effect)
        assert rec is not None
        first = outcome == "confirmed"
        if rec.action.effect == PendingActionEffect.SAVE_PLACE:
            if first or rec.position is not None:  # a replay finishes a save that was cut short
                self._save_place(principal, rec)
        else:
            self._activate(principal, rec.action.workspace_id)  # idempotent per workspace
        self._trace(telemetry.ActionConfirmed, effect=rec.action.effect, replay=not first)
        return self._result(rec)

    def _save_place(self, principal: Principal, rec: ActionRecord) -> None:
        try:
            if rec.user_label is None or rec.label_kind is None or rec.position is None:
                raise ActionError(ActionFailure.INVALID)  # never report a save that cannot happen
            self._sink.save_place(
                principal, place_id=rec.action.target, user_label=rec.user_label,
                label_kind=rec.label_kind, action_id=rec.action.id, position=rec.position)
        except BaseException as exc:
            self._store.update(principal.uid, rec.action.id, self._reopen)  # nothing was saved
            if isinstance(exc, ActionError):
                raise self._reject(exc.failure, rec.action.effect) from None
            raise
        # The position now lives in the saved place only: scrub it from the pending record.
        self._store.update(principal.uid, rec.action.id, self._scrub)

    @staticmethod
    def _scrub(rec: ActionRecord | None) -> tuple[ActionRecord | None, None]:
        if rec is None or rec.position is None:
            return None, None
        return replace(rec, position=None, user_label=None), None

    @staticmethod
    def _reopen(rec: ActionRecord | None) -> tuple[ActionRecord | None, None]:
        if rec is None or rec.status != ActionStatus.CONFIRMED:
            return None, None
        return replace(rec, status=ActionStatus.PENDING,
                       action=evolve(rec.action, confirmed_at=None)), None

    @staticmethod
    def _result(rec: ActionRecord) -> ConfirmActionResponse:
        action = rec.action
        reminder = url = None
        if action.effect == PendingActionEffect.SET_REMINDER and rec.due_at is not None:
            reminder = ReminderItem(action_id=action.id, place_id=action.target,
                                    user_label=rec.user_label, due_at=rec.due_at)
        elif action.effect == PendingActionEffect.LAUNCH_HANDOFF:
            try:
                url = build_maps_url(action.target)  # at confirm time; never stored or logged
            except HandoffError:  # a stored target that is no longer a valid place id
                raise ActionError(ActionFailure.INVALID) from None
        return ConfirmActionResponse(action=action, reminder=reminder, handoff_url=url)

    # -- cancel ------------------------------------------------------------------------------------
    def cancel(self, principal: Principal, action_id: str) -> bool:
        def decide(rec: ActionRecord | None) -> tuple[ActionRecord | None, tuple[object, ActionRecord | None]]:
            if rec is None:
                return None, (ActionFailure.NOT_FOUND, None)
            if rec.status == ActionStatus.CONFIRMED:
                return None, (ActionFailure.ALREADY_CONFIRMED, rec)
            if rec.status == ActionStatus.CANCELLED:
                return None, ("cancelled", rec)
            return replace(rec, status=ActionStatus.CANCELLED, position=None), ("cancelled", rec)

        outcome, rec = self._store.update(principal.uid, action_id, decide)
        if isinstance(outcome, ActionFailure):
            raise self._reject(outcome, rec.action.effect if rec else None)
        return True

    # -- trip activation -------------------------------------------------------------------------
    def _activate(self, principal: Principal, workspace_id: str) -> tuple[bool, bool]:
        """(counted, gated). A gated user is not an error here: handoff and reminder still work."""
        try:
            counted = self.quotas.activate_trip(principal, workspace_id)
        except PermissionError:
            return False, True
        if counted:
            self._trace(telemetry.TripActivated, principal_kind=principal.kind)
        return counted, False

    def activate(self, principal: Principal, workspace_id: str, plan_version: int) -> ActivationOutcome:
        now = self._clock()
        workspace = self._workspace(principal, now)
        if workspace is None or workspace.id != workspace_id:
            raise ActionError(ActionFailure.NOT_FOUND)
        if not self._plan_ok(workspace, plan_version):
            raise ActionError(ActionFailure.STALE_PLAN)
        counted, gated = self._activate(principal, workspace_id)
        return ActivationOutcome(workspace_id, workspace.status, counted, gated)

    # -- retention ---------------------------------------------------------------------------------
    def purge(self) -> int:
        return self._store.purge(self._clock())

    def delete_save_place_records(self, uid: str) -> int:
        """SAVED_PLACE consent withdrawn: no pending or confirmed SAVE_PLACE record (label, held
        position) remains for the user."""
        return self._store.delete_save_place_records(uid)


def build_action_service(
    *, quotas: QuotaService, store: PendingActionStore | None = None,
    firestore_client: object | None = None, transactional: Callable[..., object] | None = None,
    workspaces: WorkspaceStore | None = None, clock: Callable[[], datetime] | None = None,
    runtime_config: RuntimeConfig | None = None, saved_place_sink: SavedPlaceSink | None = None,
    trace_sink: TraceSink | None = None, account_guard: Callable[[str], None] | None = None,
) -> ActionService:
    """Composition helper. Store: pass ``store``, or a ``firestore_client`` (production), or
    neither (in-memory, development and tests)."""
    if store is None:
        store = (FirestorePendingActionStore(firestore_client, transactional=transactional)
                 if firestore_client is not None else MemoryPendingActionStore())
    return ActionService(
        store, quotas, workspaces=workspaces, clock=clock, runtime_config=runtime_config,
        saved_place_sink=saved_place_sink, trace_sink=trace_sink, account_guard=account_guard)
