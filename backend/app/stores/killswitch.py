"""The shared kill switch: a ``KillSwitchProbe`` (read and trip) backed by the ``CostStore``.

State lives in the store, so every Cloud Run instance sees a trip at once. Each provider path
("discovery", "places") has its OWN state and reason, plus the whole-pilot "global" switch (the
spend cap) that stops every path: a Places failure never switches discovery off, and the reverse. It FAILS CLOSED: when
the store cannot be read the switch reads as tripped. ``reset`` is operator-only and is not part
of the probe the discovery client sees.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from .protocols import KILL_PATH_DISCOVERY, KILL_PATH_GLOBAL, CostStore, KillSwitchReason


class StoreKillSwitch:
    def __init__(
        self, store: CostStore, clock: Callable[[], datetime] | None = None,
        *, trip_reason: KillSwitchReason = KillSwitchReason.DISCOVERY,
        path: str = KILL_PATH_DISCOVERY,
    ) -> None:
        self._store = store
        self._path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reason = trip_reason
        self._local_trip = False  # a trip that could not be persisted still stops THIS process
        self._lock = threading.Lock()

    @property
    def tripped(self) -> bool:
        with self._lock:
            if self._local_trip:
                return True
        try:
            if self._store.kill_switch().tripped:  # the whole-pilot switch (spend cap, operator)
                return True
            if self._path != KILL_PATH_GLOBAL:  # this provider path's own switch
                return self._store.kill_switch(self._path).tripped
            return False
        except Exception:  # noqa: BLE001 - unreadable shared state fails closed
            return True

    def trip(self) -> None:
        try:
            self._store.trip_kill_switch(self._reason, self._clock(), self._path)
        except Exception:  # noqa: BLE001 - never raise into a failure path; stop locally instead
            with self._lock:
                self._local_trip = True

    def reset(self) -> None:
        self._store.reset_kill_switch(self._clock(), self._path)
        with self._lock:
            self._local_trip = False
