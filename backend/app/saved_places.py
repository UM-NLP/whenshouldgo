"""Saved places and Home/Work, built DARK (decisions 24, 25, 27).

A saved place is USER DATA: a Google place ID, the label the user typed, a kind (HOME, WORK or
CUSTOM) and the user's own consented foreground position rounded to about 100 m. Nothing else from
Google. ``SavedPlaceService`` does nothing unless ``history_active`` holds, the principal is
REGISTERED_FREE (guests keep saved places on the device) and the SAVED_PLACE consent is held.

* CREATE happens only through a confirmed ``SAVE_PLACE`` PendingAction: ``SavedPlaceActionSink`` is
  the ``SavedPlaceSink`` the ``ActionService`` calls after the nonce and expiry checks. Nothing here
  creates a saved place from a plain request.
* EDIT is label-only (``relabel``): the kind and the position of an existing entry never change
  without a new confirmed action, so a Home or Work is never created or moved silently.
* A saved Home or Work is never silently applied: it is exposed to the client only. The client may
  send it in the existing ``location`` field of a turn (decision 27), which needs the consent path
  that already exists.
* The fixed "near home" question (``ask_policy.py``) and its ask-state live here for a registered
  user; the state is stored only while the SAVED_PLACE consent is held.

Telemetry events carry enums and counts only.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .actions import ActionError, ActionFailure
from .ask_policy import ASK_COOLDOWN, decide, referenced_kinds
from .consent import ConsentStore
from .domain import (
    AskDecision,
    AskStateUpdate,
    AskStateView,
    ConsentPurpose,
    Principal,
    PrincipalKind,
    RoundedPosition,
    RuntimeConfig,
    SavedPlace,
    SavedPlaceKind,
    SavedPlacePrompt,
    SavedPlaceUpsert,
    history_active,
)
from .feature_errors import FeatureError, FeatureFailure
from .stores.saved_places_store import AskRecord, AskStateStore, SavedPlaceStore
from .telemetry import (
    SavedPlaceDeleted,
    SavedPlaceSaved,
    TraceSink,
    current_trace_id,
    get_default_sink,
    trace,
)
from .textsafety import plain_text_problem

_ASKABLE = (SavedPlaceKind.HOME, SavedPlaceKind.WORK)


class SavedPlaceService:
    def __init__(
        self, places: SavedPlaceStore, asks: AskStateStore, consents: ConsentStore, *,
        runtime_config: RuntimeConfig | Callable[[], RuntimeConfig | None] | None = None,
        clock: Callable[[], datetime] | None = None, sink: TraceSink | None = None,
        account_guard: Callable[[str], None] | None = None,
    ) -> None:
        self.places = places
        self.asks = asks
        self._consents = consents
        self._config = runtime_config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sink = sink
        self._account_guard = account_guard

    # -- internals ---------------------------------------------------------------------------------
    def _runtime_config(self) -> RuntimeConfig | None:
        return self._config() if callable(self._config) else self._config

    def _trace(self, event_cls: type, **fields: object) -> None:
        trace(event_cls, self._sink or get_default_sink(), trace_id=current_trace_id(), **fields)

    def _has_consent(self, uid: str) -> bool:
        return self._consents.has_active(uid, ConsentPurpose.SAVED_PLACE)

    def _gate(self, principal: Principal, *, consent: bool = True) -> None:
        """Fail closed, in this order: flag and approved notice, registered, consent."""
        if not history_active(self._runtime_config()):
            raise FeatureError(FeatureFailure.DISABLED)
        if principal.kind != PrincipalKind.REGISTERED_FREE:
            raise FeatureError(FeatureFailure.GUEST)
        if consent and not self._has_consent(principal.uid):
            raise FeatureError(FeatureFailure.CONSENT_REQUIRED)

    def _assert_active(self, uid: str) -> None:
        if self._account_guard is None:
            return
        try:
            self._account_guard(uid)
        except Exception:  # noqa: BLE001 - deleted, or the tombstone is unreadable: fail closed
            inactive = True
        else:
            inactive = False
        if inactive:
            raise FeatureError(FeatureFailure.UNAVAILABLE)

    # -- create (only called by the confirmed-action sink) --------------------------------------------
    def precheck(self, principal: Principal) -> None:
        """Before a SAVE_PLACE is proposed or committed: the same gate as every other method."""
        self._gate(principal)

    def save_confirmed(
        self, principal: Principal, *, place_id: str, user_label: str, label_kind: SavedPlaceKind,
        position: RoundedPosition, action_id: str,
    ) -> SavedPlace:
        """Persist a place the user confirmed. Idempotent on ``action_id``."""
        self._gate(principal)
        self._assert_active(principal.uid)
        if plain_text_problem(user_label):
            raise FeatureError(FeatureFailure.INVALID)
        place = SavedPlace(place_id=place_id, user_label=user_label, label_kind=label_kind,
                           position=position, created_at=self._clock())
        outcome = self.places.replace(principal.uid, place, action_id)
        if outcome == "full":
            raise FeatureError(FeatureFailure.LIMIT)
        if outcome == "saved":
            self._trace(SavedPlaceSaved, label_kind=label_kind)
        return place

    # -- the user's controls ---------------------------------------------------------------------------
    def list(self, principal: Principal) -> tuple[SavedPlace, ...]:
        self._gate(principal)
        return self.places.list(principal.uid)

    def relabel(self, principal: Principal, body: SavedPlaceUpsert) -> SavedPlace:
        """Edit the label of an EXISTING entry. The kind and the position must match what is
        stored: anything else would create or move a Home/Work without a confirmed action."""
        self._gate(principal)
        existing = self.places.get(principal.uid, body.place_id)
        if existing is None:
            raise FeatureError(FeatureFailure.NOT_FOUND)
        if body.label_kind != existing.label_kind or body.position != existing.position:
            raise FeatureError(FeatureFailure.INVALID)
        if plain_text_problem(body.user_label):
            raise FeatureError(FeatureFailure.INVALID)
        self._assert_active(principal.uid)
        updated = self.places.relabel(principal.uid, body.place_id, body.user_label)
        if updated is None:
            raise FeatureError(FeatureFailure.NOT_FOUND)
        return updated

    def delete(self, principal: Principal, place_id: str | None) -> int:
        """One entry, or all of them. Never blocked by a missing consent."""
        self._gate(principal, consent=False)
        if place_id is not None:
            count = self.places.delete(principal.uid, place_id)
        else:
            count = self.places.delete_all(principal.uid)
        if count:
            self._trace(SavedPlaceDeleted, deleted_count=count)
        return count

    # -- the fixed question ------------------------------------------------------------------------------
    def prompt(self, principal: Principal, message: str) -> SavedPlacePrompt:
        """The fixed question, or ``ask=False``. Reads nothing unless the message refers to a
        home or work label. The message is neither stored nor logged."""
        self._gate(principal, consent=False)
        if not referenced_kinds(message):
            return SavedPlacePrompt(ask=False)
        saved = {p.label_kind for p in self.places.list(principal.uid)}
        return decide(message, saved=saved, states=self.asks.get(principal.uid),
                      now=self._clock())

    def set_ask_state(self, principal: Principal, update: AskStateUpdate) -> AskStateView:
        """Record a decline ("not now": 30-day cooldown) or "don't ask again", or clear them. Stored
        only while the SAVED_PLACE consent is held; otherwise ``recorded`` is False and the client
        keeps the state on the device."""
        self._gate(principal, consent=False)
        kind = update.label_kind
        if kind not in _ASKABLE:
            raise FeatureError(FeatureFailure.INVALID)
        if not self._has_consent(principal.uid):
            return AskStateView(label_kind=kind, recorded=False)
        self._assert_active(principal.uid)
        now = self._clock()
        record: AskRecord | None
        if update.decision == AskDecision.DECLINED:
            current = self.asks.get(principal.uid).get(kind)
            record = AskRecord(declined_at=now,
                               dont_ask_again=bool(current and current.dont_ask_again))
        elif update.decision == AskDecision.DONT_ASK_AGAIN:
            current = self.asks.get(principal.uid).get(kind)
            record = AskRecord(declined_at=current.declined_at if current else None,
                               dont_ask_again=True)
        else:
            record = None
        self.asks.put(principal.uid, kind, record)
        return AskStateView(
            label_kind=kind, recorded=True,
            dont_ask_again=bool(record and record.dont_ask_again),
            cooldown_until=(record.declined_at + ASK_COOLDOWN
                            if record and record.declined_at else None))

    # -- export and cascade -------------------------------------------------------------------------------
    def export(self, uid: str) -> tuple[SavedPlace, ...]:
        return self.places.list(uid)

    def delete_all(self, uid: str) -> int:
        """Consent withdrawal and account deletion: every place and the ask-state."""
        count = self.places.delete_all(uid)
        self.asks.delete_owner(uid)
        return count


class SavedPlaceActionSink:
    """The ``SavedPlaceSink`` for the ``ActionService``: translates this service's typed refusals
    into the action protocol's."""

    _MAP = {
        FeatureFailure.DISABLED: ActionFailure.DISABLED,
        FeatureFailure.GUEST: ActionFailure.NOT_ALLOWED,
        FeatureFailure.CONSENT_REQUIRED: ActionFailure.CONSENT_REQUIRED,
        FeatureFailure.NOT_FOUND: ActionFailure.NOT_FOUND,
        FeatureFailure.UNAVAILABLE: ActionFailure.NOT_FOUND,
    }

    def __init__(self, service: SavedPlaceService) -> None:
        self._service = service

    def _translate(self, error: FeatureError) -> ActionError:
        return ActionError(self._MAP.get(error.failure, ActionFailure.INVALID))

    def precheck(self, principal: Principal) -> None:
        try:
            self._service.precheck(principal)
        except FeatureError as error:
            raise self._translate(error) from None

    def save_place(
        self, principal: Principal, *, place_id: str, user_label: str, label_kind: SavedPlaceKind,
        action_id: str, position: RoundedPosition,
    ) -> None:
        try:
            self._service.save_confirmed(
                principal, place_id=place_id, user_label=user_label, label_kind=label_kind,
                position=position, action_id=action_id)
        except FeatureError as error:
            raise self._translate(error) from None


__all__ = ["SavedPlaceActionSink", "SavedPlaceService"]
