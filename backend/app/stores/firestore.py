"""Firestore adapters for the store protocols (decision 26: CODE ONLY, never run against the real
service here, no cloud resources created).

``google-cloud-firestore`` is an OPTIONAL dependency (extra ``gcp`` in pyproject.toml) and is
imported lazily, only by ``make_client`` and by ``default_transactional``. Everything else is
written against the small client surface that ``fake_firestore.py`` also implements:
``client.collection(name).document(id)``, ``ref.get(transaction=)``, ``ref.set/delete``,
``collection.where(...).stream(transaction=)``, ``client.transaction(max_attempts=)``, and the
``transactional`` decorator (injected, so tests pass the fake's).

Document rules, all enforced here:

* the document id is a SHA-256 digest of the tenant (and key), never the raw principal id, and
  never a reserved id; the tenant is also stored in ``owner_id`` and verified on every read;
* every document has ``schema_version`` and ``kind``;
* values are plain JSON-like types and timezone-aware datetimes (what the real client accepts);
* TTL: documents that expire carry ``expires_at`` (the field a Firestore TTL policy targets, which
  is a RUNBOOK TODO). Deletion by the policy is late (up to a day or more), so every read ALSO
  checks expiry itself;
* nothing stored here holds a coordinate, a raw provider response or route geometry. The only
  Maps content is the display cache's grounded answer (redisplay, 30 days at most, decision 18).

Transactions do all reads before any write, as the real service requires.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, TypeVar

from pydantic import ValidationError

from ..discovery._validated import ValidatedAnswer, is_validated
from ..discovery.cache import DisplayCache
from ..discovery.contracts import GroundedAnswer
from ..discovery.errors import CacheError
from ..discovery.validator import restore_validated
from ..domain import MAX_MAPS_CACHE, SCHEMA_VERSION, ConsentRecord, TripWorkspace
from . import quota_logic as logic
from .busy import StoreBusy, is_busy_failure
from .protocols import (
    PENDING_REPLAY_TTL,
    REPLAY_RETENTION,
    CostEntryDelta,
    KILL_PATH_GLOBAL,
    KillSwitchReason,
    KillSwitchState,
    QuotaLimits,
    ReserveOutcome,
    SpendBudget,
    SpendTotals,
    StoredReplay,
    Usage,
    known_kill_path,
    over_budget,
)

T = TypeVar("T")
_log = logging.getLogger(__name__)

QUOTA = "quota_usage"
WORKSPACES = "workspaces"
IDEMPOTENCY = "idempotency"
DISPLAY_CACHE = "display_cache"
CONSENT_LEDGERS = "consent_ledgers"
COST_TOTALS = "cost_totals"
KILL_SWITCH = "kill_switch"
COLLECTIONS = (QUOTA, WORKSPACES, IDEMPOTENCY, DISPLAY_CACHE, CONSENT_LEDGERS, COST_TOTALS,
               KILL_SWITCH)

DEFAULT_MAX_ATTEMPTS = 30


class StoredDataError(Exception):
    """A stored document does not match the tenant or the schema it should have. Static text:
    never document content."""

class _Filter:
    """Stand-in with the attributes the real ``FieldFilter`` has, for when the optional ``gcp``
    extra is not installed (only the in-process fake ever sees it)."""

    def __init__(self, field_path: str, op_string: str, value: Any) -> None:
        self.field_path, self.op_string, self.value = field_path, op_string, value


def where(query: Any, field: str, op: str, value: Any) -> Any:
    """``query.where(filter=FieldFilter(...))``: the keyword form, since the positional
    ``where(field, op, value)`` warns (UserWarning) in current google-cloud-firestore."""
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore[import-not-found]
    except ImportError:
        FieldFilter = _Filter  # noqa: N806
    return query.where(filter=FieldFilter(field, op, value))


def make_client(project_id: str) -> Any:
    """The real Firestore client (lazy import). Raises ImportError when the ``gcp`` extra is
    missing. Credentials come from the runtime's ambient service account, never from here."""
    from google.cloud import firestore  # type: ignore[import-not-found]

    return firestore.Client(project=project_id)


def default_transactional() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    from google.cloud import firestore  # type: ignore[import-not-found]

    return firestore.transactional


def doc_id(*parts: str) -> str:
    """Digest id for a tenant (+ key): fixed length, no slash, never reserved, no raw id."""
    for part in parts:
        if not isinstance(part, str) or not part:
            raise ValueError("document key parts must be non-empty strings")
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:48]


class _Base:
    def __init__(
        self, client: Any, *, transactional: Callable[..., Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._client = client
        self._transactional = transactional
        self._max_attempts = max_attempts

    def _col(self, name: str) -> Any:
        return self._client.collection(name)

    def _transact(self, fn: Callable[[Any], T]) -> T:
        transactional = self._transactional
        if transactional is None:
            # The fake client carries its own decorator; the real client library has none, so the
            # real one is imported (lazily) from google.cloud.firestore.
            transactional = getattr(self._client, "transactional", None) or default_transactional()
            self._transactional = transactional
        body = transactional(fn)
        try:
            return body(self._client.transaction(max_attempts=self._max_attempts))
        except Exception as exc:  # noqa: BLE001 - classified, everything else is re-raised as is
            if not is_busy_failure(exc):
                raise
        # Raised outside the except block: no chained exception (its text could hold a document
        # path) and a static message.
        raise StoreBusy()


# --- quota ---------------------------------------------------------------------------------------
def _usage_to_doc(uid: str, usage: Usage, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "kind": "quota_usage", "owner_id": uid,
        "successful_turns": usage.successful_turns, "activated_trips": usage.activated_trips,
        "committed_keys": sorted(usage.committed_keys),
        # A list of maps, not a map keyed by the idempotency key: field names may not look like
        # ``__x__`` and a client-chosen key must not become a field name.
        "reserved": [{"key": k, "at": usage.reserved_at[k]} for k in sorted(usage.reserved_at)]
        + [{"key": k, "at": None} for k in sorted(usage.reserved_keys - set(usage.reserved_at))],
        "activated_workspaces": sorted(usage.activated_workspaces),
        "window_started_at": usage.window_started_at,
        "updated_at": now,
    }


def _usage_from_doc(uid: str, doc: dict[str, Any]) -> Usage:
    if doc.get("owner_id") != uid or doc.get("kind") != "quota_usage":
        raise StoredDataError("quota document does not belong to this tenant")
    reserved = doc.get("reserved", [])
    return Usage(
        successful_turns=int(doc["successful_turns"]), activated_trips=int(doc["activated_trips"]),
        committed_keys=set(doc.get("committed_keys", [])),
        reserved_keys={r["key"] for r in reserved},
        reserved_at={r["key"]: r["at"] for r in reserved if r.get("at") is not None},
        activated_workspaces=set(doc.get("activated_workspaces", [])),
        window_started_at=doc.get("window_started_at"))


class FirestoreQuotaStore(_Base):
    def _ref(self, uid: str) -> Any:
        return self._col(QUOTA).document(doc_id(uid))

    def _mutate(self, uid: str, now: datetime, window: timedelta | None,
                fn: Callable[[Usage], T]) -> T:
        ref = self._ref(uid)

        def body(txn: Any) -> T:
            snap = ref.get(transaction=txn)
            usage = _usage_from_doc(uid, snap.to_dict()) if snap.exists else Usage()
            logic.prepare(usage, now, window)
            result = fn(usage)  # may raise: nothing is written then
            txn.set(ref, _usage_to_doc(uid, usage, now))
            return result

        return self._transact(body)

    def _read(self, uid: str, now: datetime, window: timedelta | None) -> Usage:
        snap = self._ref(uid).get()
        usage = _usage_from_doc(uid, snap.to_dict()) if snap.exists else Usage()
        return logic.prepare(usage, now, window)

    def ensure(self, uid: str, now: datetime, window: timedelta | None) -> None:
        self._mutate(uid, now, window, lambda _u: None)

    def snapshot(self, uid: str, now: datetime, window: timedelta | None) -> Usage:
        return self._read(uid, now, window)

    def has_room(self, uid: str, limits: QuotaLimits, now: datetime) -> bool:
        return logic.has_room(self._read(uid, now, limits.window), limits)

    def reserve(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> ReserveOutcome:
        return self._mutate(uid, now, limits.window, lambda u: logic.reserve(u, key, limits, now))

    def commit(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> Usage:
        def fn(usage: Usage) -> Usage:
            logic.commit(usage, key)
            return logic.copy_usage(usage)

        return self._mutate(uid, now, limits.window, fn)

    def release(self, uid: str, key: str) -> None:
        ref = self._ref(uid)

        def body(txn: Any) -> None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return
            usage = _usage_from_doc(uid, snap.to_dict())
            if key in usage.reserved_keys:
                logic.release(usage, key)
                txn.set(ref, _usage_to_doc(uid, usage, snap.to_dict().get("updated_at")))

        self._transact(body)

    def commit_turn(self, uid: str, key: str, limits: QuotaLimits, now: datetime) -> Usage:
        def fn(usage: Usage) -> Usage:
            logic.commit_turn(usage, key, limits)
            return logic.copy_usage(usage)

        return self._mutate(uid, now, limits.window, fn)

    def activate_trip(
        self, uid: str, workspace_id: str, limits: QuotaLimits, now: datetime
    ) -> bool:
        return self._mutate(uid, now, limits.window,
                            lambda u: logic.activate_trip(u, workspace_id, limits))


# --- consent ledger ------------------------------------------------------------------------------
class FirestoreConsentLedger(_Base):
    """One document per owner holding an ``entries`` list that only ever grows."""

    def _ref(self, owner_id: str) -> Any:
        return self._col(CONSENT_LEDGERS).document(doc_id(owner_id))

    @staticmethod
    def _decode(owner_id: str, doc: dict[str, Any]) -> tuple[ConsentRecord, ...]:
        if doc.get("owner_id") != owner_id or doc.get("kind") != "consent_ledger":
            raise StoredDataError("consent ledger does not belong to this tenant")
        try:
            records = tuple(ConsentRecord.model_validate(e) for e in doc.get("entries", []))
        except ValidationError:
            raise StoredDataError("consent ledger entry does not match the schema") from None
        if any(r.owner_id != owner_id for r in records):
            raise StoredDataError("consent entry belongs to another tenant")
        return records

    def read(self, owner_id: str) -> tuple[ConsentRecord, ...]:
        snap = self._ref(owner_id).get()
        return self._decode(owner_id, snap.to_dict()) if snap.exists else ()

    def update(
        self, owner_id: str,
        fn: Callable[[tuple[ConsentRecord, ...]], tuple[tuple[ConsentRecord, ...], T]],
    ) -> T:
        ref = self._ref(owner_id)

        def body(txn: Any) -> T:
            snap = ref.get(transaction=txn)
            doc = snap.to_dict() if snap.exists else None
            current = self._decode(owner_id, doc) if doc is not None else ()
            appended, result = fn(current)
            if appended:
                if any(r.owner_id != owner_id for r in appended):
                    raise StoredDataError("consent entry belongs to another tenant")
                entries = list(doc["entries"]) if doc is not None else []
                entries.extend(r.model_dump(mode="json") for r in appended)  # old ones untouched
                txn.set(ref, {"schema_version": SCHEMA_VERSION, "kind": "consent_ledger",
                              "owner_id": owner_id, "entries": entries,
                              "updated_at": appended[-1].withdrawn_at or appended[-1].granted_at})
            return result

        return self._transact(body)


# --- workspace -----------------------------------------------------------------------------------
class FirestoreWorkspaceStore(_Base):
    def _ref(self, owner_id: str, name: str) -> Any:
        return self._col(WORKSPACES).document(doc_id(owner_id, name))

    def get(self, owner_id: str, name: str, now: datetime) -> TripWorkspace | None:
        snap = self._ref(owner_id, name).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        if doc.get("owner_id") != owner_id or doc.get("name") != name:
            raise StoredDataError("workspace document does not belong to this tenant")
        expires = doc.get("expires_at")
        if expires is not None and now >= expires:
            return None
        try:
            workspace = TripWorkspace.model_validate(doc["workspace"])
        except (ValidationError, KeyError):
            raise StoredDataError("workspace document does not match the schema") from None
        if workspace.owner_id != owner_id:
            raise StoredDataError("workspace belongs to another tenant")
        return workspace

    @staticmethod
    def _record(owner_id: str, name: str, workspace: TripWorkspace, now: datetime,
                ttl: timedelta | None) -> dict[str, Any]:
        if workspace.owner_id != owner_id:
            raise ValueError("workspace owner does not match the tenant key")
        doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "kind": "workspace_record", "owner_id": owner_id,
            "name": name, "workspace": workspace.model_dump(mode="json"), "updated_at": now}
        if ttl is not None:
            doc["expires_at"] = now + ttl  # refreshed by every write: inactive days
        return doc

    def put(self, owner_id: str, name: str, workspace: TripWorkspace, now: datetime,
            ttl: timedelta | None) -> None:
        doc = self._record(owner_id, name, workspace, now, ttl)
        self._ref(owner_id, name).set(doc)  # a full replace also clears an old expires_at

    def put_if_version(self, owner_id: str, name: str, workspace: TripWorkspace, now: datetime,
                       ttl: timedelta | None, *, expected_version: int | None) -> bool:
        doc = self._record(owner_id, name, workspace, now, ttl)
        ref = self._ref(owner_id, name)

        def body(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            current: int | None = None
            if snap.exists:
                stored = snap.to_dict()
                if stored.get("owner_id") != owner_id or stored.get("name") != name:
                    raise StoredDataError("workspace document does not belong to this tenant")
                expires = stored.get("expires_at")
                if expires is None or now < expires:  # an expired record reads as absent
                    try:
                        current = TripWorkspace.model_validate(stored["workspace"]).version
                    except (ValidationError, KeyError):
                        raise StoredDataError("workspace document does not match the schema") from None
            if current != expected_version:
                return False
            txn.set(ref, doc)
            return True

        return self._transact(body)

    def delete(self, owner_id: str, name: str) -> None:
        self._ref(owner_id, name).delete()

    def delete_if_version(self, owner_id: str, name: str, *, expected_version: int) -> bool:
        ref = self._ref(owner_id, name)

        def body(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return False
            stored = snap.to_dict()
            if stored.get("owner_id") != owner_id or stored.get("name") != name:
                raise StoredDataError("workspace document does not belong to this tenant")
            try:
                current = TripWorkspace.model_validate(stored["workspace"]).version
            except (ValidationError, KeyError):
                raise StoredDataError("workspace document does not match the schema") from None
            if current != expected_version:
                return False
            txn.delete(ref)
            return True

        return self._transact(body)


# --- idempotency ---------------------------------------------------------------------------------
class FirestoreIdempotencyStore(_Base):
    def __init__(
        self, client: Any, codec: Any, *, transactional: Callable[..., Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        super().__init__(client, transactional=transactional, max_attempts=max_attempts)
        self._codec = codec

    def _ref(self, owner_id: str, key: str) -> Any:
        return self._col(IDEMPOTENCY).document(doc_id(owner_id, key))

    def get(self, owner_id: str, key: str, now: datetime) -> StoredReplay | None:
        snap = self._ref(owner_id, key).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        if doc.get("owner_id") != owner_id:
            raise StoredDataError("idempotency document does not belong to this tenant")
        expires = doc.get("expires_at")
        if expires is not None and now >= expires:
            return None
        try:
            record = self._codec.decode(doc["record"])
        except Exception:  # noqa: BLE001 - an undecodable record is as good as gone (typed 409)
            return None
        return StoredReplay(str(doc["fingerprint"]), record, pending=bool(doc["pending"]))

    def begin(self, owner_id: str, key: str, fingerprint: str, record: Any, now: datetime) -> None:
        self._ref(owner_id, key).set({
            "schema_version": SCHEMA_VERSION, "kind": "idempotency_record", "owner_id": owner_id,
            "fingerprint": fingerprint, "record": self._codec.encode(record), "pending": True,
            "created_at": now, "expires_at": now + PENDING_REPLAY_TTL})

    def commit(self, owner_id: str, key: str, now: datetime) -> None:
        ref = self._ref(owner_id, key)

        def body(txn: Any) -> None:
            snap = ref.get(transaction=txn)
            if snap.exists:
                doc = snap.to_dict()
                doc["pending"] = False
                doc["expires_at"] = now + REPLAY_RETENTION
                txn.set(ref, doc)

        self._transact(body)

    def rollback(self, owner_id: str, key: str) -> None:
        self._ref(owner_id, key).delete()


# --- cost: spend counter and kill switch ---------------------------------------------------------
_TOTALS_ID = "pilot"  # the LEGACY single counter document: still read, never written
COST_SHARDS = 16


def shard_id(shard_key: str) -> str:
    """``pilot-NN`` for a request key (stable: the same key always lands on the same shard)."""
    digest = hashlib.sha256(shard_key.encode("utf-8")).digest()
    return f"{_TOTALS_ID}-{digest[0] % COST_SHARDS:02d}"
_SWITCH_ID = "global"
_TOTAL_FIELDS = ("total_input_tokens", "total_output_tokens", "total_usd_micros",
                 "provider_input_tokens", "provider_output_tokens", "provider_usd_micros")


class FirestoreCostStore(_Base):
    """The whole-pilot spend counter is SHARDED over ``COST_SHARDS`` sub-documents
    (``pilot-NN``), the shard chosen by a hash of the request key, so concurrent turns rarely
    write the same document (Firestore sustains about one write per second per document).
    ``totals()`` reads the whole collection (one query) and sums the shards PLUS the legacy single
    ``pilot`` document, so counts written before sharding still count (no migration).

    BOUNDED EVENTUAL CONSISTENCY: ``add_spend`` increments its own shard in a transaction, then
    reads the sum (plain reads, not part of the transaction) to evaluate the cap. Two turns
    finishing together may each see a sum without the other, so the cap can be overshot by at
    most the turns in flight at that moment; the next call sees both. The kill-switch trip stays
    atomic (its own transaction, first reason wins) and the next paid-work check reads the sum."""

    def _totals_ref(self, shard_key: str = "") -> Any:
        return self._col(COST_TOTALS).document(shard_id(shard_key))

    def _switch_ref(self, path: str = KILL_PATH_GLOBAL) -> Any:
        # One document per provider path; the whole-pilot switch keeps its original id "global".
        return self._col(KILL_SWITCH).document(known_kill_path(path) if path != KILL_PATH_GLOBAL
                                               else _SWITCH_ID)

    @staticmethod
    def _totals_from(doc: dict[str, Any] | None) -> SpendTotals:
        if doc is None:
            return SpendTotals()
        return SpendTotals(**{name: int(doc.get(name, 0)) for name in _TOTAL_FIELDS})

    @staticmethod
    def _switch_from(doc: dict[str, Any] | None) -> KillSwitchState:
        if doc is None or not doc.get("tripped"):
            return KillSwitchState()
        reason = doc.get("reason")
        return KillSwitchState(True, KillSwitchReason(reason) if reason else None,
                               doc.get("tripped_at"))

    @staticmethod
    def _switch_doc(state: KillSwitchState, now: datetime) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "kind": "kill_switch", "tripped": state.tripped,
                "reason": state.reason.value if state.reason else None,
                "tripped_at": state.tripped_at, "updated_at": now}

    def add_spend(self, delta: CostEntryDelta, budget: SpendBudget | None, now: datetime,
                  *, shard_key: str = "") -> SpendTotals:
        ref = self._totals_ref(shard_key)

        def body(txn: Any) -> None:
            snap = ref.get(transaction=txn)
            t = self._totals_from(snap.to_dict() if snap.exists else None)
            values = {name: getattr(t, name) for name in _TOTAL_FIELDS}
            values["total_input_tokens"] += delta.input_tokens
            values["total_output_tokens"] += delta.output_tokens
            values["total_usd_micros"] += delta.usd_micros
            if delta.provider_attributed:
                values["provider_input_tokens"] += delta.input_tokens
                values["provider_output_tokens"] += delta.output_tokens
                values["provider_usd_micros"] += delta.usd_micros
            txn.set(ref, {"schema_version": SCHEMA_VERSION, "kind": "cost_totals",
                          **values, "updated_at": now})

        self._transact(body)
        total = self.totals()  # the sum of every shard and the legacy document
        over = over_budget(total, budget)
        if over:
            self.trip_kill_switch(KillSwitchReason.SPEND_CAP, now)  # atomic; first reason stays
        return replace(total, over_budget=over)

    def totals(self) -> SpendTotals:
        sums = dict.fromkeys(_TOTAL_FIELDS, 0)
        for snap in self._col(COST_TOTALS).stream():
            doc = snap.to_dict() or {}
            for name in _TOTAL_FIELDS:
                sums[name] += int(doc.get(name, 0))
        return SpendTotals(**sums)

    def kill_switch(self, path: str = KILL_PATH_GLOBAL) -> KillSwitchState:
        snap = self._switch_ref(path).get()
        return self._switch_from(snap.to_dict() if snap.exists else None)

    def trip_kill_switch(self, reason: KillSwitchReason, now: datetime,
                         path: str = KILL_PATH_GLOBAL) -> bool:
        ref = self._switch_ref(path)

        def body(txn: Any) -> bool:
            snap = ref.get(transaction=txn)
            if self._switch_from(snap.to_dict() if snap.exists else None).tripped:
                return False
            txn.set(ref, self._switch_doc(KillSwitchState(True, reason, now), now))
            return True

        return self._transact(body)

    def reset_kill_switch(self, now: datetime, path: str = KILL_PATH_GLOBAL) -> None:
        self._switch_ref(path).set(self._switch_doc(KillSwitchState(), now))


# --- display cache -------------------------------------------------------------------------------
class FirestoreDisplayCache(_Base):
    """Grounded answers for redisplay only: validated on the way in, re-parsed (hash-checked) on
    the way out, owner-scoped, capped per owner, never served past ``cache_expires_at`` or 30 days
    from retrieval.

    NOT enforced here: the GLOBAL entry cap of the in-memory cache (TTL bounds the total instead);
    a query-per-put owner count needs only the automatic single-field ``owner_id`` index."""

    def __init__(
        self, client: Any, clock: Callable[[], datetime], *, max_per_owner: int = 50,
        transactional: Callable[..., Any] | None = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        super().__init__(client, transactional=transactional, max_attempts=max_attempts)
        if max_per_owner < 1:
            raise ValueError("capacities must be positive")
        self._clock = clock
        self._max_owner = max_per_owner

    def __repr__(self) -> str:  # never shows keys, owners or content
        return "FirestoreDisplayCache()"

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must be timezone-aware")
        return now

    def _ref(self, owner_id: str, key: str) -> Any:
        return self._col(DISPLAY_CACHE).document(doc_id(owner_id, key))

    def put(self, owner_id: str, key: str, validated: ValidatedAnswer) -> None:
        DisplayCache._key(owner_id, key)  # noqa: SLF001 - the shared identifier check
        now = self._now()
        if not DisplayCache._storable(validated, now):  # noqa: SLF001
            raise CacheError()
        answer = validated.answer
        expires = min(answer.cache_expires_at, answer.retrieved_at + MAX_MAPS_CACHE)
        ref = self._ref(owner_id, key)
        owned = where(self._col(DISPLAY_CACHE), "owner_id", "==", owner_id)
        doc = {
            "schema_version": SCHEMA_VERSION, "kind": "display_cache_entry", "owner_id": owner_id,
            "key": key, "principal_id": validated.principal_id,
            "answer": answer.model_dump(mode="json"), "created_at": now, "expires_at": expires}

        def body(txn: Any) -> None:
            ref.get(transaction=txn)
            others = [(s.to_dict()["created_at"], s.reference) for s in owned.stream(transaction=txn)
                      if s.id != ref.id]
            txn.set(ref, doc)
            overflow = len(others) + 1 - self._max_owner
            for _created, other in sorted(others, key=lambda item: item[0])[: max(0, overflow)]:
                txn.delete(other)  # this owner's own oldest entries go

        self._transact(body)

    def get(self, owner_id: str, key: str) -> ValidatedAnswer | None:
        DisplayCache._key(owner_id, key)  # noqa: SLF001
        now = self._now()
        ref = self._ref(owner_id, key)
        snap = ref.get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        if doc.get("owner_id") != owner_id or doc.get("key") != key:
            return None
        try:
            answer = GroundedAnswer.model_validate(doc["answer"])
            restored = restore_validated(answer, str(doc["principal_id"]))
        except (ValidationError, KeyError, TypeError, ValueError):
            ref.delete()  # an entry that no longer parses or hashes is as good as gone
            return None
        if (not is_validated(restored) or DisplayCache._expired(answer, now)  # noqa: SLF001
                or not DisplayCache._intact(restored)):  # noqa: SLF001
            ref.delete()
            return None
        return restored

    def place_ids(self, owner_id: str) -> frozenset[str]:
        try:
            DisplayCache._key(owner_id, "k")  # noqa: SLF001
        except CacheError:
            return frozenset()
        now = self._now()
        found: set[str] = set()
        for snap in where(self._col(DISPLAY_CACHE), "owner_id", "==", owner_id).stream():
            doc = snap.to_dict()
            if doc.get("owner_id") != owner_id:
                continue
            try:
                answer = GroundedAnswer.model_validate(doc["answer"])
                restored = restore_validated(answer, str(doc["principal_id"]))
            except (ValidationError, KeyError, TypeError, ValueError):
                continue
            if (is_validated(restored) and not DisplayCache._expired(answer, now)  # noqa: SLF001
                    and DisplayCache._intact(restored)):  # noqa: SLF001
                found.update(place.place_id for place in answer.places)
        return frozenset(found)

    def purge(self) -> int:
        now = self._now()
        removed = 0
        for snap in where(self._col(DISPLAY_CACHE), "expires_at", "<=", now).stream():
            snap.reference.delete()
            removed += 1
        return removed

    def delete_owner(self, owner_id: str) -> int:
        DisplayCache._key(owner_id, "k")  # noqa: SLF001 - validates the owner id shape
        count = 0
        for snap in where(self._col(DISPLAY_CACHE), "owner_id", "==", owner_id).stream():
            snap.reference.delete()
            count += 1
        return count
