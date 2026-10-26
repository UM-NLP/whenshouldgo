"""Durable account state: link intents, registered-account records, the merge journal and
deletion tombstones (docs/access-tiers.md, decisions 26 and 27).

One protocol (``AccountStore``) with an in-memory implementation (development, tests; per process)
and a Firestore adapter written against the same small client surface the fake in
``fake_firestore.py`` implements. This file owns its own schema; no other store is touched.

Rules every implementation keeps:

* every document carries ``schema_version`` and ``kind``; the tenant is also stored in
  ``owner_id`` (what the owner sweep deletes by) and the document id is a SHA-256 digest;
* a link-intent NONCE is never stored: only its SHA-256 digest, compared in constant time;
* a TOMBSTONE holds no personal data: the SHA-256 digest of the uid and two timestamps;
* the merge JOURNAL holds ids, enums, timestamps and the user's own trip title (user text, cleared
  on completion): never a token, a coordinate or Maps content;
* each method is one atomic operation (a lock in memory, a transaction in Firestore); callers never
  read-modify-write across two calls. Functions passed to ``update_migration`` may run more than
  once (a retried transaction) and must be pure.

Time is always passed in; the stores never read a wall clock.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ..domain import SCHEMA_VERSION, LinkProvider, MigrationState
from .firestore import StoredDataError, _Base, doc_id

ACCOUNTS = "accounts"
LINK_INTENTS = "account_link_intents"
LINK_KEYS = "account_link_keys"
MIGRATIONS = "account_migrations"
MIGRATION_KEYS = "account_migration_keys"
TOMBSTONES = "account_tombstones"
# Collections whose documents carry ``owner_id`` and are removed by the account cascade. The
# tombstone collection is deliberately NOT here: it must outlive the deletion.
OWNED_COLLECTIONS = (ACCOUNTS, LINK_INTENTS, LINK_KEYS, MIGRATION_KEYS)


def uid_hash(uid: str) -> str:
    """SHA-256 hex digest of a uid: the only form in which a tombstone or an intent keeps one."""
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()


def nonce_digest(nonce: str) -> str:
    return hashlib.sha256(("link-nonce\x1f" + nonce).encode("utf-8")).hexdigest()


def key_digest(key: str) -> str:
    return hashlib.sha256(("idempotency-key\x1f" + key).encode("utf-8")).hexdigest()


# --- records -----------------------------------------------------------------------------------
class IntentState(StrEnum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    COLLISION = "COLLISION"  # the credential already belongs to another account


@dataclass(frozen=True)
class LinkIntentRecord:
    id: str
    owner_uid: str  # the verified guest uid the intent is bound to
    provider: LinkProvider
    nonce_hash: str
    created_at: datetime
    expires_at: datetime
    state: IntentState = IntentState.PENDING
    key_hash: str | None = None
    collision_uid_hash: str | None = None  # the other account that presented the intent
    consumed_at: datetime | None = None


@dataclass(frozen=True)
class AccountRecord:
    """The durable fact that this uid is a registered-free account (the tier change)."""

    uid: str
    provider: LinkProvider
    registered_at: datetime


class LinkCompletion(StrEnum):
    LINKED = "LINKED"
    NOT_FOUND = "NOT_FOUND"  # unknown id, wrong nonce or another tenant's intent
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"
    COLLISION = "COLLISION"  # the intent was parked for the explicit merge flow


class ItemState(StrEnum):
    PENDING = "PENDING"
    COPIED = "COPIED"


@dataclass(frozen=True)
class MigrationItem:
    id: str
    source: str  # the registered ``MigrationSource`` that lists and copies it
    kind: str
    title: str  # the user's own text, cleared when the migration completes
    state: ItemState = ItemState.PENDING
    selected: bool = False
    target: str | None = None  # where the copy lands, decided once when the run starts


@dataclass(frozen=True)
class MigrationRecord:
    id: str
    src_uid: str
    dst_uid: str
    intent_id: str
    key_hash: str
    state: MigrationState
    items: tuple[MigrationItem, ...]
    created_at: datetime
    updated_at: datetime
    preference: str | None = None  # KEEP_REGISTERED or USE_GUEST, set when the run starts
    completed_at: datetime | None = None


@dataclass(frozen=True)
class Tombstone:
    """No personal data: the hashed uid and when deletion was requested and finished."""

    uid_hash: str
    requested_at: datetime
    completed_at: datetime | None = None


@runtime_checkable
class AccountStore(Protocol):
    # -- link intents
    def issue_intent(
        self, candidate: LinkIntentRecord, now: datetime) -> tuple[LinkIntentRecord, bool]:
        """Store ``candidate``. With a ``key_hash`` already issued to this owner for a live
        PENDING intent, return that intent (same id) with its nonce replaced by the candidate's
        and ``True``; otherwise store the candidate and return it with ``False``."""
        ...

    def get_intent(self, intent_id: str) -> LinkIntentRecord | None: ...

    def park_collision(
        self, intent_id: str, nonce_hash: str, other_uid: str, now: datetime
    ) -> LinkIntentRecord | None:
        """Mark a live, unconsumed intent as a collision presented by ``other_uid``. None when the
        id is unknown, the nonce differs, or the intent expired or was consumed."""
        ...

    def complete_link(
        self, intent_id: str, nonce_hash: str, account: AccountRecord, now: datetime
    ) -> tuple[LinkCompletion, AccountRecord | None]:
        """ONE atomic step: verify the nonce and owner (``account.uid``), consume the intent and
        upsert the account record. A repeat of the registration keeps the first record."""
        ...

    # -- registered accounts
    def get_account(self, uid: str) -> AccountRecord | None: ...
    def ensure_account(self, account: AccountRecord) -> tuple[AccountRecord, bool]: ...

    # -- merge journal
    def create_migration(
        self, candidate: MigrationRecord) -> tuple[MigrationRecord, bool]:
        """Idempotent by (dst_uid, key_hash); at most one unfinished migration per source uid
        (a second distinct request returns the unfinished one with ``False``)."""
        ...

    def get_migration(self, migration_id: str) -> MigrationRecord | None: ...
    def migrations_for_dst(self, dst_uid: str) -> tuple[MigrationRecord, ...]:
        """Journals whose destination is ``dst_uid`` (export lists the workspaces they copied)."""
        ...

    def update_migration(
        self, migration_id: str, fn: Callable[[MigrationRecord], MigrationRecord]
    ) -> MigrationRecord | None: ...

    # -- tombstones
    def begin_tombstone(self, uid: str, now: datetime) -> Tombstone:
        """Idempotent: the first ``requested_at`` stays."""
        ...

    def complete_tombstone(self, uid: str, now: datetime) -> Tombstone: ...
    def get_tombstone(self, uid: str) -> Tombstone | None: ...

    # -- deletion (the account cascade)
    def delete_accounts(self, uid: str) -> int:
        """Account record, link intents and idempotency keys owned by ``uid``."""
        ...

    def delete_migrations(self, uid: str) -> int:
        """Merge journals where ``uid`` is the destination or the source."""
        ...

    def all_documents(self) -> list[Any]:
        """Every record held (tests scan it for personal data)."""
        ...


# --- in-memory ---------------------------------------------------------------------------------
class MemoryAccountStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.intents: dict[str, LinkIntentRecord] = {}
        self.intent_keys: dict[tuple[str, str], str] = {}
        self.accounts: dict[str, AccountRecord] = {}
        self.migrations: dict[str, MigrationRecord] = {}
        self.migration_keys: dict[tuple[str, str], str] = {}
        self.tombstones: dict[str, Tombstone] = {}  # keyed by uid HASH

    # intents
    def issue_intent(
        self, candidate: LinkIntentRecord, now: datetime) -> tuple[LinkIntentRecord, bool]:
        with self._lock:
            if candidate.key_hash is not None:
                existing_id = self.intent_keys.get((candidate.owner_uid, candidate.key_hash))
                existing = self.intents.get(existing_id) if existing_id else None
                if (existing is not None and existing.state == IntentState.PENDING
                        and now < existing.expires_at):
                    rotated = replace(existing, nonce_hash=candidate.nonce_hash)
                    self.intents[existing.id] = rotated
                    return rotated, True
                self.intent_keys[(candidate.owner_uid, candidate.key_hash)] = candidate.id
            self.intents[candidate.id] = candidate
            return candidate, False

    def get_intent(self, intent_id: str) -> LinkIntentRecord | None:
        with self._lock:
            return self.intents.get(intent_id)

    def park_collision(
        self, intent_id: str, nonce_hash: str, other_uid: str, now: datetime
    ) -> LinkIntentRecord | None:
        with self._lock:
            record = self.intents.get(intent_id)
            if (record is None or not hmac.compare_digest(record.nonce_hash, nonce_hash)
                    or now >= record.expires_at or record.state == IntentState.CONSUMED):
                return None
            parked = replace(record, state=IntentState.COLLISION,
                             collision_uid_hash=uid_hash(other_uid))
            self.intents[intent_id] = parked
            return parked

    def complete_link(
        self, intent_id: str, nonce_hash: str, account: AccountRecord, now: datetime
    ) -> tuple[LinkCompletion, AccountRecord | None]:
        with self._lock:
            record = self.intents.get(intent_id)
            result = _judge_completion(record, nonce_hash, account.uid, now)
            if result != LinkCompletion.LINKED:
                return result, None
            assert record is not None
            self.intents[intent_id] = replace(record, state=IntentState.CONSUMED, consumed_at=now)
            return LinkCompletion.LINKED, self.accounts.setdefault(account.uid, account)

    # accounts
    def get_account(self, uid: str) -> AccountRecord | None:
        with self._lock:
            return self.accounts.get(uid)

    def ensure_account(self, account: AccountRecord) -> tuple[AccountRecord, bool]:
        with self._lock:
            existing = self.accounts.get(account.uid)
            if existing is not None:
                return existing, False
            self.accounts[account.uid] = account
            return account, True

    # migrations
    def create_migration(self, candidate: MigrationRecord) -> tuple[MigrationRecord, bool]:
        with self._lock:
            existing_id = self.migration_keys.get((candidate.dst_uid, candidate.key_hash))
            if existing_id is not None and existing_id in self.migrations:
                return self.migrations[existing_id], False
            for record in self.migrations.values():
                if record.src_uid == candidate.src_uid and record.state != MigrationState.COMPLETED:
                    return record, False
            self.migrations[candidate.id] = candidate
            self.migration_keys[(candidate.dst_uid, candidate.key_hash)] = candidate.id
            return candidate, True

    def get_migration(self, migration_id: str) -> MigrationRecord | None:
        with self._lock:
            return self.migrations.get(migration_id)

    def migrations_for_dst(self, dst_uid: str) -> tuple[MigrationRecord, ...]:
        with self._lock:
            return tuple(r for r in self.migrations.values() if r.dst_uid == dst_uid)

    def update_migration(
        self, migration_id: str, fn: Callable[[MigrationRecord], MigrationRecord]
    ) -> MigrationRecord | None:
        with self._lock:
            record = self.migrations.get(migration_id)
            if record is None:
                return None
            updated = fn(record)
            self.migrations[migration_id] = updated
            return updated

    # tombstones
    def begin_tombstone(self, uid: str, now: datetime) -> Tombstone:
        digest = uid_hash(uid)
        with self._lock:
            return self.tombstones.setdefault(digest, Tombstone(digest, now))

    def complete_tombstone(self, uid: str, now: datetime) -> Tombstone:
        digest = uid_hash(uid)
        with self._lock:
            current = self.tombstones.get(digest) or Tombstone(digest, now)
            done = current if current.completed_at else replace(current, completed_at=now)
            self.tombstones[digest] = done
            return done

    def get_tombstone(self, uid: str) -> Tombstone | None:
        with self._lock:
            return self.tombstones.get(uid_hash(uid))

    # deletion
    def delete_accounts(self, uid: str) -> int:
        with self._lock:
            removed = 0
            for key in [k for k, r in self.intents.items() if r.owner_uid == uid]:
                del self.intents[key]
                removed += 1
            for key in [k for k in self.intent_keys if k[0] == uid]:
                del self.intent_keys[key]
            if self.accounts.pop(uid, None) is not None:
                removed += 1
            for key in [k for k in self.migration_keys if k[0] == uid]:
                del self.migration_keys[key]
            return removed

    def delete_migrations(self, uid: str) -> int:
        with self._lock:
            doomed = [k for k, r in self.migrations.items() if uid in (r.src_uid, r.dst_uid)]
            for key in doomed:
                del self.migrations[key]
            for key in [k for k, v in self.migration_keys.items() if v in doomed]:
                del self.migration_keys[key]
            return len(doomed)

    def all_documents(self) -> list[Any]:
        with self._lock:
            return [*self.intents.values(), *self.accounts.values(), *self.migrations.values(),
                    *self.tombstones.values(), *self.intent_keys.items(),
                    *self.migration_keys.items()]


def _judge_completion(
    record: LinkIntentRecord | None, nonce_hash: str, uid: str, now: datetime
) -> LinkCompletion:
    """The one rule set both implementations apply inside their atomic step."""
    if (record is None or record.owner_uid != uid
            or not hmac.compare_digest(record.nonce_hash, nonce_hash)):
        return LinkCompletion.NOT_FOUND  # one answer for unknown, wrong nonce and another tenant
    if record.state == IntentState.CONSUMED:
        return LinkCompletion.CONSUMED
    if now >= record.expires_at:
        return LinkCompletion.EXPIRED
    if record.state == IntentState.COLLISION:
        return LinkCompletion.COLLISION
    return LinkCompletion.LINKED


# --- Firestore ---------------------------------------------------------------------------------
def _intent_to_doc(r: LinkIntentRecord) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "kind": "account_link_intent", "owner_id": r.owner_uid,
        "id": r.id, "provider": r.provider.value, "nonce_hash": r.nonce_hash,
        "created_at": r.created_at, "expires_at": r.expires_at, "state": r.state.value,
        "key_hash": r.key_hash, "collision_uid_hash": r.collision_uid_hash,
        "consumed_at": r.consumed_at}


def _intent_from_doc(doc: dict[str, Any]) -> LinkIntentRecord:
    if doc.get("kind") != "account_link_intent":
        raise StoredDataError("link intent document has the wrong kind")
    return LinkIntentRecord(
        id=str(doc["id"]), owner_uid=str(doc["owner_id"]), provider=LinkProvider(doc["provider"]),
        nonce_hash=str(doc["nonce_hash"]), created_at=doc["created_at"],
        expires_at=doc["expires_at"], state=IntentState(doc["state"]), key_hash=doc.get("key_hash"),
        collision_uid_hash=doc.get("collision_uid_hash"), consumed_at=doc.get("consumed_at"))


def _account_to_doc(a: AccountRecord) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "kind": "account_record", "owner_id": a.uid,
            "provider": a.provider.value, "registered_at": a.registered_at}


def _account_from_doc(uid: str, doc: dict[str, Any]) -> AccountRecord:
    if doc.get("owner_id") != uid or doc.get("kind") != "account_record":
        raise StoredDataError("account document does not belong to this tenant")
    return AccountRecord(uid, LinkProvider(doc["provider"]), doc["registered_at"])


def _migration_to_doc(m: MigrationRecord) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "kind": "account_migration", "owner_id": m.dst_uid,
        "id": m.id, "src_uid": m.src_uid, "intent_id": m.intent_id, "key_hash": m.key_hash,
        "state": m.state.value, "preference": m.preference, "created_at": m.created_at,
        "updated_at": m.updated_at, "completed_at": m.completed_at,
        "items": [{"id": i.id, "source": i.source, "item_kind": i.kind, "title": i.title,
                   "state": i.state.value, "selected": i.selected, "target": i.target}
                  for i in m.items]}


def _migration_from_doc(doc: dict[str, Any]) -> MigrationRecord:
    if doc.get("kind") != "account_migration":
        raise StoredDataError("migration document has the wrong kind")
    items = tuple(
        MigrationItem(id=i["id"], source=i["source"], kind=i["item_kind"], title=i["title"],
                      state=ItemState(i["state"]), selected=bool(i["selected"]),
                      target=i.get("target"))
        for i in doc.get("items", []))
    return MigrationRecord(
        id=str(doc["id"]), src_uid=str(doc["src_uid"]), dst_uid=str(doc["owner_id"]),
        intent_id=str(doc["intent_id"]), key_hash=str(doc["key_hash"]),
        state=MigrationState(doc["state"]), items=items, created_at=doc["created_at"],
        updated_at=doc["updated_at"], preference=doc.get("preference"),
        completed_at=doc.get("completed_at"))


def _tombstone_from_doc(doc: dict[str, Any]) -> Tombstone:
    return Tombstone(str(doc["uid_hash"]), doc["requested_at"], doc.get("completed_at"))


class FirestoreAccountStore(_Base):
    """Firestore adapter. Needs only the automatic single-field indexes (``owner_id`` and
    ``src_uid`` equality)."""

    def _ref(self, collection: str, *parts: str) -> Any:
        return self._col(collection).document(doc_id(collection, *parts))

    # intents
    def issue_intent(
        self, candidate: LinkIntentRecord, now: datetime) -> tuple[LinkIntentRecord, bool]:
        intent_ref = self._ref(LINK_INTENTS, candidate.id)
        key_ref = (self._ref(LINK_KEYS, candidate.owner_uid, candidate.key_hash)
                   if candidate.key_hash else None)

        def body(txn: Any) -> tuple[LinkIntentRecord, bool]:
            existing: LinkIntentRecord | None = None
            if key_ref is not None:
                key_snap = key_ref.get(transaction=txn)
                if key_snap.exists:
                    old_ref = self._ref(LINK_INTENTS, str(key_snap.to_dict()["intent_id"]))
                    old_snap = old_ref.get(transaction=txn)
                    if old_snap.exists:
                        existing = _intent_from_doc(old_snap.to_dict())
            if (existing is not None and existing.owner_uid == candidate.owner_uid
                    and existing.state == IntentState.PENDING and now < existing.expires_at):
                rotated = replace(existing, nonce_hash=candidate.nonce_hash)
                txn.set(self._ref(LINK_INTENTS, existing.id), _intent_to_doc(rotated))
                return rotated, True
            txn.set(intent_ref, _intent_to_doc(candidate))
            if key_ref is not None:
                txn.set(key_ref, {"schema_version": SCHEMA_VERSION, "kind": "account_link_key",
                                  "owner_id": candidate.owner_uid, "intent_id": candidate.id,
                                  "created_at": now})
            return candidate, False

        return self._transact(body)

    def get_intent(self, intent_id: str) -> LinkIntentRecord | None:
        snap = self._ref(LINK_INTENTS, intent_id).get()
        return _intent_from_doc(snap.to_dict()) if snap.exists else None

    def park_collision(
        self, intent_id: str, nonce_hash: str, other_uid: str, now: datetime
    ) -> LinkIntentRecord | None:
        ref = self._ref(LINK_INTENTS, intent_id)

        def body(txn: Any) -> LinkIntentRecord | None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return None
            record = _intent_from_doc(snap.to_dict())
            if (not hmac.compare_digest(record.nonce_hash, nonce_hash)
                    or now >= record.expires_at or record.state == IntentState.CONSUMED):
                return None
            parked = replace(record, state=IntentState.COLLISION,
                             collision_uid_hash=uid_hash(other_uid))
            txn.set(ref, _intent_to_doc(parked))
            return parked

        return self._transact(body)

    def complete_link(
        self, intent_id: str, nonce_hash: str, account: AccountRecord, now: datetime
    ) -> tuple[LinkCompletion, AccountRecord | None]:
        intent_ref = self._ref(LINK_INTENTS, intent_id)
        account_ref = self._ref(ACCOUNTS, account.uid)

        def body(txn: Any) -> tuple[LinkCompletion, AccountRecord | None]:
            snap = intent_ref.get(transaction=txn)
            account_snap = account_ref.get(transaction=txn)
            record = _intent_from_doc(snap.to_dict()) if snap.exists else None
            result = _judge_completion(record, nonce_hash, account.uid, now)
            if result != LinkCompletion.LINKED:
                return result, None
            assert record is not None
            txn.set(intent_ref, _intent_to_doc(
                replace(record, state=IntentState.CONSUMED, consumed_at=now)))
            if account_snap.exists:
                return result, _account_from_doc(account.uid, account_snap.to_dict())
            txn.set(account_ref, _account_to_doc(account))
            return result, account

        return self._transact(body)

    # accounts
    def get_account(self, uid: str) -> AccountRecord | None:
        snap = self._ref(ACCOUNTS, uid).get()
        return _account_from_doc(uid, snap.to_dict()) if snap.exists else None

    def ensure_account(self, account: AccountRecord) -> tuple[AccountRecord, bool]:
        ref = self._ref(ACCOUNTS, account.uid)

        def body(txn: Any) -> tuple[AccountRecord, bool]:
            snap = ref.get(transaction=txn)
            if snap.exists:
                return _account_from_doc(account.uid, snap.to_dict()), False
            txn.set(ref, _account_to_doc(account))
            return account, True

        return self._transact(body)

    # migrations
    def create_migration(self, candidate: MigrationRecord) -> tuple[MigrationRecord, bool]:
        ref = self._ref(MIGRATIONS, candidate.id)
        key_ref = self._ref(MIGRATION_KEYS, candidate.dst_uid, candidate.key_hash)
        unfinished = self._col(MIGRATIONS).where("src_uid", "==", candidate.src_uid)

        def body(txn: Any) -> tuple[MigrationRecord, bool]:
            key_snap = key_ref.get(transaction=txn)
            rows = [_migration_from_doc(s.to_dict()) for s in unfinished.stream(transaction=txn)]
            if key_snap.exists:
                for row in rows:
                    if row.id == key_snap.to_dict()["migration_id"]:
                        return row, False
            for row in rows:
                if row.state != MigrationState.COMPLETED:
                    return row, False
            txn.set(ref, _migration_to_doc(candidate))
            txn.set(key_ref, {"schema_version": SCHEMA_VERSION, "kind": "account_migration_key",
                              "owner_id": candidate.dst_uid, "migration_id": candidate.id})
            return candidate, True

        return self._transact(body)

    def get_migration(self, migration_id: str) -> MigrationRecord | None:
        snap = self._ref(MIGRATIONS, migration_id).get()
        return _migration_from_doc(snap.to_dict()) if snap.exists else None

    def migrations_for_dst(self, dst_uid: str) -> tuple[MigrationRecord, ...]:
        rows = self._col(MIGRATIONS).where("owner_id", "==", dst_uid).stream()
        return tuple(_migration_from_doc(s.to_dict()) for s in rows)

    def update_migration(
        self, migration_id: str, fn: Callable[[MigrationRecord], MigrationRecord]
    ) -> MigrationRecord | None:
        ref = self._ref(MIGRATIONS, migration_id)

        def body(txn: Any) -> MigrationRecord | None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return None
            updated = fn(_migration_from_doc(snap.to_dict()))
            txn.set(ref, _migration_to_doc(updated))
            return updated

        return self._transact(body)

    # tombstones
    def _tombstone_doc(self, t: Tombstone) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "kind": "account_tombstone",
                "uid_hash": t.uid_hash, "requested_at": t.requested_at,
                "completed_at": t.completed_at}

    def begin_tombstone(self, uid: str, now: datetime) -> Tombstone:
        ref = self._ref(TOMBSTONES, uid)

        def body(txn: Any) -> Tombstone:
            snap = ref.get(transaction=txn)
            if snap.exists:
                return _tombstone_from_doc(snap.to_dict())
            tomb = Tombstone(uid_hash(uid), now)
            txn.set(ref, self._tombstone_doc(tomb))
            return tomb

        return self._transact(body)

    def complete_tombstone(self, uid: str, now: datetime) -> Tombstone:
        ref = self._ref(TOMBSTONES, uid)

        def body(txn: Any) -> Tombstone:
            snap = ref.get(transaction=txn)
            current = _tombstone_from_doc(snap.to_dict()) if snap.exists else Tombstone(
                uid_hash(uid), now)
            done = current if current.completed_at else replace(current, completed_at=now)
            txn.set(ref, self._tombstone_doc(done))
            return done

        return self._transact(body)

    def get_tombstone(self, uid: str) -> Tombstone | None:
        snap = self._ref(TOMBSTONES, uid).get()
        return _tombstone_from_doc(snap.to_dict()) if snap.exists else None

    # deletion
    def _sweep(self, collection: str, field: str, value: str) -> int:
        removed = 0
        while True:
            rows = list(self._col(collection).where(field, "==", value).limit(200).stream())
            if not rows:
                return removed
            for snap in rows:
                snap.reference.delete()
                removed += 1

    def delete_accounts(self, uid: str) -> int:
        return sum(self._sweep(c, "owner_id", uid) for c in OWNED_COLLECTIONS)

    def delete_migrations(self, uid: str) -> int:
        return self._sweep(MIGRATIONS, "owner_id", uid) + self._sweep(MIGRATIONS, "src_uid", uid)

    def all_documents(self) -> list[Any]:
        client = self._client
        documents = getattr(client, "documents", None)
        if documents is None:
            return []
        return [data for _path, data in documents() if _path.split("/")[0] in {
            ACCOUNTS, LINK_INTENTS, LINK_KEYS, MIGRATIONS, MIGRATION_KEYS, TOMBSTONES}]


__all__ = [
    "ACCOUNTS", "LINK_INTENTS", "LINK_KEYS", "MIGRATIONS", "MIGRATION_KEYS", "TOMBSTONES",
    "AccountRecord", "AccountStore", "FirestoreAccountStore", "IntentState", "ItemState",
    "LinkCompletion", "LinkIntentRecord", "MemoryAccountStore", "MigrationItem", "MigrationRecord",
    "MigrationState", "Tombstone", "key_digest", "nonce_digest", "uid_hash",
]
