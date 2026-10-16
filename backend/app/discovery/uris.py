"""Field-pinned link validators.

Every link field the tools return has ONE rule in ONE table (``LINK_RULES``): an exact host and an
exact path pattern, plus a query rule where the field has a query. The shapes come from a live
capture (verified live 2026-10-15); nothing is matched by prefix and no field accepts another
field's shape. Hosts: ``www.google.com`` for every link and ``maps.gstatic.com`` for the weather
icon only.

Protections applied to EVERY field before the shape test: https only, the exact lowercase host
(no userinfo, port, trailing dot, other case), no fragment, no control, whitespace, non-ASCII or
backslash characters, no ``;``, bounded length, percent-escapes that are well formed and decode
to valid UTF-8, no encoded slash, backslash, NUL, control or second-level ``%``, and no ``.`` or
``..`` segment, raw or after one level of percent-decoding. No open-redirector path (``/url``,
``/imgres``, ``/maps/..``) can match a full-path pattern.

TODO(verify-live): ``www.google.com/search?q=Weather+for+...`` is pinned to the English prefix
seen live; other ``language_code`` values may localise it. The grammar of ``place_ids`` in a save
link beyond one id (comma-separated here, up to 20) is a guess. Whether ``iconBaseUri`` needs its
own attribution is open; we validate its shape and then drop it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

MAX_URI_CHARS = 2_048
LINK_HOSTS = frozenset({"www.google.com", "maps.gstatic.com"})

# The identifier inside ``data=``: ``1s<id chars>:0x<hex>``. It is NOT the place id (live captures
# show a different, shorter identifier); it is shared by all of one place's links.
_TOKEN = r"1s[A-Za-z0-9_-]{8,48}:0x[0-9a-f]{8,24}"
_SEGMENT = r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2}){1,400}"
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")
_WEATHER_QUERY = re.compile(r"^q=(?:[A-Za-z0-9+._~-]|%[0-9A-Fa-f]{2}){1,500}$")
_SAVE_QUERY = re.compile(r"^[A-Za-z0-9_=&,-]{1,6000}$")
_WEATHER_PREFIX = "Weather for "
_MAX_SAVE_IDS = 20  # TODO(verify-live): comma-separated ids are an assumption


class LinkField(StrEnum):
    PLACE_URL = "PLACE_URL"
    PLACE_ATTRIBUTION_URL = "PLACE_ATTRIBUTION_URL"
    DIRECTIONS_URL = "DIRECTIONS_URL"
    PHOTOS_URL = "PHOTOS_URL"
    REVIEWS_URL = "REVIEWS_URL"
    WRITE_REVIEW_URL = "WRITE_REVIEW_URL"
    ROUTE_ATTRIBUTION_URL = "ROUTE_ATTRIBUTION_URL"
    WEATHER_ATTRIBUTION_URL = "WEATHER_ATTRIBUTION_URL"
    SAVE_TO_MAPS_URL = "SAVE_TO_MAPS_URL"
    WEATHER_ICON_URI = "WEATHER_ICON_URI"


@dataclass(frozen=True)
class LinkRule:
    host: str
    path: re.Pattern[str]
    query: str  # "none" | "weather" | "save"


def _rule(path: str, host: str = "www.google.com", query: str = "none") -> LinkRule:
    return LinkRule(host=host, path=re.compile(path), query=query)


_PLACE_PATH = rf"/maps/place//data=!4m2!3m1!{_TOKEN}"
# One table: the field each pattern applies to. Narrow on purpose; widen only from a new capture.
LINK_RULES: dict[LinkField, LinkRule] = {
    LinkField.PLACE_URL: _rule(_PLACE_PATH),
    LinkField.PLACE_ATTRIBUTION_URL: _rule(_PLACE_PATH),  # same shape as placeUrl (seen live)
    LinkField.DIRECTIONS_URL: _rule(rf"/maps/dir//''/data=!4m7!4m6!1m1!4e2!1m2!1m1!{_TOKEN}!3e0"),
    LinkField.PHOTOS_URL: _rule(rf"/maps/place//data=!4m3!3m2!{_TOKEN}!10e5"),
    LinkField.REVIEWS_URL: _rule(rf"/maps/place//data=!4m4!3m3!{_TOKEN}!9m1!1b1"),
    LinkField.WRITE_REVIEW_URL: _rule(rf"/maps/place//data=!4m3!3m2!{_TOKEN}!12e1"),
    LinkField.ROUTE_ATTRIBUTION_URL: _rule(rf"/maps/dir/{_SEGMENT}/{_SEGMENT}"),  # echoes user text: sensitive
    LinkField.WEATHER_ATTRIBUTION_URL: _rule("/search", query="weather"),  # echoes the location
    LinkField.SAVE_TO_MAPS_URL: _rule("/maps/@", query="save"),
    LinkField.WEATHER_ICON_URI: _rule(r"/weather/v1/[A-Za-z0-9_]{1,64}", host="maps.gstatic.com"),
}

_FORBIDDEN_DECODED = frozenset("/\\%")


def _basic(uri: object) -> str:
    if not isinstance(uri, str) or not 0 < len(uri) <= MAX_URI_CHARS:
        raise ValueError("uri is not a bounded string")
    if not uri.isascii() or not uri.isprintable() or any(ch.isspace() for ch in uri):
        raise ValueError("uri is not plain printable ASCII")
    if set(uri) & set("\\;#"):
        raise ValueError("uri contains a forbidden character")
    return uri


def _decode(text: str, *, plus: bool = False) -> str:
    """Strictly percent-decode one component: every escape well formed, valid UTF-8, and nothing
    dangerous underneath (slash, backslash, a second-level percent, NUL, controls, format chars)."""
    if "%" in re.sub(r"%[0-9A-Fa-f]{2}", "", text):
        raise ValueError("malformed percent escape")
    decoded = unquote_to_bytes(text.replace("+", " ") if plus else text).decode("utf-8")
    if _FORBIDDEN_DECODED & set(decoded) or any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in decoded):
        raise ValueError("decoded text contains a forbidden character")
    return decoded


def _check_path(path: str) -> None:
    for segment in path.split("/"):
        decoded = _decode(segment)  # per segment, so an encoded slash is caught
        if segment in (".", "..") or decoded in (".", ".."):
            raise ValueError("dot segment")


def _check_query(kind: str, query: str) -> None:
    if kind == "none":
        if query:
            raise ValueError("this field has no query")
        return
    if kind == "weather":
        if not _WEATHER_QUERY.fullmatch(query):
            raise ValueError("weather query shape")
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, errors="strict")
        if len(pairs) != 1 or pairs[0][0] != "q" or not pairs[0][1].startswith(_WEATHER_PREFIX):
            raise ValueError("weather query must be exactly one q=Weather for ...")
        value = _decode(query[2:], plus=True)
        if set(value) & set("&=?#"):
            raise ValueError("weather query value")
        return
    # save link: exactly api=1, map_action=shortlist, place_ids=<id>[,<id>...]
    if not _SAVE_QUERY.fullmatch(query):
        raise ValueError("save query shape")
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, errors="strict")
    keys = [k for k, _ in pairs]
    values = dict(pairs)
    if sorted(keys) != ["api", "map_action", "place_ids"] or values["api"] != "1" or values["map_action"] != "shortlist":
        raise ValueError("save link keys")
    ids = values["place_ids"].split(",")
    if not 1 <= len(ids) <= _MAX_SAVE_IDS or not all(_PLACE_ID.fullmatch(i) for i in ids):
        raise ValueError("save link place_ids")


def check_link(field: LinkField, uri: str) -> None:
    """Raise ValueError unless ``uri`` is exactly the shape ``field`` is documented to have."""
    rule = LINK_RULES[field]
    parts = urlsplit(_basic(uri))
    if parts.scheme != "https" or not parts.netloc or parts.netloc != (parts.hostname or ""):
        raise ValueError("uri must be https without userinfo, a port or an uppercase host")
    if parts.hostname != rule.host or parts.fragment:
        raise ValueError("uri host or fragment is not allowed for this field")
    _check_path(parts.path)
    if not rule.path.fullmatch(parts.path):
        raise ValueError("uri path is not this field's shape")
    _check_query(rule.query, parts.query)


def data_token(uri: str) -> str | None:
    """The ``1s...:0x...`` token inside ``data=`` of a place link, else None. Shared by all links
    of one place (an integrity check); it is not the place id."""
    try:
        match = re.search(rf"!({_TOKEN})(?:!|$)", urlsplit(uri).path)
    except ValueError:
        return None
    return match.group(1) if match else None


# --- INPUT urls (what we may send to resolve_maps_urls): narrow, independent of the output table ---
_INPUT_SEGMENT = re.compile(r"^[A-Za-z0-9_@,.!=+:*()'~-]{1,200}$")
_INPUT_TOKEN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def check_input_maps_url(uri: str) -> None:
    """An INPUT url for resolve_maps_urls: the three documented single-place forms
    (maps.app.goo.gl/<token>, www.google.com/maps/place/..., maps.google.com/maps...)."""
    parts = urlsplit(_basic(uri))
    if "%" in uri or parts.scheme != "https" or not parts.netloc or parts.netloc != (parts.hostname or ""):
        raise ValueError("input url must be plain https")
    if parts.fragment or parts.query or not parts.path.startswith("/"):
        raise ValueError("input url has a query, fragment or no path")
    segments = parts.path[1:].split("/")
    if any(s in ("", ".", "..") or not _INPUT_SEGMENT.fullmatch(s) for s in segments):
        raise ValueError("input url path has an empty, dot or unsafe segment")
    host = parts.hostname
    if host == "maps.app.goo.gl":
        ok = len(segments) == 1 and bool(_INPUT_TOKEN.fullmatch(segments[0]))
    elif host in ("www.google.com", "google.com"):
        ok = len(segments) >= 3 and segments[:2] == ["maps", "place"]
    elif host == "maps.google.com":
        ok = segments[0] == "maps"
    else:
        ok = False
    if not ok:
        raise ValueError("input url is not a resolvable single-place Maps url")
