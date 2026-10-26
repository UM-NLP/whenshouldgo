"""The shared kill switch: a ``KillSwitchProbe`` (read and trip) backed by the ``CostStore``.

State lives in the store, so every Cloud Run instance sees a trip at once. It FAILS CLOSED: when
the store cannot be read the switch reads as tripped. ``reset`` is operator-only and is not part
of the probe the discovery client sees.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from .protocols import CostStore, KillSwitchReason


class StoreKillSwitch:
    def __init__(
        self, store: CostStore, clock: Callable[[], datetime] | None = None,
        *, trip_reason: KillSwitchReason = KillSwitchReason.DISCOVERY,
    ) -> None:
        self._store = store
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
            return self._store.kill_switch().tripped
        except Exception:  # noqa: BLE001 - unreadable shared state fails closed
            return True

    def trip(self) -> None:
        try:
            self._store.trip_kill_switch(self._reason, self._clock())
        except Exception:  # noqa: BLE001 - never raise into a failure path; stop locally instead
            with self._lock:
                self._local_trip = True

    def reset(self) -> None:
        self._store.reset_kill_switch(self._clock())
        with self._lock:
            self._local_trip = False
