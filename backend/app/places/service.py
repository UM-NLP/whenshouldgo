"""The Places service: Place Details and route checks for the route handlers.

Display-only (decisions 17 and 23): nothing here touches a model, a workspace, Evidence, a store
or a replay record. Per call it enforces, in order, the kill switch, the per-request tool budget,
the per-user daily USD and call ceilings, then records the spend in the CostLedger (user-attributed
when the user's own input caused the failure or it succeeded; provider-attributed otherwise).
Without a reviewed price table, or without clients, the service is OFF: a typed 503 and no network.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypeVar

from .. import telemetry
from ..consent import OperationLimiter
from ..costs import Attribution, CostEntry, CostLedger
from ..discovery.client import KillSwitch, KillSwitchProbe
from ..domain import PlaceCard, RouteCheck, RouteMode, RuntimeConfig
from ..location import RoundedLocation
from .details import PlaceDetailsCache, PlaceDetailsClient, validate_place_ids
from .errors import (
    PlacesAccessDenied,
    PlacesBudgetExceeded,
    PlacesDisabled,
    PlacesError,
    PlacesNotConfigured,
    PlacesNotFound,
    PlacesRateLimited,
)
from .pricing import DETAILS_LABEL, ROUTES_LABEL, UNREVIEWED_PRICE_TABLE, PlacesPriceTable
from .render import render_place_card, render_route_check
from .routes_check import RouteCheckClient
from .transport import HttpxPlacesTransport, PlacesTransport

_T = TypeVar("_T")
DAILY = timedelta(hours=24)
ACCESS_DENIED_TRIP_THRESHOLD = 3
DEFAULT_RUN_TOOL_CALLS = 12
DEFAULT_BUDGET_SECONDS = 20


@dataclass(frozen=True)
class PlacesLimits:
    """OWNER-TUNABLE PLACEHOLDERS (not product decisions): per-user rolling 24 h caps. They bound
    paid calls the way the daily token ceiling bounds model calls; a shared Firestore ledger is
    required before more than one instance serves traffic (see costs.py)."""

    daily_calls_per_user: int = 60
    daily_usd_per_user: Decimal = Decimal("0.50")


class _RunBudget:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.used = 0


class PlacesService:
    def __init__(
        self,
        *,
        details: PlaceDetailsClient | None,
        routes: RouteCheckClient | None,
        prices: PlacesPriceTable = UNREVIEWED_PRICE_TABLE,
        ledger: CostLedger,
        config: RuntimeConfig | None = None,
        kill_switch: KillSwitchProbe | None = None,
        limits: PlacesLimits | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
        sink: telemetry.TraceSink | None = None,
    ) -> None:
        self._details = details
        self._routes = routes
        self._prices = prices
        self._ledger = ledger
        self._config = config
        self._kill = kill_switch if kill_switch is not None else KillSwitch()
        self._limits = limits or PlacesLimits()
        self._now = now
        self._clock = clock
        self._sink = sink
        self._cache = PlaceDetailsCache(now)
        self._daily = OperationLimiter(
            now, max_operations=self._limits.daily_calls_per_user, window=DAILY)
        self._streak_lock = threading.Lock()
        self._denied_streak = 0

    # -- state -------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """False until a REVIEWED price table and both clients are configured."""
        return self._prices.reviewed and self._details is not None and self._routes is not None

    @property
    def tripped(self) -> bool:
        return self._kill.tripped

    @property
    def walk_enabled(self) -> bool:
        return self._routes is not None and self._routes.walk_enabled

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise PlacesNotConfigured()  # before any network call
        if self._kill.tripped:
            raise PlacesDisabled()

    # -- public ------------------------------------------------------------------------------
    def place_details(self, uid: str, place_ids: object) -> tuple[PlaceCard, ...]:
        ids = validate_place_ids(place_ids)
        self._require_enabled()
        details = self._details
        assert details is not None
        started = self._clock()
        run = self._new_run()
        deadline = started + self._budget_seconds()
        cards: list[PlaceCard] = []
        hits = units = 0
        failure: PlacesError | None = None
        for place_id in ids:
            cached = self._cache.get(uid, place_id)
            if cached is not None:
                cards.append(render_place_card(cached))
                hits += 1
                continue
            try:
                parsed = self._paid_call(
                    uid, run, DETAILS_LABEL,
                    lambda pid=place_id: details.fetch(pid, max(0.1, deadline - self._clock())))
            except PlacesNotFound as exc:
                units += exc.calls
                continue
            except PlacesError as exc:
                units += exc.calls
                failure = exc
                break
            units += 1
            self._cache.put(uid, parsed)
            cards.append(render_place_card(parsed))
        if failure is None and not cards:
            failure = PlacesNotFound(calls=0)
        self._emit_details(len(ids), hits, failure, started, units)
        if failure is not None:
            raise failure
        return tuple(cards)

    def route_check(
        self, uid: str, place_id: str, mode: RouteMode, origin: RoundedLocation | None
    ) -> RouteCheck:
        self._require_enabled()
        routes = self._routes
        assert routes is not None
        failure: PlacesError | None = None
        result: RouteCheck | None = None
        started = self._clock()
        units = 0
        try:
            routes.check_request(place_id, mode, origin)  # no network, nothing reserved
            run = self._new_run()
            parsed = self._paid_call(
                uid, run, ROUTES_LABEL,
                lambda: routes.compute(place_id, mode, origin, self._budget_seconds()))
            units = 1
            result = render_route_check(place_id, mode, parsed, self._now())
        except PlacesError as exc:
            units = exc.calls
            failure = exc
        self._emit_route(mode, failure, started, units)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    # -- internals ---------------------------------------------------------------------------
    def _new_run(self) -> _RunBudget:
        cap = (self._config.token_ceilings.run_tool_calls if self._config is not None
               else DEFAULT_RUN_TOOL_CALLS)
        return _RunBudget(cap)

    def _budget_seconds(self) -> float:
        return float(self._config.token_ceilings.interactive_budget_seconds
                     if self._config is not None else DEFAULT_BUDGET_SECONDS)

    def _spent_today(self, uid: str) -> Decimal:
        cutoff = self._now() - DAILY
        total = Decimal(0)
        for entry in self._ledger.entries():
            if (entry.principal_id == uid and entry.kind == "tool"
                    and entry.label in (DETAILS_LABEL, ROUTES_LABEL)
                    and entry.at is not None and entry.at > cutoff):
                total += entry.estimated_cost_usd or Decimal(0)
        return total

    def _paid_call(self, uid: str, run: _RunBudget, label: str, call: Callable[[], _T]) -> _T:
        if self._kill.tripped:
            raise PlacesDisabled()
        if run.used >= run.max_calls:
            raise PlacesBudgetExceeded()
        price = self._prices.price_for(label)
        if self._spent_today(uid) + price > self._limits.daily_usd_per_user:
            raise PlacesRateLimited(retry_after=3600)
        wait = self._daily.check(uid)
        if wait is not None:
            raise PlacesRateLimited(retry_after=max(1, wait))
        run.used += 1
        failure: PlacesError | None = None
        result: _T | None = None
        try:
            result = call()
        except PlacesError as exc:
            failure = exc
        if failure is not None:
            if failure.calls:
                self._record(uid, label, price, Attribution.PROVIDER if failure.provider_side
                             else Attribution.USER)
            self._note_failure(failure)
            raise failure
        self._record(uid, label, price, Attribution.USER)
        with self._streak_lock:
            self._denied_streak = 0
        assert result is not None
        return result

    def _record(self, uid: str, label: str, price: Decimal, attribution: Attribution) -> None:
        # Ids, enums and counts only: never a name, address, coordinate, route or URL.
        self._ledger.record(CostEntry(
            "tool", uid, f"run-{uuid.uuid4().hex[:16]}", label, tool_calls=1, billable_units=1,
            estimated_cost_usd=price, attribution=attribution))

    def _note_failure(self, failure: PlacesError) -> None:
        if not isinstance(failure, PlacesAccessDenied):
            return
        with self._streak_lock:
            self._denied_streak += 1
            trip = failure.trips_at_once or self._denied_streak >= ACCESS_DENIED_TRIP_THRESHOLD
        if trip:
            self._kill.trip()

    def _version(self) -> str | None:
        return self._config.version if self._config is not None else None

    def _latency(self, started: float) -> int:
        return min(86_400_000, int(max(0.0, self._clock() - started) * 1000))

    def _emit_details(self, count: int, hits: int, failure: PlacesError | None, started: float,
                      units: int) -> None:
        telemetry.trace(
            telemetry.PlaceDetailsEvent, self._sink, trace_id=telemetry.current_trace_id(),
            runtime_config_version=self._version(), place_count=count, cache_hits=hits,
            outcome=failure.outcome if failure else telemetry.ToolOutcome.OK,
            latency_ms=self._latency(started), billable_units=units)

    def _emit_route(self, mode: RouteMode, failure: PlacesError | None, started: float,
                    units: int) -> None:
        telemetry.trace(
            telemetry.RouteCheckEvent, self._sink, trace_id=telemetry.current_trace_id(),
            runtime_config_version=self._version(), mode=mode,
            outcome=failure.outcome if failure else telemetry.ToolOutcome.OK,
            latency_ms=self._latency(started), billable_units=units)


def build_places_service(
    secret_provider: Callable[[], str] | None,
    *,
    ledger: CostLedger,
    price_table: PlacesPriceTable = UNREVIEWED_PRICE_TABLE,
    config: RuntimeConfig | None = None,
    kill_switch: KillSwitchProbe | None = None,
    transport: PlacesTransport | None = None,
    now: Callable[[], datetime] | None = None,
    sink: telemetry.TraceSink | None = None,
    walk_enabled: bool = False,
    limits: PlacesLimits | None = None,
) -> PlacesService:
    """The composition root calls this. Without a reviewed ``price_table`` or without a
    ``secret_provider`` the service is built OFF: no client, no transport, no network, ever."""
    clock_now = now or (lambda: datetime.now(UTC))
    details = routes = None
    if price_table.reviewed and secret_provider is not None:
        wire = transport if transport is not None else HttpxPlacesTransport()
        details = PlaceDetailsClient(wire, secret_provider, now=clock_now)
        routes = RouteCheckClient(wire, secret_provider, walk_enabled=walk_enabled)
    return PlacesService(
        details=details, routes=routes, prices=price_table, ledger=ledger, config=config,
        kill_switch=kill_switch, limits=limits, now=clock_now, sink=sink)
