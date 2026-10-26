"""Durable store for the opt-in outing history (Phase 1, built dark: decisions 18, 19, 25, 27).

One document per ``OutingRecord`` (collection ``history_records``) and one small per-owner settings
document (``history_settings``, the retention the user chose). Same client surface as
``firestore.py``; the tests run the adapter over ``fake_firestore``.

What a record document holds: the typed ``OutingRecord``: a place ID, the user's own label, an
activity tag derived from the user's own request or chosen by the user, timestamps, the outcome
events, the user's feedback and the consent id. Never Maps content: no name, address, hours,
rating, category, review, link or coordinate has a field in that model.

Rules, all enforced here:

* the document id is a digest of the tenant and the record id; ``owner_id`` is stored and verified
  on every read; every document has ``schema_version`` and ``kind``;
* ``expires_at`` (the record's ``retention_expires_at``) is the field a Firestore TTL policy
  targets (RUNBOOK TODO). TTL deletion is late, so every read ALSO hides an expired record, and
  ``purge`` deletes the same records early;
* ``put`` is idempotent on the record id (derived from the tenant and the idempotency key by the
  service): a replay returns the stored record, a changed request on the same id is a conflict;
* shortening the retention purges at once and brings the survivors' expiry forward. Lengthening
  never extends a stored record (it applies to records written afterwards).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ValidationError

from ..domain import SCHEMA_VERSION, OutingRecord, evolve
from .firestore import DEFAULT_MAX_ATTEMPTS, StoredDataError, _Base, doc_id

HISTORY_RECORDS = "history_records"
HISTORY_SETTINGS = "history_settings"
HISTORY_COLLECTIONS = (HISTORY_RECORDS, HISTORY_SETTINGS)
MAX_RECORDS_PER_OWNER = 1_000  # a typed LIMIT beyond this; the export contract allows 10,000

PutOutcome = Literal["created", "replayed", "conflict", "full"]


@dataclass(frozen=True)
class StoredHistory:
    record: OutingRecord
    fingerprint: str = field(repr=False)

    def __repr__(self) -> str:  # never the label, the place ID or the note
        return "StoredHistory(<redacted>)"


@runtime_checkable
class HistoryStore(Protocol):
    def put(self, owner_id: str, record: OutingRecord, fingerprint: str
            ) -> tuple[OutingRecord, PutOutcome]: ...
    def get(self, owner_id: str, record_id: str, now: datetime) -> OutingRecord | None: ...
    def list(self, owner_id: str, now: datetime) -> tuple[OutingRecord, ...]:
        """Unexpired records, newest first."""
        ...
    def update(self, owner_id: str, record_id: str,
               fn: Callable[[OutingRecord], OutingRecord], now: datetime) -> OutingRecord | None:
        """Atomically replace a record with ``fn(record)`` (pure: it may run more than once)."""
        ...
    def delete(self, owner_id: str, record_id: str) -> bool: ...
    def delete_records(self, owner_id: str) -> int:
        """Every record of the owner (the user's "clear all"); the retention choice stays."""
        ...
    def delete_all(self, owner_id: str) -> int:
        """Every record AND the settings of the owner. Returns the number of records."""
        ...
    def retention_days(self, owner_id: str) -> int | None: ...
    def apply_retention(self, owner_id: str, days: int, now: datetime) -> int:
        """Store ``days``, purge records already past it, bring the rest forward. Returns the
        number purged."""
        ...
    def purge(self, now: datetime) -> int: ...


def _check_owner(owner_id: str, record: OutingRecord) -> None:
    if record.owner_id != owner_id:
        raise ValueError("a history record belongs to its owner only")


def _cut(record: OutingRecord, days: int) -> datetime:
    return min(record.retention_expires_at, record.occurred_at + timedelta(days=days))


class MemoryHistoryStore:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, StoredHistory]] = {}
        self.settings: dict[str, int] = {}
        self._lock = threading.Lock()

    def put(self, owner_id: str, record: OutingRecord, fingerprint: str
            ) -> tuple[OutingRecord, PutOutcome]:
        _check_owner(owner_id, record)
        with self._lock:
            rows = self.items.setdefault(owner_id, {})
            existing = rows.get(record.id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    return existing.record, "conflict"
                return existing.record, "replayed"
            live = [r for r in rows.values() if r.record.retention_expires_at > record.occurred_at]
            if len(live) >= MAX_RECORDS_PER_OWNER:
                return record, "full"
            rows[record.id] = StoredHistory(record, fingerprint)
            return record, "created"

    def get(self, owner_id: str, record_id: str, now: datetime) -> OutingRecord | None:
        with self._lock:
            row = self.items.get(owner_id, {}).get(record_id)
            if row is None or now >= row.record.retention_expires_at:
                return None
            return row.record

    def list(self, owner_id: str, now: datetime) -> tuple[OutingRecord, ...]:
        with self._lock:
            rows = [r.record for r in self.items.get(owner_id, {}).values()
                    if now < r.record.retention_expires_at]
        return tuple(sorted(rows, key=lambda r: (r.occurred_at, r.id), reverse=True))

    def update(self, owner_id: str, record_id: str,
               fn: Callable[[OutingRecord], OutingRecord], now: datetime) -> OutingRecord | None:
        with self._lock:
            rows = self.items.get(owner_id, {})
            row = rows.get(record_id)
            if row is None or now >= row.record.retention_expires_at:
                return None
            new = fn(row.record)
            _check_owner(owner_id, new)
            if new.id != record_id:
                raise ValueError("a record id never changes")
            rows[record_id] = StoredHistory(new, row.fingerprint)
            return new

    def delete(self, owner_id: str, record_id: str) -> bool:
        with self._lock:
            return self.items.get(owner_id, {}).pop(record_id, None) is not None

    def delete_records(self, owner_id: str) -> int:
        with self._lock:
            return len(self.items.pop(owner_id, {}))

    def delete_all(self, owner_id: str) -> int:
        with self._lock:
            self.settings.pop(owner_id, None)
            return len(self.items.pop(owner_id, {}))

    def retention_days(self, owner_id: str) -> int | None:
        with self._lock:
            return self.settings.get(owner_id)

    def apply_retention(self, owner_id: str, days: int, now: datetime) -> int:
        with self._lock:
            self.settings[owner_id] = days
            rows = self.items.get(owner_id, {})
            purged = 0
            for record_id, row in list(rows.items()):
                expiry = _cut(row.record, days)
                if expiry <= now:
                    del rows[record_id]
                    purged += 1
                elif expiry != row.record.retention_expires_at:
                    rows[record_id] = StoredHistory(
                        evolve(row.record, retention_expires_at=expiry), row.fingerprint)
            return purged

    def purge(self, now: datetime) -> int:
        with self._lock:
            removed = 0
            for rows in self.items.values():
                for record_id, row in list(rows.items()):
                    if row.record.retention_expires_at <= now:
                        del rows[record_id]
                        removed += 1
            return removed


class FirestoreHistoryStore(_Base):
    def _ref(self, owner_id: str, record_id: str) -> Any:
        return self._col(HISTORY_RECORDS).document(doc_id(owner_id, record_id))

    def _settings_ref(self, owner_id: str) -> Any:
        return self._col(HISTORY_SETTINGS).document(doc_id(owner_id))

    @staticmethod
    def _decode(owner_id: str, doc: dict[str, Any]) -> StoredHistory:
        if doc.get("owner_id") != owner_id or doc.get("kind") != "history_record":
            raise StoredDataError("history record does not belong to this tenant")
        try:
            record = OutingRecord.model_validate(doc["record"])
            fingerprint = str(doc["fingerprint"])
        except (ValidationError, KeyError, TypeError):
            raise StoredDataError("history record does not match the schema") from None
        if record.owner_id != owner_id:
            raise StoredDataError("history record does not belong to this tenant")
        return StoredHistory(record, fingerprint)

    @staticmethod
    def _encode(owner_id: str, row: StoredHistory) -> dict[str, Any]:
        record = row.record
        return {
            "schema_version": SCHEMA_VERSION, "kind": "history_record", "owner_id": owner_id,
            "record": record.model_dump(mode="json"), "fingerprint": row.fingerprint,
            "occurred_at": record.occurred_at, "expires_at": record.retention_expires_at}

    def _rows(self, owner_id: str, transaction: Any = None) -> list[tuple[Any, StoredHistory]]:
        query = self._col(HISTORY_RECORDS).where("owner_id", "==", owner_id)
        snaps = query.stream(transaction=transaction) if transaction is not None else query.stream()
        return [(snap.reference, self._decode(owner_id, snap.to_dict())) for snap in snaps]

    def put(self, owner_id: str, record: OutingRecord, fingerprint: str
            ) -> tuple[OutingRecord, PutOutcome]:
        _check_owner(owner_id, record)
        ref = self._ref(owner_id, record.id)

        def body(txn: Any) -> tuple[OutingRecord, PutOutcome]:
            snap = ref.get(transaction=txn)
            if snap.exists:
                existing = self._decode(owner_id, snap.to_dict())
                return existing.record, ("replayed" if existing.fingerprint == fingerprint
                                         else "conflict")
            live = [r for _ref, r in self._rows(owner_id, txn)
                    if r.record.retention_expires_at > record.occurred_at]
            if len(live) >= MAX_RECORDS_PER_OWNER:
                return record, "full"
            txn.set(ref, self._encode(owner_id, StoredHistory(record, fingerprint)))
            return record, "created"

        return self._transact(body)

    def get(self, owner_id: str, record_id: str, now: datetime) -> OutingRecord | None:
        snap = self._ref(owner_id, record_id).get()
        if not snap.exists:
            return None
        record = self._decode(owner_id, snap.to_dict()).record
        return None if now >= record.retention_expires_at else record

    def list(self, owner_id: str, now: datetime) -> tuple[OutingRecord, ...]:
        rows = [r.record for _ref, r in self._rows(owner_id) if now < r.record.retention_expires_at]
        return tuple(sorted(rows, key=lambda r: (r.occurred_at, r.id), reverse=True))

    def update(self, owner_id: str, record_id: str,
               fn: Callable[[OutingRecord], OutingRecord], now: datetime) -> OutingRecord | None:
        ref = self._ref(owner_id, record_id)

        def body(txn: Any) -> OutingRecord | None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return None
            row = self._decode(owner_id, snap.to_dict())
            if now >= row.record.retention_expires_at:
                return None
            new = fn(row.record)
            _check_owner(owner_id, new)
            if new.id != record_id:
                raise ValueError("a record id never changes")
            txn.set(ref, self._encode(owner_id, StoredHistory(new, row.fingerprint)))
            return new

        return self._transact(body)

    def delete(self, owner_id: str, record_id: str) -> bool:
        ref = self._ref(owner_id, record_id)

        def body(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            self._decode(owner_id, snap.to_dict())  # verifies the tenant before deleting
            txn.delete(ref)
            return True

        return self._transact(body)

    def delete_records(self, owner_id: str) -> int:
        count = 0
        for snap in self._col(HISTORY_RECORDS).where("owner_id", "==", owner_id).stream():
            snap.reference.delete()
            count += 1
        return count

    def delete_all(self, owner_id: str) -> int:
        count = self.delete_records(owner_id)
        self._settings_ref(owner_id).delete()
        return count

    def retention_days(self, owner_id: str) -> int | None:
        snap = self._settings_ref(owner_id).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        if doc.get("owner_id") != owner_id or doc.get("kind") != "history_settings":
            raise StoredDataError("history settings do not belong to this tenant")
        days = doc.get("retention_days")
        if not isinstance(days, int):
            raise StoredDataError("history settings do not match the schema")
        return days

    def apply_retention(self, owner_id: str, days: int, now: datetime) -> int:
        self._settings_ref(owner_id).set({
            "schema_version": SCHEMA_VERSION, "kind": "history_settings", "owner_id": owner_id,
            "retention_days": days, "updated_at": now})
        purged = 0
        for ref, row in self._rows(owner_id):
            expiry = _cut(row.record, days)
            if expiry <= now:
                ref.delete()
                purged += 1
            elif expiry != row.record.retention_expires_at:
                self._shorten(ref, owner_id, days)
        return purged

    def _shorten(self, ref: Any, owner_id: str, days: int) -> None:
        def body(txn: Any) -> None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            row = self._decode(owner_id, snap.to_dict())
            expiry = _cut(row.record, days)
            if expiry != row.record.retention_expires_at:
                txn.set(ref, self._encode(owner_id, StoredHistory(
                    evolve(row.record, retention_expires_at=expiry), row.fingerprint)))

        self._transact(body)

    def purge(self, now: datetime) -> int:
        removed = 0
        for snap in self._col(HISTORY_RECORDS).where("expires_at", "<=", now).stream():
            snap.reference.delete()
            removed += 1
        return removed


def memory_history_store() -> HistoryStore:
    return MemoryHistoryStore()


def firestore_history_store(
    client: Any, *, transactional: Callable[..., Any] | None = None,
    max_attempts: int | None = None,
) -> HistoryStore:
    return FirestoreHistoryStore(
        client, transactional=transactional, max_attempts=max_attempts or DEFAULT_MAX_ATTEMPTS)


__all__ = [
    "HISTORY_COLLECTIONS", "HISTORY_RECORDS", "HISTORY_SETTINGS", "MAX_RECORDS_PER_OWNER",
    "FirestoreHistoryStore", "HistoryStore", "MemoryHistoryStore", "StoredHistory",
    "firestore_history_store", "memory_history_store",
]
