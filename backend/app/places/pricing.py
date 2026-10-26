"""SKU price table for Places Details and Routes (display-only features).

The table is UNREVIEWED until the owner enters and reviews real prices: with ``reviewed`` False the
service is OFF (typed 503, no network call), like the model gateway without a reviewed table.
Nothing here is a product decision; every number is an owner-tunable placeholder.

SKU ASSUMPTIONS (owner review needed, checked against Google's SKU grouping as understood on
2026-10-26, not against the billing console):
* Place Details is billed by the HIGHEST SKU among the requested fields. ``id``, ``displayName`` and
  ``formattedAddress`` fall in a lower tier; ``currentOpeningHours`` and ``rating`` fall in the
  Enterprise tier, so the minimal mask below is billed as Place Details Enterprise. Dropping
  open-now and rating would make it cheaper; that is a product call.
* computeRoutes without traffic awareness, route modifiers or a polyline is the base (Essentials)
  Routes SKU. TRAFFIC_AWARE is NOT used (it changes the SKU). Walking is a beta travel mode.
* Free monthly allowances and per-SKU caps are not modelled.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from ..domain import Frozen, Ident

UsdAmount = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=8)]

DETAILS_LABEL = "place_details"  # CostLedger tool label (an enum-like constant, never free text)
ROUTES_LABEL = "route_check"


class PlacesPriceTable(Frozen):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    reviewed: bool  # False until the owner has entered and reviewed real prices
    details_sku: Ident
    routes_sku: Ident
    details_usd_per_request: UsdAmount
    routes_usd_per_request: UsdAmount

    def price_for(self, label: str) -> Decimal:
        return self.details_usd_per_request if label == DETAILS_LABEL else self.routes_usd_per_request


UNREVIEWED_PRICE_TABLE = PlacesPriceTable(
    version="unreviewed-0", reviewed=False,
    details_sku="places_details_enterprise", routes_sku="routes_compute_essentials",
    details_usd_per_request=Decimal("0"), routes_usd_per_request=Decimal("0"))
