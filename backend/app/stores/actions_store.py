"""Durable store for server-authored PendingActions (item 6; decisions 15, 26, 27).

One tenant-keyed record per action. The action id is derived from the tenant and the idempotency
key, so one key can never create two records. ``update`` is the only mutation primitive: it runs a
pure function against the current record inside one transaction, so a confirmation is atomic
(single-use nonce, exactly one effect).

What a record holds: the typed ``PendingAction`` (place ID as ``target``, server-written preview,
nonce, expiry), the user's own label, an optional due time, a status and a failed-attempt counter.
Never a coordinate, an address, a Maps name, a URL or any provider content. Every record carries
``schema_version`` and ``kind``; ``expires_at`` (action expiry plus a short grace) is the field a
Firestore TTL policy targets (RUNBOOK TODO), and ``purge`` deletes the same records early.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import ValidationError

from ..domain import (
    SCHEMA_VERSION,
    PendingAction,
    PendingActionEffect,
    RoundedPosition,
    SavedPlaceKind,
)
from .firestore import StoredDataError, _Base, doc_id

T = TypeVar("T")

PENDING_ACTIONS = "pending_actions"
# A record outlives its action by this long so a late confirm gets a typed EXPIRED, not NOT_FOUND.
RECORD_GRACE = timedelta(hours=24)


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ActionRecord:
    owner_id: str
    action: PendingAction
    fingerprint: str  # sha256 of the typed request: same key + other request is a conflict
    status: ActionStatus = ActionStatus.PENDING
    user_label: str | None = None  # the user's own words, as typed
    label_kind: SavedPlaceKind | None = None
    due_at: datetime | None = None
    failed_attempts: int = 0
    # SAVE_PLACE only: the user's consented position, rounded on parse. Held ONLY until confirm
    # (then it lives in the saved place and is scrubbed here), cancel or expiry. A record that
    # holds one gets no grace period: it expires with its action.
    position: RoundedPosition | None = None
    schema_version: int = SCHEMA_VERSION
    kind: str = "pending_action_record"

    @property
    def expires_at(self) -> datetime:
        if self.position is not None:
            return self.action.expires_at
        return self.action.expires_at + RECORD_GRACE

    def __repr__(self) -> str:  # never the label, place ID or nonce
        return f"ActionRecord(status={self.status.value})"


@runtime_checkable
class PendingActionStore(Protocol):
    def get(self, owner_id: str, action_id: str) -> ActionRecord | None: ...

    def update(
        self, owner_id: str, action_id: str,
        fn: Callable[[ActionRecord | None], tuple[ActionRecord | None, T]],
    ) -> T:
        """Atomically run ``fn(current)``; a returned record replaces the stored one, None writes
        nothing. ``fn`` may run more than once (a retried transaction), so it must be pure."""
        ...

    def purge(self, now: datetime) -> int: ...

    def delete_owner(self, owner_id: str) -> int: ...

    def delete_save_place_records(self, owner_id: str) -> int:
        """Delete the owner's SAVE_PLACE records (they hold the user's label and, until confirm,
        position): the SAVED_PLACE consent was withdrawn. Returns how many were deleted."""
        ...


def _check(owner_id: str, record: ActionRecord | None, action_id: str) -> None:
    if record is not None and (record.owner_id != owner_id or record.action.id != action_id):
        raise StoredDataError("action record does not belong to this tenant")


class MemoryPendingActionStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], ActionRecord] = {}
        self._lock = threading.Lock()

    def get(self, owner_id: str, action_id: str) -> ActionRecord | None:
        with self._lock:
            return self.items.get((owner_id, action_id))

    def update(
        self, owner_id: str, action_id: str,
        fn: Callable[[ActionRecord | None], tuple[ActionRecord | None, T]],
    ) -> T:
        with self._lock:
            new, result = fn(self.items.get((owner_id, action_id)))
            if new is not None:
                _check(owner_id, new, action_id)
                self.items[(owner_id, action_id)] = new
            return result

    def purge(self, now: datetime) -> int:
        with self._lock:
            stale = [k for k, r in self.items.items() if r.expires_at <= now]
            for key in stale:
                del self.items[key]
            return len(stale)

    def delete_owner(self, owner_id: str) -> int:
        with self._lock:
            mine = [k for k in self.items if k[0] == owner_id]
            for key in mine:
                del self.items[key]
            return len(mine)

    def delete_save_place_records(self, owner_id: str) -> int:
        with self._lock:
            mine = [k for k, r in self.items.items()
                    if k[0] == owner_id and r.action.effect == PendingActionEffect.SAVE_PLACE]
            for key in mine:
                del self.items[key]
            return len(mine)


class FirestorePendingActionStore(_Base):
    def _ref(self, owner_id: str, action_id: str) -> Any:
        return self._col(PENDING_ACTIONS).document(doc_id(owner_id, action_id))

    @staticmethod
    def _decode(owner_id: str, action_id: str, doc: dict[str, Any]) -> ActionRecord:
        if (doc.get("owner_id") != owner_id or doc.get("kind") != "pending_action_record"
                or doc.get("action", {}).get("id") != action_id):
            raise StoredDataError("action record does not belong to this tenant")
        try:
            due = doc.get("due_at")
            return ActionRecord(
                owner_id=owner_id, action=PendingAction.model_validate(doc["action"]),
                fingerprint=str(doc["fingerprint"]), status=ActionStatus(doc["status"]),
                user_label=doc.get("user_label"),
                label_kind=SavedPlaceKind(doc["label_kind"]) if doc.get("label_kind") else None,
                due_at=datetime.fromisoformat(due) if due else None,
                failed_attempts=int(doc.get("failed_attempts", 0)),
                position=(RoundedPosition(**doc["position"]) if doc.get("position") else None))
        except (ValidationError, KeyError, ValueError, TypeError):
            raise StoredDataError("action record does not match the schema") from None

    @staticmethod
    def _encode(record: ActionRecord, now: datetime | None) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "kind": "pending_action_record",
            "owner_id": record.owner_id, "action": record.action.model_dump(mode="json"),
            "fingerprint": record.fingerprint, "status": record.status.value,
            "user_label": record.user_label,
            "label_kind": record.label_kind.value if record.label_kind else None,
            "due_at": record.due_at.isoformat() if record.due_at else None,
            "failed_attempts": record.failed_attempts, "expires_at": record.expires_at,
            "updated_at": record.action.confirmed_at or record.action.created_at}
        if record.position is not None:  # only a SAVE_PLACE proposal, until confirm/cancel/expiry
            doc["position"] = {"latitude": record.position.latitude,
                               "longitude": record.position.longitude}
        return doc

    def get(self, owner_id: str, action_id: str) -> ActionRecord | None:
        snap = self._ref(owner_id, action_id).get()
        return self._decode(owner_id, action_id, snap.to_dict()) if snap.exists else None

    def update(
        self, owner_id: str, action_id: str,
        fn: Callable[[ActionRecord | None], tuple[ActionRecord | None, T]],
    ) -> T:
        ref = self._ref(owner_id, action_id)

        def body(txn: Any) -> T:
            snap = ref.get(transaction=txn)
            current = self._decode(owner_id, action_id, snap.to_dict()) if snap.exists else None
            new, result = fn(current)
            if new is not None:
                _check(owner_id, new, action_id)
                txn.set(ref, self._encode(new, None))
            return result

        return self._transact(body)

    def purge(self, now: datetime) -> int:
        removed = 0
        for snap in self._col(PENDING_ACTIONS).where("expires_at", "<=", now).stream():
            snap.reference.delete()
            removed += 1
        return removed

    def delete_owner(self, owner_id: str) -> int:
        count = 0
        for snap in self._col(PENDING_ACTIONS).where("owner_id", "==", owner_id).stream():
            snap.reference.delete()
            count += 1
        return count

    def delete_save_place_records(self, owner_id: str) -> int:
        count = 0
        for snap in self._col(PENDING_ACTIONS).where("owner_id", "==", owner_id).stream():
            if snap.to_dict().get("action", {}).get("effect") == PendingActionEffect.SAVE_PLACE.value:
                snap.reference.delete()
                count += 1
        return count
