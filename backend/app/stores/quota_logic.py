"""The quota state machine, as pure functions over one ``Usage`` record.

Both the in-memory store (under a lock) and the Firestore store (inside a transaction) call
these, so the two cannot drift: the atomicity comes from the store, the rules live here.
Every function first normalises the record (``prepare``): it starts or rolls the rolling window
and drops abandoned reservations.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .protocols import RESERVATION_TTL, QuotaLimits, ReserveOutcome, Usage


def copy_usage(usage: Usage) -> Usage:
    return Usage(
        successful_turns=usage.successful_turns, activated_trips=usage.activated_trips,
        committed_keys=set(usage.committed_keys), reserved_keys=set(usage.reserved_keys),
        reserved_at=dict(usage.reserved_at), activated_workspaces=set(usage.activated_workspaces),
        window_started_at=usage.window_started_at)


def prepare(usage: Usage, now: datetime, window: timedelta | None) -> Usage:
    """Mutate ``usage`` to its state at ``now``: a window that has run its course restarts at
    ``now`` with fresh counts (committed keys and counted trips are per window); reservations older
    than ``RESERVATION_TTL`` are dropped."""
    if window is not None:
        started = usage.window_started_at
        if started is None:
            usage.window_started_at = now
        elif now >= started + window:
            usage.successful_turns = 0
            usage.activated_trips = 0
            usage.committed_keys.clear()
            usage.activated_workspaces.clear()
            usage.window_started_at = now
    for key, at in list(usage.reserved_at.items()):
        if now >= at + RESERVATION_TTL:
            del usage.reserved_at[key]
            usage.reserved_keys.discard(key)
    return usage


def has_room(usage: Usage, limits: QuotaLimits) -> bool:
    return (
        usage.successful_turns + len(usage.reserved_keys) < limits.turn_limit
        and usage.activated_trips < limits.trip_limit
    )


def reserve(usage: Usage, key: str, limits: QuotaLimits, now: datetime) -> ReserveOutcome:
    if key in usage.committed_keys:
        return ReserveOutcome.DUPLICATE
    if key in usage.reserved_keys:
        return ReserveOutcome.IN_FLIGHT
    if not has_room(usage, limits):
        return ReserveOutcome.EXHAUSTED
    usage.reserved_keys.add(key)
    usage.reserved_at[key] = now
    return ReserveOutcome.RESERVED


def _drop_reservation(usage: Usage, key: str) -> None:
    usage.reserved_keys.discard(key)
    usage.reserved_at.pop(key, None)


def commit(usage: Usage, key: str) -> None:
    """Count a reserved key once. Committing a committed key is a no-op; committing a key that
    holds no reservation raises."""
    if key in usage.committed_keys:
        _drop_reservation(usage, key)
        return
    if key not in usage.reserved_keys:
        raise RuntimeError("commit without an active reservation")
    _drop_reservation(usage, key)
    usage.successful_turns += 1
    usage.committed_keys.add(key)


def release(usage: Usage, key: str) -> None:
    _drop_reservation(usage, key)


def commit_turn(usage: Usage, key: str, limits: QuotaLimits) -> None:
    if key in usage.committed_keys:
        return
    if not has_room(usage, limits):
        raise PermissionError("allowance exhausted")
    usage.successful_turns += 1
    usage.committed_keys.add(key)


def activate_trip(usage: Usage, workspace_id: str, limits: QuotaLimits) -> bool:
    """Count one trip activation per workspace. True when this call counted it; False when it
    was already counted (never an error). Raises when a NEW activation would pass the limit."""
    if workspace_id in usage.activated_workspaces:
        return False
    if usage.activated_trips >= limits.trip_limit:
        raise PermissionError("trip allowance exhausted")
    usage.activated_trips += 1
    usage.activated_workspaces.add(workspace_id)
    return True
