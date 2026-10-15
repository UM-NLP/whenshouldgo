"""In-memory display cache for grounded answers (docs/legal/google-maps-llm-policy-summary.md
section 5). Firestore comes later behind ``DisplayCacheStore``.

An entry is never served at or after its ``cache_expires_at``, and never past 30 days from
retrieval even if the stored answer claims longer: ``put`` refuses such an answer (and one
retrieved in the future) and ``get`` re-checks both limits and both content hashes. A per-owner
capacity cap keeps one tenant from evicting another's entries. Expired entries are removed on
read and by ``purge``; ``delete_owner`` supports account deletion and consent withdrawal.
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain import MAX_MAPS_CACHE, ContentClass, EvidenceStorageClass
from .contracts import GroundedAnswer, content_hash, display_hash
from .errors import CacheError

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@runtime_checkable
class DisplayCacheStore(Protocol):
    def put(self, owner_id: str, key: str, answer: GroundedAnswer) -> None: ...
    def get(self, owner_id: str, key: str) -> GroundedAnswer | None: ...
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
        self._entries: OrderedDict[tuple[str, str], GroundedAnswer] = OrderedDict()

    def __repr__(self) -> str:  # never shows keys, owners or content
        return f"DisplayCache(entries={len(self)})"

    def __len__(self) -> int:
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
    def _intact(answer: GroundedAnswer) -> bool:
        """Both content hashes still match what is stored (catches in-memory corruption)."""
        try:
            fields = [(p.place_id, p.attribution_title, p.attribution_url, p.place_url) for p in answer.places]
            return (content_hash(answer.text) == answer.content_hash
                    and display_hash(answer.text, fields) == answer.display_hash)
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _storable(answer: object, now: datetime) -> bool:
        if not isinstance(answer, GroundedAnswer):
            return False
        try:
            return (
                answer.content_class == ContentClass.MAPS_GROUNDED
                and answer.storage_class == EvidenceStorageClass.DISPLAY_CACHE
                and answer.retrieved_at < answer.cache_expires_at <= answer.retrieved_at + MAX_MAPS_CACHE
                and answer.retrieved_at <= now
                and not DisplayCache._expired(answer, now)
                and DisplayCache._intact(answer)
            )
        except (AttributeError, TypeError):
            return False

    def put(self, owner_id: str, key: str, answer: GroundedAnswer) -> None:
        slot = self._key(owner_id, key)
        now = self._now()
        if not self._storable(answer, now):
            raise CacheError()
        with self._lock:
            self._entries.pop(slot, None)
            self._entries[slot] = answer
            # A per-owner cap first, so one tenant cannot evict another tenant's entries.
            mine = [s for s in self._entries if s[0] == owner_id]
            for old in mine[: max(0, len(mine) - self._max_owner)]:
                del self._entries[old]  # this owner's oldest
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)  # then the global cap, oldest first

    def get(self, owner_id: str, key: str) -> GroundedAnswer | None:
        slot = self._key(owner_id, key)
        now = self._now()
        with self._lock:
            answer = self._entries.get(slot)
            if answer is None:
                return None
            if self._expired(answer, now) or not self._intact(answer):
                del self._entries[slot]
                return None
            return answer

    def purge(self) -> int:
        now = self._now()
        with self._lock:
            stale = [slot for slot, a in self._entries.items() if self._expired(a, now)]
            for slot in stale:
                del self._entries[slot]
            return len(stale)

    def delete_owner(self, owner_id: str) -> int:
        if not (isinstance(owner_id, str) and _ID.fullmatch(owner_id)):
            raise CacheError("owner id must be a bounded identifier")
        with self._lock:
            mine = [slot for slot in self._entries if slot[0] == owner_id]
            for slot in mine:
                del self._entries[slot]
            return len(mine)
