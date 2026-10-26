"""Phase 1 trip history, built DARK (decisions 18, 19, 25, 27; docs/data-and-history-design.md).

``HistoryService`` is the only writer of ``OutingRecord``s. It does nothing unless ALL of these hold:

* ``history_active(config)``: the ``history_enabled`` flag is on AND the configured notice version
  is on the owner-approved list (``APPROVED_HISTORY_NOTICE_VERSIONS``). Otherwise every method is a
  typed DISABLED before any read or write;
* the principal is REGISTERED_FREE: a guest's history stays on the device (decision 19);
* the user holds an ACTIVE HISTORY_STORE consent, which the consent store only grants against the
  approved notice version.

A record is opt-in user data: a place ID, the user's own label, an activity tag (derived
conservatively from the user's own request, or chosen by the user), timestamps, the outcome events,
the user's feedback and the consent id. It has NO field that can carry Maps content (no name,
address, hours, rating, category, review, link or coordinate); the client re-fetches details by
place ID and shows "details unavailable" on a miss. Records are written from a confirmed outcome
(``POST /v1/outcomes``) or an explicit user save, both idempotent by key.

Retention: 90 days by default; the user may pick 30 to 180 (the 180-day ceiling). Shortening purges
immediately and brings stored records' expiry forward; lengthening never extends a stored record.
``expires_at`` is the TTL field and ``purge`` deletes early. Edit (label, tag, feedback), delete one,
clear all and export are the user's controls; withdrawal and account deletion delete everything.

Telemetry events carry enums and counts only.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from .consent import ConsentStore
from .domain import (
    HISTORY_DEFAULT_RETENTION_DAYS,
    HISTORY_MAX_RETENTION_DAYS,
    HISTORY_MIN_RETENTION_DAYS,
    ConsentPurpose,
    Daypart,
    HistoryRecordCreate,
    HistoryRecordEdit,
    HistorySettings,
    IntentTag,
    OutcomeCheckin,
    OutcomeValue,
    OutingEvent,
    OutingEventType,
    OutingFeedback,
    OutingRecord,
    OutingStrength,
    PlaceRef,
    Principal,
    PrincipalKind,
    RuntimeConfig,
    evolve,
    history_active,
)
from .feature_errors import FeatureError, FeatureFailure
from .stores.history_store import HistoryStore
from .stores.protocols import WorkspaceStore
from .telemetry import (
    HistoryRecordDeleted,
    HistoryRecordWritten,
    TraceSink,
    current_trace_id,
    get_default_sink,
    trace,
)
from .textsafety import plain_text_problem

WORKSPACE_NAME = "default"  # Phase 0: one workspace per tenant

# --- the activity tag: from the user's own words, conservatively --------------------------------
_TAG_WORDS: dict[IntentTag, re.Pattern[str]] = {
    IntentTag.FOOD_DRINK: re.compile(
        r"\b(?:eat|eating|food|lunch|dinner|breakfast|brunch|restaurant|cafe|coffee|bar|pub|drink|"
        r"drinks|pizza|sushi|bakery|snack|dessert|manger|dejeuner|diner|resto|boire|boulangerie|"
        r"patisserie|bistro)\b"),
    IntentTag.OUTDOOR: re.compile(
        r"\b(?:hike|hiking|trail|park|beach|walk|outdoor|outdoors|picnic|garden|lake|bike|cycling|"
        r"promenade|randonnee|plage|parc|jardin|sentier|velo)\b"),
    IntentTag.ERRAND: re.compile(
        r"\b(?:groceries|grocery|pharmacy|errand|errands|post office|bank|laundry|epicerie|"
        r"pharmacie|banque|courses)\b"),
}
# KIDS can describe a child's activity, a sensitive inference (docs/data-and-history-design.md):
# it is never derived, only chosen by the user.
_DERIVABLE = tuple(_TAG_WORDS)


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text[:4_000].casefold())
    return "".join(c for c in folded if not unicodedata.combining(c))


def derive_intent_tag(text: str | None) -> IntentTag:
    """FOOD_DRINK, OUTDOOR or ERRAND when exactly one matches the user's own words; else OTHER."""
    if not text:
        return IntentTag.OTHER
    plain = _plain(text)
    hits = [tag for tag in _DERIVABLE if _TAG_WORDS[tag].search(plain)]
    return hits[0] if len(hits) == 1 else IntentTag.OTHER


def _daypart(hour: int) -> Daypart:
    if 5 <= hour < 12:
        return Daypart.MORNING
    if 12 <= hour < 17:
        return Daypart.AFTERNOON
    if 17 <= hour < 22:
        return Daypart.EVENING
    return Daypart.NIGHT


def _record_id(uid: str, source: str, key: str) -> str:
    return "out_" + hashlib.sha256(f"{uid}\x1f{source}\x1f{key}".encode()).hexdigest()[:32]


def _fingerprint(uid: str, payload: str) -> str:
    return hashlib.sha256(f"{uid}\x1f{payload}".encode()).hexdigest()


_OUTCOME_EVENT = {
    OutcomeValue.WENT: OutingEventType.VISITED_CONFIRMED,
    OutcomeValue.DID_NOT_GO: OutingEventType.SKIPPED,
    OutcomeValue.CHANGED_PLAN: OutingEventType.SKIPPED,
}


class HistoryService:
    def __init__(
        self, store: HistoryStore, consents: ConsentStore, *,
        runtime_config: RuntimeConfig | Callable[[], RuntimeConfig | None] | None = None,
        workspaces: WorkspaceStore | None = None, clock: Callable[[], datetime] | None = None,
        sink: TraceSink | None = None, account_guard: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self._consents = consents
        self._config = runtime_config
        self._workspaces = workspaces
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sink = sink
        self._account_guard = account_guard

    # -- internals ---------------------------------------------------------------------------------
    def _runtime_config(self) -> RuntimeConfig | None:
        return self._config() if callable(self._config) else self._config

    def _trace(self, event_cls: type, **fields: object) -> None:
        trace(event_cls, self._sink or get_default_sink(), trace_id=current_trace_id(), **fields)

    def _gate(self, principal: Principal, *, consent: bool = True) -> str | None:
        """Fail closed, in this order: flag and approved notice, registered, consent. Returns the
        active HISTORY_STORE consent id when ``consent`` is required."""
        if not history_active(self._runtime_config()):
            raise FeatureError(FeatureFailure.DISABLED)
        if principal.kind != PrincipalKind.REGISTERED_FREE:
            raise FeatureError(FeatureFailure.GUEST)
        if not consent:
            return None
        for record in self._consents.active(principal.uid):
            if record.purpose == ConsentPurpose.HISTORY_STORE:
                return record.id
        raise FeatureError(FeatureFailure.CONSENT_REQUIRED)

    def _assert_active(self, uid: str) -> None:
        """Before a write: a request in flight when the account is deleted writes nothing."""
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

    def _days(self, uid: str) -> int:
        config = self._runtime_config()
        ceiling = config.ttls.outing_retention_days if config else HISTORY_MAX_RETENTION_DAYS
        chosen = self.store.retention_days(uid) or HISTORY_DEFAULT_RETENTION_DAYS
        return max(1, min(chosen, ceiling, HISTORY_MAX_RETENTION_DAYS))

    def _workspace(self, uid: str, now: datetime):  # noqa: ANN202
        return self._workspaces.get(uid, WORKSPACE_NAME, now) if self._workspaces else None

    def _build(
        self, uid: str, record_id: str, consent_id: str, now: datetime, *, workspace_id: str,
        plan_version: int, timezone: str, place_id: str, label: str | None, tag: IntentTag,
        events: tuple[OutingEvent, ...], strength: OutingStrength, feedback: OutingFeedback,
    ) -> OutingRecord:
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            zone = ZoneInfo("UTC")
        local = now.astimezone(zone)
        try:
            return OutingRecord(
                id=record_id, owner_id=uid, workspace_id=workspace_id, plan_version=plan_version,
                occurred_at=now.astimezone(UTC), tz=zone.key, local_dow=local.weekday(),
                local_daypart=_daypart(local.hour),
                place_ref=PlaceRef(place_id=place_id, id_checked_at=now), user_label=label,
                intent_tag=tag, events=events, feedback=feedback, strength=strength,
                consent_id=consent_id,
                retention_expires_at=now + timedelta(days=self._days(uid)))
        except ValidationError:
            raise FeatureError(FeatureFailure.INVALID) from None

    @staticmethod
    def _plain_or_none(text: str | None) -> None:
        if text is not None and plain_text_problem(text):
            raise FeatureError(FeatureFailure.INVALID)

    def _put(self, uid: str, record: OutingRecord, fingerprint: str) -> OutingRecord:
        self._assert_active(uid)
        stored, outcome = self.store.put(uid, record, fingerprint)
        if outcome == "conflict":
            raise FeatureError(FeatureFailure.CONFLICT)
        if outcome == "full":
            raise FeatureError(FeatureFailure.LIMIT)
        if outcome == "created":
            self._trace(HistoryRecordWritten, strength=stored.strength)
        return stored

    # -- writes ------------------------------------------------------------------------------------
    def create(self, principal: Principal, body: HistoryRecordCreate, idempotency_key: str
               ) -> OutingRecord:
        """The user's explicit save of an outing (idempotent by key)."""
        consent_id = self._gate(principal)
        assert consent_id is not None
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise FeatureError(FeatureFailure.INVALID)
        self._plain_or_none(body.user_label)
        now = self._clock()
        workspace = self._workspace(principal.uid, now)
        if workspace is None or workspace.id != body.workspace_id:
            raise FeatureError(FeatureFailure.NOT_FOUND)  # the caller's own workspace only
        active = workspace.active_plan_version
        if (body.plan_version != 1) if active is None else (body.plan_version > active):
            raise FeatureError(FeatureFailure.NOT_FOUND)
        record = self._build(
            principal.uid, _record_id(principal.uid, "save", idempotency_key), consent_id, now,
            workspace_id=workspace.id, plan_version=body.plan_version, timezone=workspace.timezone,
            place_id=body.place_id, label=body.user_label, tag=body.intent_tag,
            events=(OutingEvent(type=OutingEventType.SELECTED, at=now),),
            strength=OutingStrength.HANDOFF, feedback=OutingFeedback())
        return self._put(principal.uid, record,
                         _fingerprint(principal.uid, body.model_dump_json()))

    def record_outcome(self, principal: Principal, checkin: OutcomeCheckin,
                       idempotency_key: str) -> bool:
        """Called after a confirmed check-in. True when a record was written (or already was).
        Never raises for a closed gate: a guest, a dark flag or a missing consent writes nothing."""
        try:
            consent_id = self._gate(principal)
        except FeatureError:
            return False
        assert consent_id is not None
        now = self._clock()
        workspace = self._workspace(principal.uid, now)
        event = _OUTCOME_EVENT[checkin.outcome]
        tag = checkin.intent_tag or derive_intent_tag(workspace.goal if workspace else None)
        record = self._build(
            principal.uid, _record_id(principal.uid, "outcome", idempotency_key), consent_id, now,
            workspace_id=workspace.id if workspace else "unknown",
            plan_version=(workspace.active_plan_version or 1) if workspace else 1,
            timezone=workspace.timezone if workspace else "UTC",
            place_id=checkin.place_id, label=checkin.user_label, tag=tag,
            events=(OutingEvent(type=event, at=now),),
            strength=(OutingStrength.SELF_REPORTED_VISIT if event == OutingEventType.VISITED_CONFIRMED
                      else OutingStrength.HANDOFF),
            feedback=OutingFeedback(note=checkin.note))
        self._put(principal.uid, record, _fingerprint(principal.uid, checkin.model_dump_json()))
        return True

    def edit(self, principal: Principal, record_id: str, edit: HistoryRecordEdit) -> OutingRecord:
        """The user's own edit: label, tag and feedback. The place and the times never change."""
        self._gate(principal)
        if edit.clear_label and edit.user_label is not None:
            raise FeatureError(FeatureFailure.INVALID)
        self._plain_or_none(edit.user_label)
        if edit.feedback is not None:
            self._plain_or_none(edit.feedback.note)
        self._assert_active(principal.uid)

        def apply(record: OutingRecord) -> OutingRecord:
            changes: dict[str, object] = {}
            if edit.clear_label:
                changes["user_label"] = None
            elif edit.user_label is not None:
                changes["user_label"] = edit.user_label
            if edit.intent_tag is not None:
                changes["intent_tag"] = edit.intent_tag
            if edit.feedback is not None:
                changes["feedback"] = edit.feedback
            return evolve(record, **changes) if changes else record

        updated = self.store.update(principal.uid, _checked_id(record_id), apply, self._clock())
        if updated is None:
            raise FeatureError(FeatureFailure.NOT_FOUND)
        return updated

    def delete(self, principal: Principal, record_id: str) -> int:
        """Delete one record. Never blocked by a missing consent (the user can always delete)."""
        self._gate(principal, consent=False)
        if not self.store.delete(principal.uid, _checked_id(record_id)):
            raise FeatureError(FeatureFailure.NOT_FOUND)
        self._trace(HistoryRecordDeleted, deleted_count=1)
        return 1

    def clear(self, principal: Principal) -> int:
        self._gate(principal, consent=False)
        count = self.store.delete_records(principal.uid)
        if count:
            self._trace(HistoryRecordDeleted, deleted_count=count)
        return count

    # -- reads and settings --------------------------------------------------------------------------
    def list(self, principal: Principal) -> tuple[OutingRecord, ...]:
        self._gate(principal)
        return self.store.list(principal.uid, self._clock())

    def settings(self, principal: Principal) -> HistorySettings:
        self._gate(principal)
        return HistorySettings(retention_days=self._days(principal.uid))

    def set_retention(self, principal: Principal, days: int) -> HistorySettings:
        """30 to 180 days (never above the configured ceiling). Shortening purges at once."""
        self._gate(principal)
        config = self._runtime_config()
        ceiling = min(HISTORY_MAX_RETENTION_DAYS,
                      config.ttls.outing_retention_days if config else HISTORY_MAX_RETENTION_DAYS)
        if not HISTORY_MIN_RETENTION_DAYS <= days <= ceiling:
            raise FeatureError(FeatureFailure.INVALID)
        self._assert_active(principal.uid)
        purged = self.store.apply_retention(principal.uid, days, self._clock())
        if purged:
            self._trace(HistoryRecordDeleted, deleted_count=purged)
        return HistorySettings(retention_days=days)

    # -- retention sweep, export, cascade -------------------------------------------------------------
    def purge(self) -> int:
        """Delete expired records early (the TTL policy is the primary mechanism). Needs no flag:
        retention applies to data that exists whatever the flag says now."""
        return self.store.purge(self._clock())

    def export(self, uid: str) -> tuple[OutingRecord, ...]:
        return tuple(sorted(self.store.list(uid, self._clock()), key=lambda r: r.occurred_at))

    def delete_all(self, uid: str) -> int:
        """Consent withdrawal and account deletion: every record and the retention choice."""
        return self.store.delete_all(uid)


def _checked_id(record_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", record_id):
        raise FeatureError(FeatureFailure.NOT_FOUND)
    return record_id


__all__ = ["HistoryService", "derive_intent_tag"]
