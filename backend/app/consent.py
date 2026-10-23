"""Consent ledger (docs/data-and-history-design.md) for foreground location.

APPEND-ONLY, decided here: a ``ConsentRecord`` is frozen and never mutated. A grant appends a
record; a withdrawal appends a NEW record with the same ``id`` and ``withdrawn_at`` set. A consent
is ACTIVE when the latest ledger entry for its id has no ``withdrawn_at``. A grant after a
withdrawal is a new consent with a new id, and a repeated grant while one is active changes
nothing (idempotent). A record holds the purpose, notice version, processors (GOOGLE only for
location: the model never receives a location), scope and times: never a coordinate.

``InMemoryConsentStore`` is PER PROCESS. Shared, durable ledgers (Firestore) come with issue
ACCESS-02 (#35); until then a Cloud Run restart or a second instance forgets consent, which fails
closed (the user is asked to consent again).

``OperationLimiter`` is a small per-principal sliding window over consent endpoint calls.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from .domain import ConsentProcessor, ConsentPurpose, ConsentRecord, ConsentScope, evolve

# The notice text the client shows before it calls POST /v1/me/consents. A grant must name this
# exact version: a stale client cannot record consent to a notice the server no longer serves.
LOCATION_NOTICE_VERSION = "location-notice-1"
SUPPORTED_PURPOSES = frozenset({ConsentPurpose.LOCATION_FOREGROUND})
PROCESSORS: dict[ConsentPurpose, tuple[ConsentProcessor, ...]] = {
    ConsentPurpose.LOCATION_FOREGROUND: (ConsentProcessor.GOOGLE,),
}
NOTICE_VERSIONS: dict[ConsentPurpose, str] = {
    ConsentPurpose.LOCATION_FOREGROUND: LOCATION_NOTICE_VERSION,
}
_CONTEXT = {ConsentPurpose.LOCATION_FOREGROUND: "location.foreground"}


# Upper bound on principals an OperationLimiter tracks (least recently used are dropped). A dropped
# owner simply gets a fresh window. TTLs, IP buckets and App Check are tracked under ACCESS-02 (#35).
MAX_TRACKED_OWNERS = 50_000


class WrongNoticeVersion(Exception):
    """The grant named a notice version the server does not currently serve."""


@runtime_checkable
class ConsentStore(Protocol):
    def grant(self, owner_id: str, purpose: ConsentPurpose, notice_version: str) -> ConsentRecord: ...
    def withdraw(self, owner_id: str, purpose: ConsentPurpose) -> bool: ...
    def active(self, owner_id: str) -> tuple[ConsentRecord, ...]: ...
    def has_active(self, owner_id: str, purpose: ConsentPurpose) -> bool: ...
    def ledger(self, owner_id: str) -> tuple[ConsentRecord, ...]: ...


class InMemoryConsentStore:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ledger: dict[str, list[ConsentRecord]] = defaultdict(list)
        self._lock = threading.Lock()

    def _latest(self, owner_id: str) -> dict[str, ConsentRecord]:
        latest: dict[str, ConsentRecord] = {}
        for record in self._ledger.get(owner_id, ()):
            latest[record.id] = record
        return latest

    def _active_for(self, owner_id: str, purpose: ConsentPurpose) -> ConsentRecord | None:
        for record in self._latest(owner_id).values():
            if (record.purpose == purpose and record.withdrawn_at is None
                    and record.notice_version == NOTICE_VERSIONS.get(purpose)):
                return record
        return None

    def grant(self, owner_id: str, purpose: ConsentPurpose, notice_version: str) -> ConsentRecord:
        if purpose not in SUPPORTED_PURPOSES or notice_version != NOTICE_VERSIONS[purpose]:
            raise WrongNoticeVersion()
        with self._lock:
            existing = self._active_for(owner_id, purpose)
            if existing is not None:
                return existing
            record = ConsentRecord(
                id=f"consent-{uuid.uuid4().hex[:24]}", owner_id=owner_id, purpose=purpose,
                notice_version=notice_version, processors=PROCESSORS[purpose],
                scope=ConsentScope(), granted_at=self._clock(), context=_CONTEXT[purpose])
            self._ledger[owner_id].append(record)
            return record

    def withdraw(self, owner_id: str, purpose: ConsentPurpose) -> bool:
        with self._lock:
            existing = self._active_for(owner_id, purpose)
            if existing is None:
                return False
            now = max(self._clock(), existing.granted_at)
            self._ledger[owner_id].append(evolve(existing, withdrawn_at=now))
            return True

    def active(self, owner_id: str) -> tuple[ConsentRecord, ...]:
        with self._lock:
            return tuple(
                r for r in self._latest(owner_id).values()
                if r.withdrawn_at is None and r.notice_version == NOTICE_VERSIONS.get(r.purpose))

    def has_active(self, owner_id: str, purpose: ConsentPurpose) -> bool:
        with self._lock:
            return self._active_for(owner_id, purpose) is not None

    def ledger(self, owner_id: str) -> tuple[ConsentRecord, ...]:
        with self._lock:
            return tuple(self._ledger.get(owner_id, ()))


class OperationLimiter:
    """At most ``max_operations`` calls per principal in a rolling ``window``."""

    def __init__(
        self, clock: Callable[[], datetime] | None = None, *, max_operations: int = 30,
        window: timedelta = timedelta(hours=1),
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max = max_operations
        self._window = window
        self._calls: OrderedDict[str, deque[datetime]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, owner_id: str) -> int | None:
        """None when the call is allowed (and counted); else the seconds until it would be."""
        now = self._clock()
        with self._lock:
            # Owners are ordered by last use: drop those whose whole window has expired.
            while self._calls:
                oldest = next(iter(self._calls))
                recent = self._calls[oldest]
                if oldest != owner_id and (not recent or recent[-1] <= now - self._window):
                    del self._calls[oldest]
                else:
                    break
            calls = self._calls.get(owner_id)
            if calls is None:
                calls = self._calls[owner_id] = deque()
            self._calls.move_to_end(owner_id)
            while calls and calls[0] <= now - self._window:
                calls.popleft()
            if len(calls) >= self._max:
                return max(1, int((calls[0] + self._window - now).total_seconds()) + 1)
            calls.append(now)
            while len(self._calls) > MAX_TRACKED_OWNERS:
                self._calls.popitem(last=False)
            return None
