"""Review finding 2: the Places daily-USD check reads only the caller's own ledger entries."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.costs import CostLedger
from app.places.pricing import DETAILS_LABEL, ROUTES_LABEL

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def test_tool_usd_for_sums_only_this_principals_window_and_labels() -> None:
    clock = {"now": NOW}
    ledger = CostLedger(lambda: clock["now"])
    ledger.record_tool("u1", "r1", DETAILS_LABEL, 1, 1)
    from app.costs import CostEntry
    ledger.record(CostEntry("tool", "u1", "r2", DETAILS_LABEL, tool_calls=1,
                            estimated_cost_usd=Decimal("0.01")))
    ledger.record(CostEntry("tool", "u1", "r3", ROUTES_LABEL, tool_calls=1,
                            estimated_cost_usd=Decimal("0.005")))
    ledger.record(CostEntry("tool", "u1", "r4", "other_tool", tool_calls=1,
                            estimated_cost_usd=Decimal("9")))
    ledger.record(CostEntry("tool", "u2", "r5", DETAILS_LABEL, tool_calls=1,
                            estimated_cost_usd=Decimal("1")))
    labels = (DETAILS_LABEL, ROUTES_LABEL)
    assert ledger.tool_usd_for("u1", labels, window=timedelta(hours=24)) == Decimal("0.015")
    clock["now"] = NOW + timedelta(hours=25)
    assert ledger.tool_usd_for("u1", labels, window=timedelta(hours=24)) == Decimal(0)


def test_a_paid_call_does_not_scan_other_principals(monkeypatch) -> None:
    from app.costs import CostEntry
    from app.places import PlacesPriceTable, build_places_service
    from maps_simulator import FakePlacesTransport, places_ok, synth_id, synthetic_place_details

    ledger = CostLedger(lambda: NOW)
    for i in range(10_000):
        ledger.record(CostEntry("tool", f"other-{i}", "r", DETAILS_LABEL, tool_calls=1,
                                estimated_cost_usd=Decimal("0.01")))
    table = PlacesPriceTable(
        version="t", reviewed=True, details_sku="d", routes_sku="r",
        details_usd_per_request=Decimal("0.01"), routes_usd_per_request=Decimal("0.005"))
    transport = FakePlacesTransport([places_ok(synthetic_place_details(1))])
    service = build_places_service(lambda: "k", ledger=ledger, price_table=table,
                                   transport=transport, now=lambda: NOW)

    def boom(self):  # noqa: ANN001
        raise AssertionError("the whole ledger was scanned")

    monkeypatch.setattr(CostLedger, "entries", boom)
    assert service.place_details("me", [synth_id(1)])
    assert len(transport.requests) == 1
