"""Server rendering of Places Details and Routes results into the frozen display models.

The server builds the cards: provider text passes through ``parse_place`` (plain-text policy,
length caps) and lands in ``display_only`` fields; the required Google attribution is attached here,
by the server. Nothing is stored. The wording helper makes no precision promise (decision 15):
"about", whole minutes, 100 m steps.
"""
from __future__ import annotations

from datetime import datetime

from ..domain import PlaceCard, RouteCheck, RouteMode
from .details import ParsedPlace
from .routes_check import ParsedRoute

ATTRIBUTION = "Google Maps"  # translate="no": the name is never translated or localized
BETA_WALK_WARNING = "Walking directions are in beta and may be inaccurate. Check the route yourself."


def render_place_card(parsed: ParsedPlace) -> PlaceCard:
    return PlaceCard(
        place_id=parsed.place_id, display_name=parsed.display_name,
        formatted_address=parsed.formatted_address, open_now=parsed.open_now,
        rating=parsed.rating, attributions=(ATTRIBUTION,), fetched_at=parsed.fetched_at)


def render_route_check(place_id: str, mode: RouteMode, parsed: ParsedRoute, fetched_at: datetime) -> RouteCheck:
    return RouteCheck(
        place_id=place_id, mode=mode, duration_minutes=parsed.duration_minutes,
        distance_meters=parsed.distance_meters, beta_warning=mode == RouteMode.WALK,
        attributions=(ATTRIBUTION,), fetched_at=fetched_at)


def describe_route(route: RouteCheck) -> str:
    """Server wording for a route check (for clients and tests). Always 'about'; never exact."""
    by = "on foot" if route.mode == RouteMode.WALK else "by car"
    km = route.distance_meters / 1000
    text = f"About {route.duration_minutes} min {by}, about {km:.1f} km, without live traffic."
    return f"{text} {BETA_WALK_WARNING}" if route.beta_warning else text
