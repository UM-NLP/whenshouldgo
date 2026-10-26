"""Durable stores for saved places and the Home/Work ask-state (Phase 1, built dark: decisions 24,
25, 27).

* ``saved_places``: one document per saved place. USER DATA ONLY: a Google place ID, the label the
  user typed, the kind (HOME, WORK or CUSTOM) and a position rounded to 3 decimals (about 100 m).
  Nothing else from Google; no name, address, hours, rating or category. At most one HOME and one
  WORK per owner (saving another replaces it) and at most ``MAX_SAVED_PLACES`` per owner.
* ``saved_place_asks``: one small typed document per owner: for HOME and WORK, when the user last
  declined the fixed question and whether they chose "don't ask again" (for registered users; a
  guest keeps this on the device).

Same client surface as ``firestore.py``. Rules enforced here: the document id is a digest of the
tenant (and place), ``owner_id`` is stored and verified on every read, every document carries
``schema_version`` and ``kind``. Saved places and ask-states have no TTL: they live until the user
deletes them, withdraws the SAVED_PLACE consent or deletes the account.

``replace`` is idempotent on the pending action id that created the entry, so a retried confirm
saves once.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ValidationError

from ..domain import SCHEMA_VERSION, SavedPlace, SavedPlaceKind, evolve
from .firestore import DEFAULT_MAX_ATTEMPTS, StoredDataError, _Base, doc_id

SAVED_PLACES = "saved_places"
SAVED_PLACE_ASKS = "saved_place_asks"
SAVED_PLACE_COLLECTIONS = (SAVED_PLACES, SAVED_PLACE_ASKS)
MAX_SAVED_PLACES = 100  # matches SavedPlaceList.max_length
_ASKABLE = (SavedPlaceKind.HOME, SavedPlaceKind.WORK)
_UNIQUE_KINDS = frozenset(_ASKABLE)

ReplaceOutcome = Literal["saved", "replayed", "full"]


@dataclass(frozen=True)
class StoredPlace:
    place: SavedPlace
    action_id: str | None

    def __repr__(self) -> str:  # never the label, the place ID or the position
        return "StoredPlace(<redacted>)"


@dataclass(frozen=True)
class AskRecord:
    """When the user last declined the fixed question, and whether they chose never to be asked."""

    declined_at: datetime | None = None
    dont_ask_again: bool = False


@runtime_checkable
class SavedPlaceStore(Protocol):
    def replace(self, owner_id: str, place: SavedPlace, action_id: str | None
                ) -> ReplaceOutcome:
        """Atomically save ``place``: replaces an entry for the same place ID and, for HOME and
        WORK, the owner's existing entry of that kind. Idempotent on ``action_id``."""
        ...
    def list(self, owner_id: str) -> tuple[SavedPlace, ...]: ...
    def get(self, owner_id: str, place_id: str) -> SavedPlace | None: ...
    def relabel(self, owner_id: str, place_id: str, user_label: str) -> SavedPlace | None: ...
    def delete(self, owner_id: str, place_id: str) -> int: ...
    def delete_all(self, owner_id: str) -> int: ...


@runtime_checkable
class AskStateStore(Protocol):
    def get(self, owner_id: str) -> dict[SavedPlaceKind, AskRecord]: ...
    def put(self, owner_id: str, kind: SavedPlaceKind, record: AskRecord | None) -> None:
        """Set (or, with None, clear) the ask-state of HOME or WORK."""
        ...
    def delete_owner(self, owner_id: str) -> int: ...


def _order(places: list[SavedPlace]) -> tuple[SavedPlace, ...]:
    rank = {SavedPlaceKind.HOME: 0, SavedPlaceKind.WORK: 1, SavedPlaceKind.CUSTOM: 2}
    return tuple(sorted(places, key=lambda p: (rank[p.label_kind], p.created_at, p.place_id)))


def _plan_replace(rows: list[StoredPlace], place: SavedPlace, action_id: str | None
                  ) -> tuple[ReplaceOutcome, list[str]]:
    """The outcome and the place IDs whose entries the new one supersedes (pure)."""
    if action_id is not None and any(r.action_id == action_id for r in rows):
        return "replayed", []
    superseded = [r.place.place_id for r in rows if r.place.place_id == place.place_id or (
        place.label_kind in _UNIQUE_KINDS and r.place.label_kind == place.label_kind)]
    remaining = len(rows) - len(set(superseded))
    if remaining >= MAX_SAVED_PLACES:
        return "full", []
    return "saved", superseded


def _askable(kind: SavedPlaceKind) -> None:
    if kind not in _ASKABLE:
        raise ValueError("an ask-state exists for HOME and WORK only")


class MemorySavedPlaceStore:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, StoredPlace]] = {}
        self._lock = threading.Lock()

    def replace(self, owner_id: str, place: SavedPlace, action_id: str | None) -> ReplaceOutcome:
        with self._lock:
            rows = self.items.setdefault(owner_id, {})
            outcome, superseded = _plan_replace(list(rows.values()), place, action_id)
            if outcome == "saved":
                for place_id in superseded:
                    rows.pop(place_id, None)
                rows[place.place_id] = StoredPlace(place, action_id)
            return outcome

    def list(self, owner_id: str) -> tuple[SavedPlace, ...]:
        with self._lock:
            return _order([r.place for r in self.items.get(owner_id, {}).values()])

    def get(self, owner_id: str, place_id: str) -> SavedPlace | None:
        with self._lock:
            row = self.items.get(owner_id, {}).get(place_id)
            return row.place if row else None

    def relabel(self, owner_id: str, place_id: str, user_label: str) -> SavedPlace | None:
        with self._lock:
            rows = self.items.get(owner_id, {})
            row = rows.get(place_id)
            if row is None:
                return None
            new = evolve(row.place, user_label=user_label)
            rows[place_id] = replace(row, place=new)
            return new

    def delete(self, owner_id: str, place_id: str) -> int:
        with self._lock:
            return 1 if self.items.get(owner_id, {}).pop(place_id, None) is not None else 0

    def delete_all(self, owner_id: str) -> int:
        with self._lock:
            return len(self.items.pop(owner_id, {}))


class MemoryAskStateStore:
    def __init__(self) -> None:
        self.items: dict[str, dict[SavedPlaceKind, AskRecord]] = {}
        self._lock = threading.Lock()

    def get(self, owner_id: str) -> dict[SavedPlaceKind, AskRecord]:
        with self._lock:
            return dict(self.items.get(owner_id, {}))

    def put(self, owner_id: str, kind: SavedPlaceKind, record: AskRecord | None) -> None:
        _askable(kind)
        with self._lock:
            state = self.items.setdefault(owner_id, {})
            if record is None:
                state.pop(kind, None)
            else:
                state[kind] = record
            if not state:
                self.items.pop(owner_id, None)

    def delete_owner(self, owner_id: str) -> int:
        with self._lock:
            return 1 if self.items.pop(owner_id, None) else 0


class FirestoreSavedPlaceStore(_Base):
    def _ref(self, owner_id: str, place_id: str) -> Any:
        return self._col(SAVED_PLACES).document(doc_id(owner_id, "place", place_id))

    @staticmethod
    def _decode(owner_id: str, doc: dict[str, Any]) -> StoredPlace:
        if doc.get("owner_id") != owner_id or doc.get("kind") != "saved_place_doc":
            raise StoredDataError("saved place does not belong to this tenant")
        try:
            place = SavedPlace.model_validate(doc["place"])
            action_id = doc.get("action_id")
        except (ValidationError, KeyError, TypeError):
            raise StoredDataError("saved place does not match the schema") from None
        return StoredPlace(place, action_id if isinstance(action_id, str) else None)

    @staticmethod
    def _encode(owner_id: str, row: StoredPlace) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "kind": "saved_place_doc", "owner_id": owner_id,
            "place_id": row.place.place_id, "label_kind": row.place.label_kind.value,
            "place": row.place.model_dump(mode="json"), "action_id": row.action_id,
            "updated_at": row.place.created_at}

    def _rows(self, owner_id: str, transaction: Any = None) -> list[tuple[Any, StoredPlace]]:
        query = self._col(SAVED_PLACES).where("owner_id", "==", owner_id)
        snaps = query.stream(transaction=transaction) if transaction is not None else query.stream()
        return [(snap.reference, self._decode(owner_id, snap.to_dict())) for snap in snaps]

    def replace(self, owner_id: str, place: SavedPlace, action_id: str | None) -> ReplaceOutcome:
        def body(txn: Any) -> ReplaceOutcome:
            rows = self._rows(owner_id, txn)  # every read before any write
            outcome, superseded = _plan_replace([r for _ref, r in rows], place, action_id)
            if outcome == "saved":
                for ref, row in rows:
                    if row.place.place_id in superseded:
                        txn.delete(ref)
                txn.set(self._ref(owner_id, place.place_id),
                        self._encode(owner_id, StoredPlace(place, action_id)))
            return outcome

        return self._transact(body)

    def list(self, owner_id: str) -> tuple[SavedPlace, ...]:
        return _order([r.place for _ref, r in self._rows(owner_id)])

    def get(self, owner_id: str, place_id: str) -> SavedPlace | None:
        snap = self._ref(owner_id, place_id).get()
        return self._decode(owner_id, snap.to_dict()).place if snap.exists else None

    def relabel(self, owner_id: str, place_id: str, user_label: str) -> SavedPlace | None:
        ref = self._ref(owner_id, place_id)

        def body(txn: Any) -> SavedPlace | None:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return None
            row = self._decode(owner_id, snap.to_dict())
            new = evolve(row.place, user_label=user_label)
            txn.set(ref, self._encode(owner_id, replace(row, place=new)))
            return new

        return self._transact(body)

    def delete(self, owner_id: str, place_id: str) -> int:
        ref = self._ref(owner_id, place_id)

        def body(txn: Any) -> int:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                return 0
            self._decode(owner_id, snap.to_dict())  # verifies the tenant before deleting
            txn.delete(ref)
            return 1

        return self._transact(body)

    def delete_all(self, owner_id: str) -> int:
        count = 0
        for snap in self._col(SAVED_PLACES).where("owner_id", "==", owner_id).stream():
            snap.reference.delete()
            count += 1
        return count


class FirestoreAskStateStore(_Base):
    def _ref(self, owner_id: str) -> Any:
        return self._col(SAVED_PLACE_ASKS).document(doc_id(owner_id))

    @staticmethod
    def _decode(owner_id: str, doc: dict[str, Any]) -> dict[SavedPlaceKind, AskRecord]:
        if doc.get("owner_id") != owner_id or doc.get("kind") != "saved_place_ask_state":
            raise StoredDataError("ask state does not belong to this tenant")
        try:
            state: dict[SavedPlaceKind, AskRecord] = {}
            for row in doc.get("asks", []):
                kind = SavedPlaceKind(row["label_kind"])
                _askable(kind)
                state[kind] = AskRecord(row.get("declined_at"), bool(row.get("dont_ask_again")))
            return state
        except (KeyError, ValueError, TypeError):
            raise StoredDataError("ask state does not match the schema") from None

    def get(self, owner_id: str) -> dict[SavedPlaceKind, AskRecord]:
        snap = self._ref(owner_id).get()
        return self._decode(owner_id, snap.to_dict()) if snap.exists else {}

    def put(self, owner_id: str, kind: SavedPlaceKind, record: AskRecord | None) -> None:
        _askable(kind)
        ref = self._ref(owner_id)

        def body(txn: Any) -> None:
            snap = ref.get(transaction=txn)
            state = self._decode(owner_id, snap.to_dict()) if snap.exists else {}
            if record is None:
                state.pop(kind, None)
            else:
                state[kind] = record
            if not state:
                if snap.exists:
                    txn.delete(ref)
                return
            txn.set(ref, {
                "schema_version": SCHEMA_VERSION, "kind": "saved_place_ask_state",
                "owner_id": owner_id,
                "asks": [{"label_kind": k.value, "declined_at": r.declined_at,
                          "dont_ask_again": r.dont_ask_again} for k, r in sorted(
                              state.items(), key=lambda item: item[0].value)]})

        self._transact(body)

    def delete_owner(self, owner_id: str) -> int:
        ref = self._ref(owner_id)
        if not ref.get().exists:
            return 0
        ref.delete()
        return 1


def memory_saved_place_stores() -> tuple[SavedPlaceStore, AskStateStore]:
    return MemorySavedPlaceStore(), MemoryAskStateStore()


def firestore_saved_place_stores(
    client: Any, *, transactional: Callable[..., Any] | None = None,
    max_attempts: int | None = None,
) -> tuple[SavedPlaceStore, AskStateStore]:
    common = {"transactional": transactional, "max_attempts": max_attempts or DEFAULT_MAX_ATTEMPTS}
    return FirestoreSavedPlaceStore(client, **common), FirestoreAskStateStore(client, **common)


__all__ = [
    "MAX_SAVED_PLACES", "SAVED_PLACES", "SAVED_PLACE_ASKS", "SAVED_PLACE_COLLECTIONS", "AskRecord",
    "AskStateStore", "FirestoreAskStateStore", "FirestoreSavedPlaceStore", "MemoryAskStateStore",
    "MemorySavedPlaceStore", "SavedPlaceStore", "StoredPlace", "firestore_saved_place_stores",
    "memory_saved_place_stores",
]
