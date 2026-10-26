"""Tool identifiers (kept apart so contracts and client share them without importing each other)."""
from __future__ import annotations

from enum import StrEnum


class ToolId(StrEnum):
    """The only Grounding Lite tools the server will call (documented tool names)."""

    SEARCH_PLACES = "search_places"
    LOOKUP_WEATHER = "lookup_weather"
    COMPUTE_ROUTES = "compute_routes"
    RESOLVE_NAMES = "resolve_names"
    RESOLVE_MAPS_URLS = "resolve_maps_urls"
