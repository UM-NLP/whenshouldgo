"""The user's own location, foreground only and with consent (owner decision 2026-10-17).

Containment is the whole design. A turn's coordinate pair exists in exactly three places: the
in-memory request that carried it, the one outbound ``search_places`` arguments dict sent to Google
(as a location bias), and the one outbound Routes origin of ``POST /v1/routes/check`` (rounded; the
route response is displayed and never stored). It is never given to the model, and never reaches a
log, a telemetry event, an exception text, a repr, the workspace, evidence, the cost ledger, the
replay record, the display cache or its key, a quota or limiter record, or a consent record.

The ONE durable exception (decisions 24 and 25, dark behind ``history_enabled``): a registered
user's explicit, confirmed SAVE_PLACE keeps the position, rounded to this precision, in the saved
place (``saved_places.py``), and in the pending-action record only until confirm, cancel or expiry.
That needs the separate SAVED_PLACE consent; withdrawing it deletes both. Nothing else is stored,
so withdrawing LOCATION_FOREGROUND has nothing to delete.

* ``LocationInput`` is the API model. It validates the range and finiteness, hides itself from
  repr and from validation errors, and ROUNDS to ``LOCATION_DECIMALS`` as soon as it is parsed,
  so the unrounded value is never kept on any model.
* ``RoundedLocation`` is what the orchestrator and pipeline carry: frozen, slotted, redacted repr.
* It is used only for a DISCOVERY proposal that ends with no usable area and for the origin of a
  route check, and only when the principal holds an active LOCATION_FOREGROUND consent (checked at
  the API, before any work).
  It is never inferred and never taken from headers or the IP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# About 100 m at the equator. A named constant, not a user setting.
LOCATION_DECIMALS = 3
# OWNER-TUNABLE PLACEHOLDER (not a product decision): the radius of the location bias for a
# "near me" search. The live-verified search_places schema accepts up to 50,000 m.
LOCATION_BIAS_RADIUS_METERS = 5000


@dataclass(frozen=True, slots=True, repr=False)
class RoundedLocation:
    latitude: float
    longitude: float

    def __repr__(self) -> str:
        return "RoundedLocation(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> Any:  # never pickle or copy coordinates into a trace
        raise TypeError("RoundedLocation cannot be serialized")

    def bias(self) -> dict[str, Any]:
        """The ``location_bias`` argument of search_places (snake_case, as accepted live)."""
        return {"circle": {"center": {"latitude": self.latitude, "longitude": self.longitude},
                           "radius_meters": LOCATION_BIAS_RADIUS_METERS}}


class LocationInput(BaseModel):
    """``{latitude, longitude}`` and nothing else (no accuracy, altitude or timestamp)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, strict=True)

    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)

    @field_validator("latitude", "longitude")
    @classmethod
    def _round(cls, value: float) -> float:
        return round(value, LOCATION_DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0

    def __repr__(self) -> str:
        return "LocationInput(<redacted>)"

    __str__ = __repr__

    def rounded(self) -> RoundedLocation:
        return RoundedLocation(self.latitude, self.longitude)
