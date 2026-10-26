"""Consent ledger (docs/data-and-history-design.md) for foreground location.

APPEND-ONLY, decided here: a ``ConsentRecord`` is frozen and never mutated. A grant appends a
record; a withdrawal appends a NEW record with the same ``id`` and ``withdrawn_at`` set. A consent
is ACTIVE when the latest ledger entry for its id has no ``withdrawn_at``. A grant after a
withdrawal is a new consent with a new id, and a repeated grant while one is active changes
nothing (idempotent). A record holds the purpose, notice version, processors (GOOGLE only for
location: the model never receives a location), scope and times: never a coordinate.

``InMemoryConsentStore`` holds the rules; where entries live is a ``ConsentLedgerStore`` (in memory
by default and PER PROCESS, so a restart forgets consent and the user is asked again, which fails
closed; the Firestore ledger in ``app/stores`` is shared and durable, ACCESS-02 / #35).

``OperationLimiter`` is a small per-principal sliding window over consent endpoint calls.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from .domain import (
    ConsentProcessor,
    ConsentPurpose,
    ConsentRecord,
    ConsentScope,
    RuntimeConfig,
    evolve,
    history_active,
)
from .stores.memory import MemoryConsentLedger
from .stores.protocols import ConsentLedgerStore

# The notice text the client shows before it calls POST /v1/me/consents. A grant must name this
# exact version: a stale client cannot record consent to a notice the server no longer serves.
LOCATION_NOTICE_VERSION = "location-notice-1"
# History and saved places (decisions 24 and 25) are grantable only while ``history_active(config)``
# holds, and then only against the config's approved ``history_notice_version`` (never a constant
# here): with history off the grant is a typed DISABLED and a stored consent reads as inactive.
HISTORY_PURPOSES = frozenset({ConsentPurpose.HISTORY_STORE, ConsentPurpose.SAVED_PLACE})
SUPPORTED_PURPOSES = frozenset({ConsentPurpose.LOCATION_FOREGROUND}) | HISTORY_PURPOSES
# docs/data-and-history-design.md: the notice names Google, OpenRouter and the upstream model host.
_NOTICE_PROCESSORS = (
    ConsentProcessor.GOOGLE, ConsentProcessor.OPENROUTER, ConsentProcessor.UPSTREAM_MODEL_HOST)
PROCESSORS: dict[ConsentPurpose, tuple[ConsentProcessor, ...]] = {
    ConsentPurpose.LOCATION_FOREGROUND: (ConsentProcessor.GOOGLE,),
    ConsentPurpose.HISTORY_STORE: _NOTICE_PROCESSORS,
    ConsentPurpose.SAVED_PLACE: _NOTICE_PROCESSORS,
}
# Fixed notices only; the history purposes' notice comes from the runtime config.
NOTICE_VERSIONS: dict[ConsentPurpose, str] = {
    ConsentPurpose.LOCATION_FOREGROUND: LOCATION_NOTICE_VERSION,
}
_CONTEXT = {
    ConsentPurpose.LOCATION_FOREGROUND: "location.foreground",
    ConsentPurpose.HISTORY_STORE: "history.store",
    ConsentPurpose.SAVED_PLACE: "saved_place",
}


# The ledger only ever grows, so it is bounded: past this many raw entries a new grant is refused
# (typed, nothing written). Grants and withdrawals are rate limited per principal, so a user only
# gets here by cycling consent for days. Export compacts to at most ``MAX_EXPORT_CONSENTS``.
MAX_LEDGER_ENTRIES = 2_000
MAX_EXPORT_CONSENTS = 1_000

# Upper bound on principals an OperationLimiter tracks (least recently used are dropped). A dropped
# owner simply gets a fresh window. TTLs, IP buckets and App Check are tracked under ACCESS-02 (#35).
MAX_TRACKED_OWNERS = 50_000


class WrongNoticeVersion(Exception):
    """The grant named a notice version the server does not currently serve."""


class ConsentLedgerFull(Exception):
    """The owner's append-only ledger reached ``MAX_LEDGER_ENTRIES``: no further grant is written
    (a withdrawal always works). Bounds the ledger document and the export."""


class ConsentDisabled(Exception):
    """The purpose belongs to a feature that is switched off (history is not active)."""


@runtime_checkable
class ConsentStore(Protocol):
    def grant(self, owner_id: str, purpose: ConsentPurpose, notice_version: str) -> ConsentRecord: ...
    def withdraw(self, owner_id: str, purpose: ConsentPurpose) -> bool: ...
    def active(self, owner_id: str) -> tuple[ConsentRecord, ...]: ...
    def has_active(self, owner_id: str, purpose: ConsentPurpose) -> bool: ...
    def ledger(self, owner_id: str) -> tuple[ConsentRecord, ...]: ...


class InMemoryConsentStore:
    """The consent rules over a ``ConsentLedgerStore`` (in memory by default; Firestore in
    production, see ``app/stores``). Despite the name it is the rules, not the storage: every
    decision runs inside one atomic ledger ``update``, and the ledger can only append."""

    def __init__(
        self, clock: Callable[[], datetime] | None = None, *,
        ledger: ConsentLedgerStore | None = None,
        config: RuntimeConfig | Callable[[], RuntimeConfig | None] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ledger: ConsentLedgerStore = ledger if ledger is not None else MemoryConsentLedger()
        self._config = config

    def notice_version_for(self, purpose: ConsentPurpose) -> str | None:
        """The notice a grant must name now: fixed for location, the approved config notice for
        the history purposes (None while history is not active, so nothing matches)."""
        if purpose in HISTORY_PURPOSES:
            config = self._config() if callable(self._config) else self._config
            return config.history_notice_version if history_active(config) else None
        return NOTICE_VERSIONS.get(purpose)

    @staticmethod
    def _latest(records: tuple[ConsentRecord, ...]) -> dict[str, ConsentRecord]:
        latest: dict[str, ConsentRecord] = {}
        for record in records:
            latest[record.id] = record
        return latest

    def _active_for(
        self, records: tuple[ConsentRecord, ...], purpose: ConsentPurpose
    ) -> ConsentRecord | None:
        current = self.notice_version_for(purpose)
        for record in self._latest(records).values():
            if (record.purpose == purpose and record.withdrawn_at is None
                    and record.notice_version == current):
                return record
        return None

    def _open_for(
        self, records: tuple[ConsentRecord, ...], purpose: ConsentPurpose
    ) -> ConsentRecord | None:
        """A consent not yet withdrawn, whatever notice it named: withdrawal always works."""
        for record in self._latest(records).values():
            if record.purpose == purpose and record.withdrawn_at is None:
                return record
        return None

    def grant(self, owner_id: str, purpose: ConsentPurpose, notice_version: str) -> ConsentRecord:
        if purpose not in SUPPORTED_PURPOSES:
            raise WrongNoticeVersion()
        current = self.notice_version_for(purpose)
        if purpose in HISTORY_PURPOSES and current is None:
            raise ConsentDisabled()  # history is off: nothing is grantable, nothing is written
        if notice_version != current:
            raise WrongNoticeVersion()

        def decide(records: tuple[ConsentRecord, ...]) -> tuple[tuple[ConsentRecord, ...], ConsentRecord]:
            existing = self._active_for(records, purpose)
            if existing is not None:
                return (), existing
            if len(records) >= MAX_LEDGER_ENTRIES:
                raise ConsentLedgerFull()
            record = ConsentRecord(
                id=f"consent-{uuid.uuid4().hex[:24]}", owner_id=owner_id, purpose=purpose,
                notice_version=notice_version, processors=PROCESSORS[purpose],
                scope=ConsentScope(), granted_at=self._clock(), context=_CONTEXT[purpose])
            return (record,), record

        return self._ledger.update(owner_id, decide)

    def withdraw(self, owner_id: str, purpose: ConsentPurpose) -> bool:
        def decide(records: tuple[ConsentRecord, ...]) -> tuple[tuple[ConsentRecord, ...], bool]:
            existing = self._open_for(records, purpose)
            if existing is None:
                return (), False
            now = max(self._clock(), existing.granted_at)
            return (evolve(existing, withdrawn_at=now),), True

        return self._ledger.update(owner_id, decide)

    def active(self, owner_id: str) -> tuple[ConsentRecord, ...]:
        return tuple(
            r for r in self._latest(self._ledger.read(owner_id)).values()
            if r.withdrawn_at is None and r.notice_version == self.notice_version_for(r.purpose))

    def has_active(self, owner_id: str, purpose: ConsentPurpose) -> bool:
        return self._active_for(self._ledger.read(owner_id), purpose) is not None

    def ledger(self, owner_id: str) -> tuple[ConsentRecord, ...]:
        return self._ledger.read(owner_id)


def compact_for_export(records: tuple[ConsentRecord, ...]) -> tuple[ConsentRecord, ...]:
    """The ledger as the export shows it: ONE line per consent (its latest state, so a withdrawn
    grant/withdraw pair is a single withdrawn entry), at most ``MAX_EXPORT_CONSENTS``: every ACTIVE
    consent is kept and the oldest withdrawn ones are dropped first. Oldest first."""
    latest: dict[str, ConsentRecord] = {}
    for record in records:
        latest[record.id] = record
    rows = list(latest.values())
    if len(rows) > MAX_EXPORT_CONSENTS:
        withdrawn = sorted((r for r in rows if r.withdrawn_at is not None),
                           key=lambda r: r.withdrawn_at or r.granted_at)
        drop = {r.id for r in withdrawn[: len(rows) - MAX_EXPORT_CONSENTS]}
        rows = [r for r in rows if r.id not in drop]
    return tuple(sorted(rows, key=lambda r: r.granted_at))


# The same class under a name that says what it is.
LedgerConsentStore = InMemoryConsentStore


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
