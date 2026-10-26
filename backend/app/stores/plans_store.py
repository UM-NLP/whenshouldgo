"""Plan versions, outcome check-ins and edit receipts (Trip Board server, decisions 18, 19, 27).

Own protocols, a memory implementation and a Firestore adapter (same client surface as
``firestore.py``; tests run it over ``fake_firestore``). Nothing here can hold Maps content:

* ``PlanVersion`` has only ids, enums, timestamps and the user's own label; grounded evidence is a
  pointer into the display cache that may expire;
* an outcome entry has a place ID, an enum, the user's note and a timestamp;
* an edit receipt has a key digest, a request fingerprint and a version number.

Time is always passed in. A guest's documents carry an expiry refreshed by every write (the same
inactive-days rule as the workspace); every read also checks expiry itself.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from ..domain import (
    MAX_OUTING_RETENTION,
    SCHEMA_VERSION,
    OutcomeCheckin,
    OutcomeValue,
    PlanVersion,
)
from .firestore import DEFAULT_MAX_ATTEMPTS, StoredDataError, _Base, doc_id

MAX_OUTCOMES = 500  # per owner; the oldest are dropped first
RECEIPT_RETENTION = timedelta(days=7)

PLAN_HEADS = "plan_heads"
PLAN_VERSIONS = "plan_versions"
OUTCOME_LEDGERS = "outcome_ledgers"
EDIT_RECEIPTS = "edit_receipts"
PLAN_COLLECTIONS = (PLAN_HEADS, PLAN_VERSIONS, OUTCOME_LEDGERS, EDIT_RECEIPTS)


class PlanConflict(Exception):
    """The new version does not follow the stored latest one (a concurrent writer). Static text."""

    def __init__(self) -> None:
        super().__init__("plan version does not follow the latest version")


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# --- plan versions ---------------------------------------------------------------------------
@runtime_checkable
class PlanVersionStore(Protocol):
    """Immutable, append-only plan versions per (owner, workspace). A stored version is never
    changed; ``append`` is one transaction that checks the lineage against the stored latest."""

    def append(self, owner_id: str, plan: PlanVersion, now: datetime,
               ttl: timedelta | None) -> None: ...
    def get(self, owner_id: str, workspace_id: str, version: int, now: datetime
            ) -> PlanVersion | None: ...
    def latest(self, owner_id: str, workspace_id: str, now: datetime) -> PlanVersion | None: ...
    def history(self, owner_id: str, workspace_id: str, now: datetime) -> tuple[PlanVersion, ...]: ...
    def delete_workspace(self, owner_id: str, workspace_id: str) -> None: ...
    def delete_owner(self, owner_id: str) -> int:
        """Every version of every workspace of ``owner_id`` (account deletion). Returns a count."""
        ...


def _check_lineage(plan: PlanVersion, latest: int) -> None:
    expected_parent = latest or None
    if plan.version != latest + 1 or plan.parent_version != expected_parent:
        raise PlanConflict()


class MemoryPlanVersionStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], list[PlanVersion]] = {}
        self._expires: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()

    def _live(self, key: tuple[str, str], now: datetime) -> list[PlanVersion]:
        expires = self._expires.get(key)
        if expires is not None and now >= expires:
            self.items.pop(key, None)
            del self._expires[key]
        return self.items.get(key, [])

    def append(self, owner_id: str, plan: PlanVersion, now: datetime,
               ttl: timedelta | None) -> None:
        key = (owner_id, plan.workspace_id)
        with self._lock:
            versions = self._live(key, now)
            _check_lineage(plan, versions[-1].version if versions else 0)
            self.items.setdefault(key, versions).append(plan)
            if ttl is None:
                self._expires.pop(key, None)
            else:
                self._expires[key] = now + ttl

    def get(self, owner_id: str, workspace_id: str, version: int, now: datetime
            ) -> PlanVersion | None:
        with self._lock:
            for plan in self._live((owner_id, workspace_id), now):
                if plan.version == version:
                    return plan
        return None

    def latest(self, owner_id: str, workspace_id: str, now: datetime) -> PlanVersion | None:
        with self._lock:
            versions = self._live((owner_id, workspace_id), now)
            return versions[-1] if versions else None

    def history(self, owner_id: str, workspace_id: str, now: datetime) -> tuple[PlanVersion, ...]:
        with self._lock:
            return tuple(self._live((owner_id, workspace_id), now))

    def delete_workspace(self, owner_id: str, workspace_id: str) -> None:
        with self._lock:
            self.items.pop((owner_id, workspace_id), None)
            self._expires.pop((owner_id, workspace_id), None)

    def delete_owner(self, owner_id: str) -> int:
        with self._lock:
            mine = [key for key in self.items if key[0] == owner_id]
            for key in mine:
                del self.items[key]
                self._expires.pop(key, None)
            return len(mine)


class FirestorePlanVersionStore(_Base):
    """One head document (the latest version number) and one document per immutable version."""

    def _head(self, owner_id: str, workspace_id: str) -> Any:
        return self._col(PLAN_HEADS).document(doc_id(owner_id, workspace_id))

    def _ver(self, owner_id: str, workspace_id: str, version: int) -> Any:
        return self._col(PLAN_VERSIONS).document(doc_id(owner_id, workspace_id, str(version)))

    @staticmethod
    def _head_latest(owner_id: str, workspace_id: str, doc: dict[str, Any] | None,
                     now: datetime) -> int:
        if doc is None:
            return 0
        if (doc.get("owner_id") != owner_id or doc.get("workspace_id") != workspace_id
                or doc.get("kind") != "plan_head"):
            raise StoredDataError("plan head does not belong to this tenant")
        expires = doc.get("expires_at")
        if expires is not None and now >= expires:
            return 0
        return int(doc["latest_version"])

    @staticmethod
    def _decode(owner_id: str, workspace_id: str, doc: dict[str, Any]) -> PlanVersion:
        if (doc.get("owner_id") != owner_id or doc.get("workspace_id") != workspace_id
                or doc.get("kind") != "plan_version_record"):
            raise StoredDataError("plan version does not belong to this tenant")
        try:
            plan = PlanVersion.model_validate(doc["plan"])
        except (ValidationError, KeyError):
            raise StoredDataError("plan version does not match the schema") from None
        if plan.workspace_id != workspace_id:
            raise StoredDataError("plan version belongs to another workspace")
        return plan

    def append(self, owner_id: str, plan: PlanVersion, now: datetime,
               ttl: timedelta | None) -> None:
        workspace_id = plan.workspace_id
        head, ref = self._head(owner_id, workspace_id), self._ver(owner_id, workspace_id, plan.version)

        def body(txn: Any) -> None:
            snap = head.get(transaction=txn)
            latest = self._head_latest(owner_id, workspace_id,
                                       snap.to_dict() if snap.exists else None, now)
            _check_lineage(plan, latest)
            expires = {"expires_at": now + ttl} if ttl is not None else {}
            txn.set(ref, {
                "schema_version": SCHEMA_VERSION, "kind": "plan_version_record",
                "owner_id": owner_id, "workspace_id": workspace_id, "version": plan.version,
                "plan": plan.model_dump(mode="json"), "created_at": now, **expires})
            txn.set(head, {
                "schema_version": SCHEMA_VERSION, "kind": "plan_head", "owner_id": owner_id,
                "workspace_id": workspace_id, "latest_version": plan.version,
                "updated_at": now, **expires})

        self._transact(body)

    def _read(self, owner_id: str, workspace_id: str, version: int, now: datetime
              ) -> PlanVersion | None:
        snap = self._ver(owner_id, workspace_id, version).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        expires = doc.get("expires_at")
        if expires is not None and now >= expires:
            return None
        return self._decode(owner_id, workspace_id, doc)

    def get(self, owner_id: str, workspace_id: str, version: int, now: datetime
            ) -> PlanVersion | None:
        if version < 1:
            return None
        return self._read(owner_id, workspace_id, version, now)

    def _latest_number(self, owner_id: str, workspace_id: str, now: datetime) -> int:
        snap = self._head(owner_id, workspace_id).get()
        return self._head_latest(owner_id, workspace_id, snap.to_dict() if snap.exists else None,
                                 now)

    def latest(self, owner_id: str, workspace_id: str, now: datetime) -> PlanVersion | None:
        number = self._latest_number(owner_id, workspace_id, now)
        return self._read(owner_id, workspace_id, number, now) if number else None

    def history(self, owner_id: str, workspace_id: str, now: datetime) -> tuple[PlanVersion, ...]:
        number = self._latest_number(owner_id, workspace_id, now)
        plans = (self._read(owner_id, workspace_id, v, now) for v in range(1, number + 1))
        return tuple(p for p in plans if p is not None)

    def delete_workspace(self, owner_id: str, workspace_id: str) -> None:
        number = self._latest_number(owner_id, workspace_id, _EPOCH)  # ignore expiry: delete all
        for version in range(1, number + 1):
            self._ver(owner_id, workspace_id, version).delete()
        self._head(owner_id, workspace_id).delete()

    def delete_owner(self, owner_id: str) -> int:
        removed = 0
        for collection in (PLAN_VERSIONS, PLAN_HEADS):
            while True:
                rows = list(self._col(collection).where("owner_id", "==", owner_id).limit(200).stream())
                if not rows:
                    break
                for snap in rows:
                    snap.reference.delete()
                    removed += 1
        return removed


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --- outcome check-ins -----------------------------------------------------------------------
@dataclass(frozen=True)
class StoredOutcome:
    key: str  # digest of the idempotency key
    place_id: str
    outcome: OutcomeValue
    recorded_at: datetime
    note: str | None = field(default=None, repr=False)
    # When this entry must be gone: the user's history retention at write time (default: the
    # 180-day ceiling). Shortening the retention brings it forward; ``purge`` deletes it.
    expires_at: datetime | None = None

    def alive(self, now: datetime) -> bool:
        limit = self.expires_at if self.expires_at is not None else (
            self.recorded_at + MAX_OUTING_RETENTION)
        return now < limit


@runtime_checkable
class OutcomeStore(Protocol):
    """Per-owner outcome ledger. ``record`` is idempotent on the key and returns True only when it
    stored a new entry. Entries older than the 180-day ceiling and beyond ``MAX_OUTCOMES`` are
    dropped on write."""

    def record(self, owner_id: str, key: str, checkin: OutcomeCheckin, now: datetime,
               retention_days: int | None = None) -> bool: ...
    def entries(self, owner_id: str, now: datetime) -> tuple[StoredOutcome, ...]: ...
    def delete_all(self, owner_id: str) -> None: ...
    def delete_place(self, owner_id: str, place_id: str) -> int:
        """The user deleted history for this place: drop its entries. Returns a count."""
        ...
    def clear_notes(self, owner_id: str, place_id: str) -> int:
        """Drop the free-text note of this place's entries (the user edited their feedback)."""
        ...
    def apply_retention(self, owner_id: str, days: int, now: datetime) -> int:
        """The user shortened their retention: bring every entry's expiry forward to at most
        ``days`` after it was recorded and delete what is already past. Never extends."""
        ...
    def purge(self, now: datetime) -> int:
        """Delete every owner's expired entries early (the TTL policy is the primary mechanism)."""
        ...


def _prune(entries: list[StoredOutcome], now: datetime) -> list[StoredOutcome]:
    kept = [e for e in entries if e.alive(now)]
    return kept[-MAX_OUTCOMES:]


def _new_entry(digest: str, checkin: OutcomeCheckin, now: datetime,
               retention_days: int | None) -> StoredOutcome:
    span = MAX_OUTING_RETENTION if retention_days is None else min(
        timedelta(days=max(1, retention_days)), MAX_OUTING_RETENTION)
    return StoredOutcome(digest, checkin.place_id, checkin.outcome, now, checkin.note, now + span)


def _shorten(entries: list[StoredOutcome], days: int, now: datetime) -> list[StoredOutcome]:
    cap = timedelta(days=max(1, days))
    out = []
    for e in entries:
        limit = e.recorded_at + cap
        if e.expires_at is None or limit < e.expires_at:
            e = replace(e, expires_at=limit)
        if e.alive(now):
            out.append(e)
    return out


class MemoryOutcomeStore:
    def __init__(self) -> None:
        self.items: dict[str, list[StoredOutcome]] = {}
        self._lock = threading.Lock()

    def record(self, owner_id: str, key: str, checkin: OutcomeCheckin, now: datetime,
               retention_days: int | None = None) -> bool:
        digest = key_digest(key)
        with self._lock:
            entries = _prune(self.items.get(owner_id, []), now)
            if any(e.key == digest for e in entries):
                self.items[owner_id] = entries
                return False
            entries.append(_new_entry(digest, checkin, now, retention_days))
            self.items[owner_id] = _prune(entries, now)
            return True

    def entries(self, owner_id: str, now: datetime) -> tuple[StoredOutcome, ...]:
        with self._lock:
            return tuple(_prune(self.items.get(owner_id, []), now))

    def delete_all(self, owner_id: str) -> None:
        with self._lock:
            self.items.pop(owner_id, None)

    def delete_place(self, owner_id: str, place_id: str) -> int:
        with self._lock:
            rows = self.items.get(owner_id, [])
            kept = [e for e in rows if e.place_id != place_id]
            self.items[owner_id] = kept
            return len(rows) - len(kept)

    def clear_notes(self, owner_id: str, place_id: str) -> int:
        with self._lock:
            rows = self.items.get(owner_id, [])
            changed = sum(1 for e in rows if e.place_id == place_id and e.note is not None)
            self.items[owner_id] = [replace(e, note=None) if e.place_id == place_id else e
                                    for e in rows]
            return changed

    def apply_retention(self, owner_id: str, days: int, now: datetime) -> int:
        with self._lock:
            rows = self.items.get(owner_id, [])
            kept = _shorten(rows, days, now)
            self.items[owner_id] = kept
            return len(rows) - len(kept)

    def purge(self, now: datetime) -> int:
        with self._lock:
            removed = 0
            for owner, rows in list(self.items.items()):
                kept = [e for e in rows if e.alive(now)]
                removed += len(rows) - len(kept)
                self.items[owner] = kept
            return removed


class FirestoreOutcomeStore(_Base):
    def _ref(self, owner_id: str) -> Any:
        return self._col(OUTCOME_LEDGERS).document(doc_id(owner_id))

    @staticmethod
    def _decode(owner_id: str, doc: dict[str, Any]) -> list[StoredOutcome]:
        if doc.get("owner_id") != owner_id or doc.get("kind") != "outcome_ledger":
            raise StoredDataError("outcome ledger does not belong to this tenant")
        try:
            return [StoredOutcome(e["key"], e["place_id"], OutcomeValue(e["outcome"]),
                                  e["recorded_at"], e.get("note"), e.get("expires_at"))
                    for e in doc.get("entries", [])]
        except (KeyError, ValueError, TypeError):
            raise StoredDataError("outcome ledger entry does not match the schema") from None

    @staticmethod
    def _encode(owner_id: str, entries: list[StoredOutcome], now: datetime) -> dict[str, Any]:
        rows = []
        for e in entries:
            row: dict[str, Any] = {"key": e.key, "place_id": e.place_id,
                                   "outcome": e.outcome.value, "recorded_at": e.recorded_at}
            if e.note is not None:
                row["note"] = e.note
            if e.expires_at is not None:
                row["expires_at"] = e.expires_at
            rows.append(row)
        doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "kind": "outcome_ledger", "owner_id": owner_id,
            "entries": rows, "updated_at": now}
        if entries:
            # The document-level TTL target: the latest entry expiry (each entry is also pruned).
            doc["expires_at"] = max(
                e.expires_at if e.expires_at is not None else e.recorded_at + MAX_OUTING_RETENTION
                for e in entries)
        return doc

    def _rewrite(self, owner_id: str, now: datetime,
                 fn: Callable[[list[StoredOutcome]], list[StoredOutcome]]) -> int:
        ref = self._ref(owner_id)

        def body(txn: Any) -> int:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return 0
            rows = self._decode(owner_id, snap.to_dict())
            kept = fn(rows)
            if kept:
                txn.set(ref, self._encode(owner_id, kept, now))
            else:
                txn.delete(ref)
            return len(rows) - len(kept)

        return self._transact(body)

    def record(self, owner_id: str, key: str, checkin: OutcomeCheckin, now: datetime,
               retention_days: int | None = None) -> bool:
        ref, digest = self._ref(owner_id), key_digest(key)

        def body(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            entries = _prune(self._decode(owner_id, snap.to_dict()) if snap.exists else [], now)
            if any(e.key == digest for e in entries):
                return False
            entries.append(_new_entry(digest, checkin, now, retention_days))
            txn.set(ref, self._encode(owner_id, _prune(entries, now), now))
            return True

        return self._transact(body)

    def entries(self, owner_id: str, now: datetime) -> tuple[StoredOutcome, ...]:
        snap = self._ref(owner_id).get()
        return tuple(_prune(self._decode(owner_id, snap.to_dict()), now)) if snap.exists else ()

    def delete_all(self, owner_id: str) -> None:
        self._ref(owner_id).delete()

    def delete_place(self, owner_id: str, place_id: str) -> int:
        return self._rewrite(owner_id, _EPOCH, lambda rows: [
            e for e in rows if e.place_id != place_id])

    def clear_notes(self, owner_id: str, place_id: str) -> int:
        changed: list[StoredOutcome] = []

        def fn(rows: list[StoredOutcome]) -> list[StoredOutcome]:
            changed.clear()
            changed.extend(e for e in rows if e.place_id == place_id and e.note is not None)
            return [replace(e, note=None) if e.place_id == place_id else e for e in rows]

        self._rewrite(owner_id, _EPOCH, fn)
        return len(changed)

    def apply_retention(self, owner_id: str, days: int, now: datetime) -> int:
        return self._rewrite(owner_id, now, lambda rows: _shorten(rows, days, now))

    def purge(self, now: datetime) -> int:
        removed = 0
        for snap in self._col(OUTCOME_LEDGERS).where("expires_at", "<=", now).stream():
            owner = snap.to_dict().get("owner_id")
            if isinstance(owner, str):
                removed += self._rewrite(owner, now, lambda rows: [e for e in rows if e.alive(now)])
        return removed


# --- edit receipts (idempotency of PATCH /v1/workspace) -----------------------------------------
@dataclass(frozen=True)
class EditReceipt:
    fingerprint: str
    version: int  # the workspace version the edit produced


@runtime_checkable
class EditReceiptStore(Protocol):
    def get(self, owner_id: str, key: str, now: datetime) -> EditReceipt | None: ...
    def put(self, owner_id: str, key: str, receipt: EditReceipt, now: datetime) -> None: ...
    def delete_owner(self, owner_id: str) -> None: ...


class MemoryEditReceiptStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], tuple[EditReceipt, datetime]] = {}
        self._lock = threading.Lock()

    def get(self, owner_id: str, key: str, now: datetime) -> EditReceipt | None:
        with self._lock:
            row = self.items.get((owner_id, key_digest(key)))
            if row is None or now >= row[1]:
                return None
            return row[0]

    def put(self, owner_id: str, key: str, receipt: EditReceipt, now: datetime) -> None:
        with self._lock:
            self.items[(owner_id, key_digest(key))] = (receipt, now + RECEIPT_RETENTION)

    def delete_owner(self, owner_id: str) -> None:
        with self._lock:
            for k in [k for k in self.items if k[0] == owner_id]:
                del self.items[k]


class FirestoreEditReceiptStore(_Base):
    def _ref(self, owner_id: str, key: str) -> Any:
        return self._col(EDIT_RECEIPTS).document(doc_id(owner_id, key_digest(key)))

    def get(self, owner_id: str, key: str, now: datetime) -> EditReceipt | None:
        snap = self._ref(owner_id, key).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        if doc.get("owner_id") != owner_id or doc.get("kind") != "edit_receipt":
            raise StoredDataError("edit receipt does not belong to this tenant")
        expires = doc.get("expires_at")
        if expires is not None and now >= expires:
            return None
        return EditReceipt(str(doc["fingerprint"]), int(doc["version"]))

    def put(self, owner_id: str, key: str, receipt: EditReceipt, now: datetime) -> None:
        self._ref(owner_id, key).set({
            "schema_version": SCHEMA_VERSION, "kind": "edit_receipt", "owner_id": owner_id,
            "fingerprint": receipt.fingerprint, "version": receipt.version,
            "created_at": now, "expires_at": now + RECEIPT_RETENTION})

    def delete_owner(self, owner_id: str) -> None:
        for snap in self._col(EDIT_RECEIPTS).where("owner_id", "==", owner_id).stream():
            snap.reference.delete()


# --- factories -------------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanStores:
    kind: str
    plans: PlanVersionStore
    outcomes: OutcomeStore
    receipts: EditReceiptStore

    def __repr__(self) -> str:
        return f"PlanStores(kind={self.kind!r})"


def memory_plan_stores() -> PlanStores:
    return PlanStores("memory", MemoryPlanVersionStore(), MemoryOutcomeStore(),
                      MemoryEditReceiptStore())


def firestore_plan_stores(
    client: Any, *, transactional: Callable[..., Any] | None = None,
    max_attempts: int | None = None,
) -> PlanStores:
    common = {"transactional": transactional, "max_attempts": max_attempts or DEFAULT_MAX_ATTEMPTS}
    return PlanStores("firestore", FirestorePlanVersionStore(client, **common),
                      FirestoreOutcomeStore(client, **common),
                      FirestoreEditReceiptStore(client, **common))


__all__ = [
    "MAX_OUTCOMES", "PLAN_COLLECTIONS", "EditReceipt", "EditReceiptStore", "MemoryEditReceiptStore",
    "MemoryOutcomeStore", "MemoryPlanVersionStore", "OutcomeStore", "PlanConflict", "PlanStores",
    "PlanVersionStore", "StoredOutcome", "firestore_plan_stores", "key_digest",
    "memory_plan_stores",
]
