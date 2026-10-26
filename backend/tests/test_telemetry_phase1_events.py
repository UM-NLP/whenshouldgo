"""Item 3: typed telemetry events for the upcoming features: enums and counts only."""
from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from app import telemetry as t
from app import domain as d
from app.domain import PrincipalKind

TID = "0123456789abcdef0123456789abcdef"
NEW = {
    "PlaceDetailsEvent": dict(place_count=2, outcome=t.ToolOutcome.OK, cache_hits=1, latency_ms=5,
                              billable_units=1),
    "RouteCheckEvent": dict(mode=d.RouteMode.DRIVE, outcome=t.ToolOutcome.OK, latency_ms=5, billable_units=1),
    "ActionProposed": dict(effect=d.PendingActionEffect.SAVE_PLACE),
    "ActionConfirmed": dict(effect=d.PendingActionEffect.SAVE_PLACE, replay=False),
    "ActionRejected": dict(effect=d.PendingActionEffect.SAVE_PLACE, reason=t.ActionRejectReason.EXPIRED),
    "TripActivated": dict(principal_kind=PrincipalKind.GUEST),
    "WorkspaceEdit": dict(upserts=1, removals=0, outcome=t.EditOutcome.APPLIED),
    "OutcomeCheckinEvent": dict(outcome=d.OutcomeValue.WENT, has_note=True),
    "AccountLinkStarted": dict(provider=d.LinkProvider.GOOGLE),
    "AccountLinkCompleted": dict(provider=d.LinkProvider.GOOGLE, outcome=t.LinkOutcome.LINKED),
    "ExportRequested": dict(principal_kind=PrincipalKind.REGISTERED_FREE),
    "DeleteRequested": dict(principal_kind=PrincipalKind.REGISTERED_FREE),
    "HistoryRecordWritten": dict(strength=d.OutingStrength.HANDOFF),
    "HistoryRecordDeleted": dict(deleted_count=3),
    "SavedPlaceSaved": dict(label_kind=d.SavedPlaceKind.HOME),
    "SavedPlaceDeleted": dict(deleted_count=1),
}
EXPECTED_TYPES = {
    "PLACE_DETAILS", "ROUTE_CHECK", "ACTION_PROPOSED", "ACTION_CONFIRMED", "ACTION_REJECTED",
    "TRIP_ACTIVATED", "WORKSPACE_EDIT", "OUTCOME_CHECKIN", "ACCOUNT_LINK_STARTED",
    "ACCOUNT_LINK_COMPLETED", "EXPORT_REQUESTED", "DELETE_REQUESTED", "HISTORY_RECORD_WRITTEN",
    "HISTORY_RECORD_DELETED", "SAVED_PLACE_SAVED", "SAVED_PLACE_DELETED",
}
CANARIES = ("Cafe Example", "43.6532", "canary.user@example.com", "my secret note")


def test_event_types_exist() -> None:
    assert EXPECTED_TYPES <= {e.value for e in t.EventType}


@pytest.mark.parametrize("name,fields", NEW.items())
def test_event_builds_dumps_and_rejects_free_text(name: str, fields: dict) -> None:
    cls = getattr(t, name)
    event = cls(trace_id=TID, **fields)
    dumped = json.dumps(event.model_dump(mode="json"))
    assert not any(c in dumped for c in CANARIES)
    for extra in ("note", "place_name", "label", "latitude", "place_id", "message"):
        with pytest.raises(ValidationError):
            cls(trace_id=TID, **fields, **{extra: "Cafe Example"})


@pytest.mark.parametrize("name,fields", NEW.items())
def test_event_goes_through_the_sink_and_log_guard(name: str, fields: dict, caplog) -> None:
    sink = t.InMemorySink()
    assert t.trace(getattr(t, name), sink, trace_id=TID, **fields) is True
    assert len(sink.events) == 1
    bad = {**fields, "outcome": "Cafe Example", "reason": "x y", "effect": "Cafe Example"}
    assert t.trace(getattr(t, name), sink, trace_id=TID, **bad) is False  # emit never raises
    assert len(sink.events) == 1
    caplog.set_level(logging.DEBUG)
    logging.getLogger("app.test").info("Cafe Example 43.6532,-79.3832")
    assert "Cafe Example" not in caplog.text


def test_every_new_class_is_registered_under_a_distinct_event_literal() -> None:
    seen = {getattr(t, n).model_fields["event"].default for n in NEW}
    assert len(seen) == len(NEW) and {e.value for e in seen} == EXPECTED_TYPES
