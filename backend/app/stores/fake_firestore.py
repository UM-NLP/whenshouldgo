"""An in-process fake of the SMALL Firestore surface the adapters use.

It lets the Firestore adapters (``stores/firestore.py``) run in unit tests with no emulator, no
network and no credentials (decision 26: code only, no cloud resources).

WHAT IT MODELS: documents in top-level collections; ``get`` / ``set`` / ``update`` / ``create`` /
``delete``; equality and inequality ``where`` queries; transactions with optimistic concurrency
(a conflicting commit aborts and ``fake_transactional`` retries, like the real client's
``@firestore.transactional``) and the real service's rule that reads precede writes; the
``SERVER_TIMESTAMP`` sentinel; a TTL sweep (``run_ttl_sweep``); document-id rules; and the value
types the real client accepts (anything else raises ``TypeError``, so an adapter cannot store a
``Decimal``, a tuple, a set, an enum or a naive datetime by accident).

WHAT IT DOES NOT PROVE: real indexes (a query that needs a composite index works here), real TTL
policy timing (the real service deletes expired documents hours later, so adapters ALSO check
expiry on read), real contention and latency (1 write per second per document, transaction lock
waits), security rules, quotas, or any provider behaviour. A test against the real emulator is a
TODO (docs/backlog.md).
"""
from __future__ import annotations

import copy
import math
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

_MAX_DOC_BYTES = 1_000_000
_ID_RESERVED = re.compile(r"^__.*__$")
_INT64 = 2**63


class _ServerTimestamp:
    def __repr__(self) -> str:
        return "SERVER_TIMESTAMP"


SERVER_TIMESTAMP = _ServerTimestamp()


class AbortedError(Exception):
    """The transaction conflicted with another writer."""


class NotFoundError(Exception):
    """``update`` of a document that does not exist."""


class AlreadyExistsError(Exception):
    """``create`` of a document that already exists."""


def _check_id(value: str) -> str:
    if (not isinstance(value, str) or not value or "/" in value or value in (".", "..")
            or _ID_RESERVED.match(value) or len(value.encode("utf-8")) > 1500):
        raise ValueError("invalid Firestore document or collection id")
    return value


def _check_value(value: Any, path: str = "") -> None:
    kind = type(value)
    if value is None or kind in (bool, str, bytes):
        return
    if kind is int:
        if not -_INT64 <= value < _INT64:
            raise TypeError(f"{path}: integer out of the int64 range")
        return
    if kind is float:
        if math.isnan(value) or math.isinf(value):
            raise TypeError(f"{path}: NaN and infinity are not stored")
        return
    if kind is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError(f"{path}: datetimes must be timezone-aware")
        return
    if value is SERVER_TIMESTAMP:
        return
    if kind is list:
        for i, item in enumerate(value):
            _check_value(item, f"{path}[{i}]")
        return
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str or not key:
                raise TypeError(f"{path}: map keys must be non-empty strings")
            _check_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path}: {kind.__name__} is not a Firestore value type")


def _resolve(value: Any, now: datetime) -> Any:
    if value is SERVER_TIMESTAMP:
        return now
    if type(value) is list:
        return [_resolve(v, now) for v in value]
    if type(value) is dict:
        return {k: _resolve(v, now) for k, v in value.items()}
    return value


def _validated(data: dict[str, Any]) -> dict[str, Any]:
    if type(data) is not dict:
        raise TypeError("a document is a dict")
    _check_value(data)
    if len(repr(data).encode("utf-8")) > _MAX_DOC_BYTES:
        raise ValueError("document exceeds the 1 MiB limit")
    return data


class FakeSnapshot:
    def __init__(self, ref: FakeDocRef, data: dict[str, Any] | None) -> None:
        self.reference = ref
        self.id = ref.id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field: str) -> Any:
        if self._data is None:
            raise KeyError(field)
        return copy.deepcopy(self._data[field])


class FakeDocRef:
    def __init__(self, client: FakeFirestoreClient, collection: str, doc_id: str) -> None:
        self._client = client
        self.collection_id = collection
        self.id = _check_id(doc_id)

    @property
    def path(self) -> str:
        return f"{self.collection_id}/{self.id}"

    def get(self, transaction: FakeTransaction | None = None) -> FakeSnapshot:
        if transaction is not None:
            return transaction._read(self)
        with self._client._lock:
            return FakeSnapshot(self, self._client._data(self))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        self._client._commit([("set", self, _validated(data), merge)], {})

    def create(self, data: dict[str, Any]) -> None:
        self._client._commit([("create", self, _validated(data), False)], {})

    def update(self, data: dict[str, Any]) -> None:
        self._client._commit([("update", self, _validated(data), False)], {})

    def delete(self) -> None:
        self._client._commit([("delete", self, None, False)], {})


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
}


class FakeQuery:
    def __init__(self, client: FakeFirestoreClient, collection: str,
                 filters: tuple[tuple[str, str, Any], ...] = (), limit: int | None = None) -> None:
        self._client = client
        self._collection = collection
        self._filters = filters
        self._limit = limit

    def where(self, field: str, op: str, value: Any) -> FakeQuery:
        if op not in _OPS:
            raise ValueError("unsupported operator")
        _check_value(value)
        return FakeQuery(self._client, self._collection, (*self._filters, (field, op, value)),
                         self._limit)

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(self._client, self._collection, self._filters, count)

    def stream(self, transaction: FakeTransaction | None = None) -> list[FakeSnapshot]:
        with self._client._lock:
            rows = []
            for doc_id, data in sorted(self._client._collection(self._collection).items()):
                if all(f in data and _OPS[op](data[f], v) for f, op, v in self._filters):
                    rows.append(FakeSnapshot(
                        FakeDocRef(self._client, self._collection, doc_id), data))
            if self._limit is not None:
                rows = rows[: self._limit]
            if transaction is not None:
                transaction._note_query(self._collection, rows)
            return rows

    def get(self, transaction: FakeTransaction | None = None) -> list[FakeSnapshot]:
        return self.stream(transaction)


class FakeCollection(FakeQuery):
    def __init__(self, client: FakeFirestoreClient, name: str) -> None:
        super().__init__(client, _check_id(name))

    def document(self, doc_id: str) -> FakeDocRef:
        return FakeDocRef(self._client, self._collection, doc_id)


class FakeTransaction:
    def __init__(self, client: FakeFirestoreClient, max_attempts: int = 5) -> None:
        self._client = client
        self.max_attempts = max_attempts
        self._reset()

    def _reset(self) -> None:
        self._versions: dict[tuple[str, str], int] = {}
        self._structure: dict[str, int] = {}
        self._writes: list[tuple[str, FakeDocRef, dict[str, Any] | None, bool]] = []

    def _read(self, ref: FakeDocRef) -> FakeSnapshot:
        if self._writes:
            raise RuntimeError("Firestore transactions must do all reads before any write")
        with self._client._lock:
            self._versions[(ref.collection_id, ref.id)] = self._client._version(ref)
            return FakeSnapshot(ref, self._client._data(ref))

    def _note_query(self, collection: str, rows: list[FakeSnapshot]) -> None:
        if self._writes:
            raise RuntimeError("Firestore transactions must do all reads before any write")
        self._structure[collection] = self._client._structure.get(collection, 0)
        for snap in rows:
            self._versions[(collection, snap.id)] = self._client._version(snap.reference)

    def get(self, ref_or_query: FakeDocRef | FakeQuery) -> Any:
        return ref_or_query.get(transaction=self)

    def set(self, ref: FakeDocRef, data: dict[str, Any], merge: bool = False) -> None:
        self._writes.append(("set", ref, _validated(data), merge))

    def create(self, ref: FakeDocRef, data: dict[str, Any]) -> None:
        self._writes.append(("create", ref, _validated(data), False))

    def update(self, ref: FakeDocRef, data: dict[str, Any]) -> None:
        self._writes.append(("update", ref, _validated(data), False))

    def delete(self, ref: FakeDocRef) -> None:
        self._writes.append(("delete", ref, None, False))

    def _commit(self) -> None:
        self._client._commit(self._writes, self._versions, self._structure)


def fake_transactional(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The fake's ``firestore.transactional``: run ``fn(transaction)`` and retry it when the
    commit aborts on a conflict, up to ``transaction.max_attempts``."""

    def run(transaction: FakeTransaction, *args: Any, **kwargs: Any) -> Any:
        for _attempt in range(transaction.max_attempts):
            transaction._reset()
            result = fn(transaction, *args, **kwargs)
            try:
                transaction._commit()
            except AbortedError:
                continue
            return result
        raise AbortedError("transaction aborted after the maximum number of attempts")

    return run


class FakeFirestoreClient:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._docs: dict[str, dict[str, dict[str, Any]]] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._structure: dict[str, int] = {}
        self.aborted_commits = 0  # for tests: how many commits lost a conflict

    # -- public surface used by the adapters ----------------------------------------------------
    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, name)

    def transaction(self, max_attempts: int = 5) -> FakeTransaction:
        return FakeTransaction(self, max_attempts)

    # The adapters take the ``transactional`` decorator from the client when it has one (the real
    # client library does not; the fake does, so no google import is needed in tests).
    transactional = staticmethod(fake_transactional)

    # -- test helpers ---------------------------------------------------------------------------
    def documents(self, collection: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """Every stored document as (path, LIVE dict): tests scan it and may tamper with it."""
        with self._lock:
            names = [collection] if collection is not None else sorted(self._docs)
            return [(f"{name}/{doc_id}", data) for name in names
                    for doc_id, data in sorted(self._docs.get(name, {}).items())]

    def run_ttl_sweep(self, field: str = "expires_at") -> int:
        """Delete documents whose TTL ``field`` is at or before now (what a TTL policy does, late)."""
        now = self._clock()
        removed = 0
        with self._lock:
            for name in list(self._docs):
                for doc_id, data in list(self._docs[name].items()):
                    expires = data.get(field)
                    if isinstance(expires, datetime) and expires <= now:
                        self._apply_delete(name, doc_id)
                        removed += 1
        return removed

    # -- internals ------------------------------------------------------------------------------
    def _collection(self, name: str) -> dict[str, dict[str, Any]]:
        return self._docs.get(name, {})

    def _data(self, ref: FakeDocRef) -> dict[str, Any] | None:
        return self._docs.get(ref.collection_id, {}).get(ref.id)

    def _version(self, ref: FakeDocRef) -> int:
        return self._versions.get((ref.collection_id, ref.id), 0)

    def _apply_delete(self, collection: str, doc_id: str) -> None:
        self._docs.get(collection, {}).pop(doc_id, None)
        self._versions[(collection, doc_id)] = self._versions.get((collection, doc_id), 0) + 1
        self._structure[collection] = self._structure.get(collection, 0) + 1

    def _commit(
        self,
        writes: list[tuple[str, FakeDocRef, dict[str, Any] | None, bool]],
        read_versions: dict[tuple[str, str], int],
        read_structure: dict[str, int] | None = None,
    ) -> None:
        with self._lock:
            for (collection, doc_id), version in read_versions.items():
                if self._versions.get((collection, doc_id), 0) != version:
                    self.aborted_commits += 1
                    raise AbortedError("a document read by the transaction changed")
            for collection, version in (read_structure or {}).items():
                if self._structure.get(collection, 0) != version:
                    self.aborted_commits += 1
                    raise AbortedError("a queried collection changed")
            # Check preconditions against the state the writes will see, before applying any.
            exists = {(r.collection_id, r.id): self._data(r) is not None for _op, r, _d, _m in writes}
            for op, ref, _data, _merge in writes:
                key = (ref.collection_id, ref.id)
                if op == "update" and not exists[key]:
                    raise NotFoundError(ref.path)
                if op == "create" and exists[key]:
                    raise AlreadyExistsError(ref.path)
                exists[key] = op != "delete"
            now = self._clock()
            for op, ref, data, merge in writes:
                name, doc_id = ref.collection_id, ref.id
                if op == "delete":
                    self._apply_delete(name, doc_id)
                    continue
                assert data is not None
                bucket = self._docs.setdefault(name, {})
                resolved = copy.deepcopy(_resolve(data, now))
                existed = doc_id in bucket
                if op == "update" or (op == "set" and merge and existed):
                    bucket[doc_id] = {**bucket[doc_id], **resolved}
                else:
                    bucket[doc_id] = resolved
                self._versions[(name, doc_id)] = self._versions.get((name, doc_id), 0) + 1
                if not existed:
                    self._structure[name] = self._structure.get(name, 0) + 1
