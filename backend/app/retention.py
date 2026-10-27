"""Bounded retention sweep over every Firestore collection that holds user data with ``expires_at``.

The Firestore TTL policy is the primary mechanism (a runbook TODO) and deletes late; this deletes
the same documents early. Reads already treat an expired document as absent, so a delete only
removes data that was never served. Counts only: no id, no document content leaves this module.

``saved_places`` and ``saved_place_asks`` carry no ``expires_at`` (a saved place lives until the
user deletes it or withdraws consent); position-bearing pending actions live in ``pending_actions``
and ARE swept. ``consent_ledgers``, ``quota_usage``, ``cost_totals`` and ``kill_switch`` hold no
expiring user content.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

# Most sensitive first (user text, labels and positions), then derived and replay state.
SWEPT_COLLECTIONS: tuple[str, ...] = (
    "history_records", "outcome_ledgers", "pending_actions", "display_cache",
    "plan_heads", "plan_versions", "idempotency", "edit_receipts", "account_link_intents",
    "workspaces",
)
# Public names of the counts the runtime already reported.
COUNT_NAMES = {"history_records": "history"}


def _sweep_one(client: Any, collection: str, now: datetime, limit: int | None) -> int:
    query = client.collection(collection).where("expires_at", "<=", now)
    if limit is not None:
        query = query.limit(limit)
    removed = 0
    for snap in query.stream():
        snap.reference.delete()
        removed += 1
    return removed


def sweep_firestore(client: Any, now: datetime, limit: int | None = None) -> dict[str, int]:
    """Delete expired documents, at most ``limit`` in total (None: no bound). The budget is first
    split evenly so one busy collection cannot starve the others, then the remainder is spent in
    order. Idempotent: a second run deletes only what expired since."""
    counts = {name: 0 for name in SWEPT_COLLECTIONS}
    if limit is None:
        for name in SWEPT_COLLECTIONS:
            counts[name] = _sweep_one(client, name, now, None)
    else:
        if limit < 1:
            raise ValueError("the purge limit must be at least 1")
        share = max(1, limit // len(SWEPT_COLLECTIONS))
        for name in SWEPT_COLLECTIONS:
            counts[name] = _sweep_one(client, name, now, min(share, limit - sum(counts.values())))
        for name in SWEPT_COLLECTIONS:
            left = limit - sum(counts.values())
            if left <= 0:
                break
            counts[name] += _sweep_one(client, name, now, left)
    return {COUNT_NAMES.get(name, name): value for name, value in counts.items()}
