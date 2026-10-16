"""In-memory display cache for VALIDATED grounded answers (docs/legal/
google-maps-llm-policy-summary.md section 5). Firestore comes later behind ``DisplayCacheStore``.

Only a ``ValidatedAnswer`` (minted by the validator) can be stored: ``put`` refuses a bare
``GroundedAnswer`` or a forged wrapper. An entry is never served at or after its
``cache_expires_at``, and never past 30 days from retrieval even if the answer claims longer:
``put`` refuses such an answer (and one retrieved in the future) and ``get`` re-checks both
limits plus the hashes recorded at validation time.

Capacity is fair per owner: entries are indexed by owner, an owner is capped, and when the global
cap is hit a writer over its fair share evicts its OWN oldest entry, otherwise the OLDEST entry of
the owner holding the MOST entries is evicted (ties go to the owner that is inserting), so many
owners or one flooder cannot push a well-behaved owner below its share. Owners can never outnumber entries, so their number is bounded by ``max_entries``. The
eviction scan is over owners only (at most ``max_entries``), not over entries.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain import MAX_MAPS_CACHE, ContentClass, EvidenceStorageClass
from ._validated import ValidatedAnswer, is_validated
from .contracts import GroundedAnswer, content_hash, display_hash
from .errors import CacheError

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@runtime_checkable
class DisplayCacheStore(Protocol):
    def put(self, owner_id: str, key: str, validated: ValidatedAnswer) -> None: ...
    def get(self, owner_id: str, key: str) -> ValidatedAnswer | None: ...
    def purge(self) -> int: ...
    def delete_owner(self, owner_id: str) -> int: ...


class DisplayCache:
    def __init__(
        self, clock: Callable[[], datetime], *, max_entries: int = 1_000, max_per_owner: int = 50
    ) -> None:
        if max_entries < 1 or max_per_owner < 1:
            raise ValueError("capacities must be positive")
        self._clock = clock
        self._max = max_entries
        self._max_owner = max_per_owner
        self._lock = threading.Lock()
        self._entries: dict[str, OrderedDict[str, ValidatedAnswer]] = {}
        self._count = 0

    def __repr__(self) -> str:  # never shows keys, owners or content
        return f"DisplayCache(entries={len(self)})"

    def __len__(self) -> int:
        with self._lock:
            return self._count

    @property
    def owner_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must be timezone-aware")
        return now

    @staticmethod
    def _key(owner_id: str, key: str) -> tuple[str, str]:
        if not (isinstance(owner_id, str) and isinstance(key, str)
                and _ID.fullmatch(owner_id) and _ID.fullmatch(key)):
            raise CacheError("owner id and key must be bounded identifiers")
        return owner_id, key

    @staticmethod
    def _expired(answer: GroundedAnswer, now: datetime) -> bool:
        return now >= answer.cache_expires_at or now >= answer.retrieved_at + MAX_MAPS_CACHE

    @staticmethod
    def _intact(validated: ValidatedAnswer) -> bool:
        """The answer still hashes to what was recorded when the validator accepted it."""
        try:
            answer = validated.answer
            fields = [(p.place_id, p.attribution_title, p.attribution_url, p.place_url) for p in answer.places]
            return (
                content_hash(answer.text) == answer.content_hash == validated.content_hash
                and display_hash(answer.text, fields) == answer.display_hash == validated.display_hash
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _storable(validated: object, now: datetime) -> bool:
        if not is_validated(validated):
            return False
        try:
            answer = validated.answer  # type: ignore[attr-defined]
            return (
                isinstance(answer, GroundedAnswer)
                and answer.content_class == ContentClass.MAPS_GROUNDED
                and answer.storage_class == EvidenceStorageClass.DISPLAY_CACHE
                and answer.retrieved_at < answer.cache_expires_at <= answer.retrieved_at + MAX_MAPS_CACHE
                and answer.retrieved_at <= now
                and not DisplayCache._expired(answer, now)
                and DisplayCache._intact(validated)  # type: ignore[arg-type]
            )
        except (AttributeError, TypeError):
            return False

    def _largest_owner(self, prefer: str) -> str:
        best, size = prefer, len(self._entries.get(prefer, ()))
        for owner, bucket in self._entries.items():
            if len(bucket) > size:
                best, size = owner, len(bucket)
        return best

    def _eviction_victim(self, writer: str) -> str:
        """Whose oldest entry goes when the global cap is hit. A writer over its fair share
        (the cap divided by the number of owners, rounded up) evicts its OWN oldest entry, so a
        tenant at its share can never push another tenant out; a writer within its share takes
        from the largest owner (ties go to the writer)."""
        share = -(-self._max // max(1, len(self._entries)))
        if len(self._entries.get(writer, ())) > share:
            return writer
        return self._largest_owner(writer)

    def put(self, owner_id: str, key: str, validated: ValidatedAnswer) -> None:
        self._key(owner_id, key)
        now = self._now()
        if not self._storable(validated, now):
            raise CacheError()
        with self._lock:
            bucket = self._entries.setdefault(owner_id, OrderedDict())
            if key in bucket:
                del bucket[key]
                self._count -= 1
            bucket[key] = validated
            self._count += 1
            while len(bucket) > self._max_owner:  # this owner's own cap: its oldest goes
                bucket.popitem(last=False)
                self._count -= 1
            while self._count > self._max:
                victim = self._eviction_victim(owner_id)
                self._entries[victim].popitem(last=False)
                self._count -= 1
                if not self._entries[victim]:
                    del self._entries[victim]
            if owner_id in self._entries and not self._entries[owner_id]:
                del self._entries[owner_id]

    def get(self, owner_id: str, key: str) -> ValidatedAnswer | None:
        self._key(owner_id, key)
        now = self._now()
        with self._lock:
            bucket = self._entries.get(owner_id)
            validated = bucket.get(key) if bucket is not None else None
            if validated is None or bucket is None:
                return None
            if not is_validated(validated) or self._expired(validated.answer, now) or not self._intact(validated):
                del bucket[key]
                self._count -= 1
                if not bucket:
                    del self._entries[owner_id]
                return None
            return validated

    def purge(self) -> int:
        now = self._now()
        removed = 0
        with self._lock:
            for owner in list(self._entries):
                bucket = self._entries[owner]
                for key in [k for k, v in bucket.items() if not is_validated(v) or self._expired(v.answer, now)]:
                    del bucket[key]
                    removed += 1
                if not bucket:
                    del self._entries[owner]
            self._count -= removed
        return removed

    def delete_owner(self, owner_id: str) -> int:
        if not (isinstance(owner_id, str) and _ID.fullmatch(owner_id)):
            raise CacheError("owner id must be a bounded identifier")
        with self._lock:
            bucket = self._entries.pop(owner_id, None)
            count = len(bucket) if bucket is not None else 0
            self._count -= count
            return count
