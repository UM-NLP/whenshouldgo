"""Explicit, previewed, resumable merge of a guest's server data into an existing account
(docs/access-tiers.md "Identity, quota and upgrade design", docs/trip-assistant-security.md).

The flow, every step server-owned:

1. ``POST /v1/account/link-intents/{id}:complete`` answered COLLISION: the credential already
   belongs to another account. The intent is parked, bound to the uid that presented it.
2. ``start`` authenticates BOTH identities. The registered account is the verified bearer token;
   the guest is proved by the one-time intent that was issued under the guest's own verified token
   (id plus nonce) and parked for exactly this registered uid. It produces the MANIFEST of the
   guest's server data and a journal record, idempotent by key. Nothing is copied yet.
3. ``run`` copies the items the user selected, each with its own idempotency key
   (``<migration id>:<item id>``) and a journal entry per copied item. An interrupted run is
   resumed by calling ``run`` again: copied items are skipped and the rest continue.
4. Only when every selected item is COPIED does the guest's server data get deleted (the cascade
   the caller supplies), the guest uid is tombstoned, and the journal is COMPLETED. Until then
   both identities and all their data are untouched.

Never done here: merging by email, adding the guest allowance to the registered one (usage
counters are never copied; the guest ledger is deleted with the rest), overwriting the registered
account's preferences without the user's choice (``KEEP_REGISTERED`` is the default), or carrying
over consent (registration is not consent; the guest's ledger is deleted, not copied).
"""
from __future__ import annotations

import hmac
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from .entitlements import REGISTERED_WORKSPACE_INACTIVITY_DAYS
from .domain import (
    MIGRATION_TITLE_MAX as TITLE_MAX,
    FactSource,
    MigrationItemView,
    MigrationRunRequest,
    MigrationStartRequest,
    MigrationState,
    MigrationView,
    PreferenceChoice,
    WorkspaceStatus,
    evolve,
)
from .stores.account_store import (
    AccountStore,
    IntentState,
    ItemState,
    MigrationItem,
    MigrationRecord,
    key_digest,
    nonce_digest,
    uid_hash,
)
from .stores.protocols import WorkspaceStore

DEFAULT_WORKSPACE = "default"  # the contract itself (requests, views, MigrationState) is in domain.py


def view_of(record: MigrationRecord) -> MigrationView:
    return MigrationView(
        id=record.id, state=record.state, completed=record.state == MigrationState.COMPLETED,
        preference=PreferenceChoice(record.preference) if record.preference else None,
        items=tuple(MigrationItemView(
            id=i.id, kind=i.kind, title=i.title, selected=i.selected,
            copied=i.state == ItemState.COPIED) for i in record.items))


# --- errors ------------------------------------------------------------------------------------
class MigrationFailure(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    NOT_REGISTERED = "NOT_REGISTERED"  # the caller is not a registered account
    NOT_COLLISION = "NOT_COLLISION"  # the intent was not parked for this account
    EXPIRED = "EXPIRED"
    CONFLICT = "CONFLICT"  # another unfinished migration exists for this guest
    UNKNOWN_ITEM = "UNKNOWN_ITEM"
    INTERRUPTED = "INTERRUPTED"  # a copy or the cleanup failed; calling run again resumes


class MigrationError(Exception):
    """Typed, static; never carries data."""

    def __init__(self, failure: MigrationFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


# --- sources -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class ManifestEntry:
    id: str
    kind: str
    title: str


class MigrationSource(Protocol):
    """One kind of guest server data. History, saved places and plans register theirs later."""

    name: str

    def manifest(self, src_uid: str, now: datetime) -> Sequence[ManifestEntry]: ...

    def plan(self, dst_uid: str, entry_id: str, preference: PreferenceChoice,
             migration_id: str, now: datetime) -> str:
        """Where the copy will land. Decided once, when the run starts (side-effect free)."""
        ...

    def copy(self, src_uid: str, dst_uid: str, entry_id: str, target: str,
             idempotency_key: str, now: datetime) -> None:
        """Copy one item. Idempotent: running it twice for the same target and key is harmless."""
        ...


class WorkspaceSource:
    """The guest's server workspace summary (user text only)."""

    name = "workspace"

    def __init__(self, workspaces: WorkspaceStore) -> None:
        self._workspaces = workspaces

    def manifest(self, src_uid: str, now: datetime) -> Sequence[ManifestEntry]:
        workspace = self._workspaces.get(src_uid, DEFAULT_WORKSPACE, now)
        if workspace is None or not (workspace.goal or workspace.facts):
            return ()
        title = (workspace.goal or "Outing workspace")[:TITLE_MAX]
        return (ManifestEntry("workspace-default", "workspace", title),)

    def plan(self, dst_uid: str, entry_id: str, preference: PreferenceChoice,
             migration_id: str, now: datetime) -> str:
        if preference == PreferenceChoice.USE_GUEST:
            return DEFAULT_WORKSPACE
        if self._workspaces.get(dst_uid, DEFAULT_WORKSPACE, now) is None:
            return DEFAULT_WORKSPACE
        return f"migrated-{migration_id[:16]}"  # the registered workspace is left untouched

    def copy(self, src_uid: str, dst_uid: str, entry_id: str, target: str,
             idempotency_key: str, now: datetime) -> None:
        source = self._workspaces.get(src_uid, DEFAULT_WORKSPACE, now)
        if source is None:
            return  # expired meanwhile: nothing left to copy
        kept = [f for f in source.facts if f.source != FactSource.TOOL]  # no provider-derived text
        kept_ids = {f.id for f in kept}
        facts = [evolve(f, evidence_ids=(),
                        depends_on=tuple(d for d in f.depends_on if d in kept_ids))
                 for f in kept]
        # Evidence and pending actions belong to the guest session (display cache entries, nonces
        # bound to the guest): they do not travel. The copy restarts as a captured workspace.
        copied = evolve(source, owner_id=dst_uid, facts=facts, evidence=[], pending_actions=[],
                        active_plan_version=None, status=WorkspaceStatus.CAPTURED)
        self._workspaces.put(dst_uid, target, copied, now,
                             timedelta(days=REGISTERED_WORKSPACE_INACTIVITY_DAYS))


# --- service -----------------------------------------------------------------------------------
class MigrationService:
    def __init__(
        self, store: AccountStore, sources: Sequence[MigrationSource],
        cleanup_guest: Callable[[str], bool], clock: Callable[[], datetime] | None = None,
    ) -> None:
        names = [s.name for s in sources]
        if len(set(names)) != len(names):
            raise ValueError("duplicate migration source name")
        self._store = store
        self._sources = {s.name: s for s in sources}
        self._cleanup = cleanup_guest
        self._clock = clock or (lambda: datetime.now(UTC))

    def add_source(self, source: MigrationSource) -> None:
        if source.name in self._sources:
            raise ValueError("duplicate migration source name")
        self._sources[source.name] = source

    # -- step 2: authenticate both identities, show the manifest
    def start(self, dst_uid: str, dst_is_registered: bool, intent_id: str, nonce: str,
              idempotency_key: str | None) -> MigrationView:
        if not dst_is_registered:
            raise MigrationError(MigrationFailure.NOT_REGISTERED)
        now = self._clock()
        intent = self._store.get_intent(intent_id)
        if intent is None or not hmac.compare_digest(intent.nonce_hash, nonce_digest(nonce)):
            raise MigrationError(MigrationFailure.NOT_FOUND)  # unknown, wrong nonce: one answer
        if (intent.state != IntentState.COLLISION
                or intent.collision_uid_hash != uid_hash(dst_uid) or intent.owner_uid == dst_uid):
            raise MigrationError(MigrationFailure.NOT_COLLISION)
        if now >= intent.expires_at:
            raise MigrationError(MigrationFailure.EXPIRED)
        items: list[MigrationItem] = []
        for source in self._sources.values():
            for entry in source.manifest(intent.owner_uid, now):
                items.append(MigrationItem(
                    id=entry.id, source=source.name, kind=entry.kind, title=entry.title[:TITLE_MAX]))
        candidate = MigrationRecord(
            id=f"mig-{uuid4().hex[:24]}", src_uid=intent.owner_uid, dst_uid=dst_uid,
            intent_id=intent_id, key_hash=key_digest(idempotency_key or f"intent:{intent_id}"),
            state=MigrationState.PREVIEW, items=tuple(items), created_at=now, updated_at=now)
        record, _created = self._store.create_migration(candidate)
        if record.dst_uid != dst_uid:
            raise MigrationError(MigrationFailure.CONFLICT)
        return view_of(record)

    def get(self, dst_uid: str, migration_id: str) -> MigrationView:
        return view_of(self._owned(dst_uid, migration_id))

    def _owned(self, dst_uid: str, migration_id: str) -> MigrationRecord:
        record = self._store.get_migration(migration_id)
        if record is None or record.dst_uid != dst_uid:
            raise MigrationError(MigrationFailure.NOT_FOUND)  # another tenant's id is "unknown"
        return record

    # -- steps 3 and 4: copy, resume, then (only then) delete the guest's server data
    def run(self, dst_uid: str, migration_id: str, item_ids: Sequence[str],
            preference: PreferenceChoice) -> MigrationView:
        record = self._owned(dst_uid, migration_id)
        now = self._clock()
        if record.state == MigrationState.COMPLETED:
            return view_of(record)
        if record.state == MigrationState.PREVIEW:
            record = self._begin(record, item_ids, preference, now)
        if record.state == MigrationState.RUNNING:
            record = self._copy_selected(record)
        if record.state == MigrationState.CLEANING:
            record = self._finish(record)
        return view_of(record)

    def _begin(self, record: MigrationRecord, item_ids: Sequence[str],
               preference: PreferenceChoice, now: datetime) -> MigrationRecord:
        known = {i.id for i in record.items}
        chosen = set(item_ids)
        if not chosen <= known:
            raise MigrationError(MigrationFailure.UNKNOWN_ITEM)
        targets = {
            i.id: self._sources[i.source].plan(
                record.dst_uid, i.id, preference, record.id, now)
            for i in record.items if i.id in chosen}

        def start(current: MigrationRecord) -> MigrationRecord:
            if current.state != MigrationState.PREVIEW:
                return current  # a concurrent call already began: its selection stands
            return replace(
                current, state=MigrationState.RUNNING, preference=preference.value, updated_at=now,
                items=tuple(replace(i, selected=i.id in chosen, target=targets.get(i.id))
                            for i in current.items))

        updated = self._store.update_migration(record.id, start)
        if updated is None:
            raise MigrationError(MigrationFailure.NOT_FOUND)
        return updated

    def _copy_selected(self, record: MigrationRecord) -> MigrationRecord:
        for item in record.items:
            if not item.selected or item.state == ItemState.COPIED:
                continue
            source = self._sources.get(item.source)
            if source is None or item.target is None:
                raise MigrationError(MigrationFailure.INTERRUPTED)
            try:
                source.copy(record.src_uid, record.dst_uid, item.id, item.target,
                            f"{record.id}:{item.id}", self._clock())
            except Exception:  # noqa: BLE001 - static typed failure; nothing about the data leaks
                raise MigrationError(MigrationFailure.INTERRUPTED) from None
            self._mark_copied(record.id, item.id)
        return self._advance_to_cleaning(record.id)

    def _mark_copied(self, migration_id: str, item_id: str) -> None:
        def mark(current: MigrationRecord) -> MigrationRecord:
            return replace(current, items=tuple(
                replace(i, state=ItemState.COPIED) if i.id == item_id else i
                for i in current.items))

        self._store.update_migration(migration_id, mark)

    def _advance_to_cleaning(self, migration_id: str) -> MigrationRecord:
        now = self._clock()

        def advance(current: MigrationRecord) -> MigrationRecord:
            if current.state != MigrationState.RUNNING:
                return current
            if any(i.selected and i.state != ItemState.COPIED for i in current.items):
                return current  # a concurrent run has not finished copying: stay RUNNING
            return replace(current, state=MigrationState.CLEANING, updated_at=now)

        updated = self._store.update_migration(migration_id, advance)
        if updated is None:
            raise MigrationError(MigrationFailure.NOT_FOUND)
        return updated

    def _finish(self, record: MigrationRecord) -> MigrationRecord:
        try:
            deleted = self._cleanup(record.src_uid)
        except Exception:  # noqa: BLE001
            deleted = False
        if not deleted:
            raise MigrationError(MigrationFailure.INTERRUPTED)  # still CLEANING: resumable
        now = self._clock()

        def complete(current: MigrationRecord) -> MigrationRecord:
            # Once complete the journal keeps no user text and only a digest of the guest's uid.
            return replace(
                current, state=MigrationState.COMPLETED, completed_at=now, updated_at=now,
                src_uid=uid_hash(current.src_uid) if len(current.src_uid) != 64 else current.src_uid,
                items=tuple(replace(i, title="") for i in current.items))  # no user text kept

        updated = self._store.update_migration(record.id, complete)
        if updated is None:
            raise MigrationError(MigrationFailure.NOT_FOUND)
        return updated

    def copied_targets(self, dst_uid: str) -> tuple[str, ...]:
        """Workspace names created by this account's completed or running merges (for export)."""
        names: list[str] = []
        for record in self._store.migrations_for_dst(dst_uid):
            for item in record.items:
                if (item.state == ItemState.COPIED and item.source == "workspace"
                        and item.target):
                    names.append(item.target)
        return tuple(names)


__all__ = [
    "DEFAULT_WORKSPACE", "ManifestEntry", "MigrationError", "MigrationFailure", "MigrationItemView",
    "MigrationRunRequest", "MigrationService", "MigrationSource", "MigrationStartRequest",
    "MigrationState", "MigrationView", "PreferenceChoice", "WorkspaceSource", "view_of",
]
