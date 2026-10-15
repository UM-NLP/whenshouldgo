"""Strict link allowlists. Every host has a pinned PATH SHAPE; nothing is matched by prefix.

Rejected before any shape test: non-ASCII, whitespace and controls, backslash, ``%`` (so no
encoded dot-segments), ``;``, fragments, userinfo, ports, uppercase or trailing-dot hosts,
dot and empty path segments. No redirector is ever allowed (``/url``, ``/imgres``, ``/amp``).

TODO(verify-live): the real link shapes Grounding Lite returns. These are deliberately
narrow so an unexpected real shape fails closed until a live capture widens them.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

MAX_URI_CHARS = 2_048
MAPS_LINK_HOSTS = frozenset({"maps.app.goo.gl", "www.google.com", "google.com"})
_SEGMENT = re.compile(r"^[A-Za-z0-9_@,.!=+:*()'~-]{1,200}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_QUERY = re.compile(r"^[A-Za-z0-9_=&:.,+-]{0,512}$")
_FORBIDDEN_CHARS = frozenset("\x5c%;#")


def _split(uri: object) -> tuple[str, list[str]]:
    if not isinstance(uri, str) or not 0 < len(uri) <= MAX_URI_CHARS:
        raise ValueError("uri is not a bounded string")
    if not uri.isascii() or not uri.isprintable() or any(ch.isspace() for ch in uri):
        raise ValueError("uri is not plain printable ASCII")
    if _FORBIDDEN_CHARS & set(uri):
        raise ValueError("uri contains a forbidden character")
    parts = urlsplit(uri)
    if parts.scheme != "https" or not parts.netloc or parts.netloc != (parts.hostname or ""):
        raise ValueError("uri must be https without userinfo, a port or an uppercase host")
    if parts.fragment or not parts.path.startswith("/"):
        raise ValueError("uri has a fragment or no absolute path")
    segments = [] if parts.path == "/" else parts.path[1:].split("/")
    for segment in segments:
        if segment in ("", ".", "..") or not _SEGMENT.fullmatch(segment):
            raise ValueError("uri path has an empty, dot or unsafe segment")
    if parts.query and (
        not _QUERY.fullmatch(parts.query) or "http" in parts.query.lower() or "//" in parts.query
    ):
        raise ValueError("uri query is not allowed")
    return parts.hostname or "", segments


def _maps_shape(host: str, segments: list[str]) -> bool:
    if host == "maps.app.goo.gl":
        return len(segments) == 1 and bool(_TOKEN.fullmatch(segments[0]))
    if host in ("www.google.com", "google.com"):
        return bool(segments) and segments[0] == "maps"
    return False


def check_maps_link(uri: str) -> None:
    """A place link (``placeUrl``) or save link: only the pinned Maps shapes."""
    host, segments = _split(uri)
    if host not in MAPS_LINK_HOSTS or not _maps_shape(host, segments):
        raise ValueError("uri is not a Google Maps link shape")


def check_attribution_url(uri: str) -> None:
    """An attribution url: Maps shapes, or www.google.com/search (documented as possible)."""
    host, segments = _split(uri)
    if host == "www.google.com" and segments == ["search"]:
        return
    if host not in MAPS_LINK_HOSTS or not _maps_shape(host, segments):
        raise ValueError("uri is not an attribution link shape")


def check_input_maps_url(uri: str) -> None:
    """An INPUT url for resolve_maps_urls: the three documented single-place forms."""
    host, segments = _split(uri)
    if host == "maps.app.goo.gl":
        ok = _maps_shape(host, segments)
    elif host in ("www.google.com", "google.com"):
        ok = len(segments) >= 3 and segments[:2] == ["maps", "place"]
    elif host == "maps.google.com":
        ok = bool(segments) and segments[0] == "maps"
    else:
        ok = False
    if not ok:
        raise ValueError("uri is not a resolvable single-place Maps url")
