"""Grounded discovery contracts (docs/data-and-history-design.md, "Discovery and evidence
contracts"; docs/legal/google-maps-llm-policy-summary.md sections 4-6; Google's Grounding Lite
search_places reference and attribution page, retrieved 2026-10-15).

``GroundedAnswer`` carries the summary exactly as the tool returned it (RAW: escaping happens
only at an HTML sink, see ``render.py``) and, per place, only: the place id, the attribution
title (shown exactly as provided), the attribution url and the Maps ``placeUrl``. Coordinates
and every other link in the payload are parsed and DISCARDED. There is no server-authored text
field and no typed field for coordinates, ratings, hours, categories, images or previews.

Two hashes anchor the answer to the run ledger written by the client:
``content_hash`` (the summary string the user sees) and ``display_hash`` (the summary plus every
displayed place field, in order). The summary appears in no repr or validation message and is
stored nowhere except the in-memory display cache (DISPLAY_CACHE, at most 30 days).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from ..domain import (
    MAX_MAPS_CACHE,
    ContentClass,
    Evidence,
    EvidenceKind,
    EvidenceStorageClass,
    Frozen,
    Ident,
    PlaceId,
    RuntimeTTLs,
)
from ._mint import IssuedToken
from .errors import DiscoveryError
from .ids import ToolId
from .text import has_lone_surrogate, short_field_problem, summary_problem
from .uris import LinkField, check_link, data_token

GROUNDED_TEXT_MAX_CHARS = 8_000
MAX_PLACES = 20
MAX_TITLE_CHARS = 200
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,300}$")  # same pattern as domain.PlaceId
_CITATION = re.compile(r"\[(\d{1,4}(?:\s*,\s*\d{1,4})*)\]")


class GroundFrozen(Frozen):
    """Frozen contract whose validation errors never echo input (it may be Maps content)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


# --- hashing (surrogate-safe: a lone surrogate must never raise UnicodeEncodeError) -----------
def _digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def canonical_json(value: object) -> str:
    """Deterministic JSON: sorted keys, ASCII only (surrogate-safe), no NaN or Infinity."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def payload_digest(payload: object) -> str:
    """Hash of a whole tool payload: pass the payload, or its canonical JSON string."""
    text = payload if isinstance(payload, str) else canonical_json(payload)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def content_hash(text: str) -> str:
    """Hash of the exact summary string the user sees."""
    return _digest(text)


PlaceFields = tuple[str, str, str, str]  # (place_id, attribution_title, attribution_url, place_url)
# The other link fields of one place, kept only long enough to validate and cross-check them.
OtherLinks = tuple[tuple[LinkField, str], ...]


def display_hash(text: str, places: Iterable[PlaceFields]) -> str:
    """Hash of everything displayed: the summary plus each place's four fields, in order."""
    return _digest([text, [list(p) for p in places]])


def citation_indexes(text: str) -> set[int]:
    found: set[int] = set()
    for group in _CITATION.findall(text):
        found.update(int(part) for part in group.split(","))
    return found


def cited_flags(text: str, count: int) -> list[bool]:
    """Best effort: which places a bracketed index in the summary points at. Only indexes that
    fall inside ``range(count)`` matter; years like [2019] and anything else out of range are
    ignored, never a reason to reject. TODO(verify-live): the docs say citations are zero-based
    ("[0]"); a different real convention would flag the wrong place but cannot fail validation."""
    cited = citation_indexes(text)
    return [i in cited for i in range(count)]


class GroundedPlace(GroundFrozen):
    place_id: PlaceId
    attribution_title: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TITLE_CHARS)] = Field(repr=False)
    attribution_url: str = Field(repr=False)
    place_url: str = Field(repr=False)
    tool_call_id: Ident  # must equal the answer's: every place comes from the summary's call
    cited: bool  # True when the summary cites this place as [index]

    @model_validator(mode="after")
    def _safe(self) -> GroundedPlace:
        if short_field_problem(self.attribution_title):
            raise ValueError("hidden or control characters in the attribution title")
        check_link(LinkField.PLACE_ATTRIBUTION_URL, self.attribution_url)
        check_link(LinkField.PLACE_URL, self.place_url)
        if data_token(self.attribution_url) != data_token(self.place_url):
            raise ValueError("the attribution link and the place link are for different places")
        return self


class GroundedAnswer(GroundFrozen):
    schema_version: int = Field(default=1, ge=1)
    kind: Literal["grounded_answer"] = "grounded_answer"
    text: Annotated[str, StringConstraints(min_length=1, max_length=GROUNDED_TEXT_MAX_CHARS)] = Field(repr=False)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    display_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    places: tuple[GroundedPlace, ...] = Field(min_length=1, max_length=MAX_PLACES)
    tool_call_id: Ident
    run_id: Ident
    retrieved_at: AwareDatetime
    cache_expires_at: AwareDatetime
    content_class: Literal[ContentClass.MAPS_GROUNDED] = ContentClass.MAPS_GROUNDED
    storage_class: Literal[EvidenceStorageClass.DISPLAY_CACHE] = EvidenceStorageClass.DISPLAY_CACHE

    @model_validator(mode="after")
    def _consistent(self) -> GroundedAnswer:
        if summary_problem(self.text):
            raise ValueError("hidden or control characters in the summary")
        if content_hash(self.text) != self.content_hash:
            raise ValueError("content_hash does not match the text")
        fields = [(p.place_id, p.attribution_title, p.attribution_url, p.place_url) for p in self.places]
        if display_hash(self.text, fields) != self.display_hash:
            raise ValueError("display_hash does not match the displayed fields")
        ids = [p.place_id for p in self.places]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate place_id")
        if any(p.tool_call_id != self.tool_call_id for p in self.places):
            raise ValueError("every place must come from the summary's tool call")
        if [p.cited for p in self.places] != cited_flags(self.text, len(self.places)):
            raise ValueError("cited flags do not match the summary")
        if not self.retrieved_at < self.cache_expires_at <= self.retrieved_at + MAX_MAPS_CACHE:
            raise ValueError("cache_expires_at must be within (retrieved_at, retrieved_at + 30 days]")
        return self

    @classmethod
    def build(
        cls, *, text: str, places: tuple[GroundedPlace, ...], tool_call_id: str, run_id: str,
        retrieved_at: datetime, ttls: RuntimeTTLs,
    ) -> GroundedAnswer:
        return cls(
            text=text, content_hash=content_hash(text),
            display_hash=display_hash(text, [(p.place_id, p.attribution_title, p.attribution_url,
                                              p.place_url) for p in places]),
            places=places, tool_call_id=tool_call_id, run_id=run_id,
            retrieved_at=retrieved_at,
            cache_expires_at=retrieved_at + timedelta(hours=ttls.grounded_cache_hours),
        )


class GroundedParseError(DiscoveryError):
    default_message = "tool payload is not a grounded answer"


# --- the documented search_places payload (and nothing else) --------------------------------------
# Verified live 2026-10-15: output keys are camelCase (googleMapsLinks, placeUrl, ...), the payload
# has exactly {places, summary}, and each place has {place, id, location, googleMapsLinks,
# attribution}. The snake_case spellings of the same keys are still accepted (never mixed).
_PAYLOAD_KEYS = frozenset({"summary", "places"})
_PLACE_ALIASES = {
    "place": "place", "id": "id", "attribution": "attribution", "location": "location",
    "googleMapsLinks": "links", "google_maps_links": "links",
}
_LINK_ALIASES = {
    "directionsUrl": "directions", "directions_url": "directions",
    "placeUrl": "place_url", "place_url": "place_url",
    "writeAReviewUrl": "review", "write_a_review_url": "review",
    "reviewsUrl": "reviews", "reviews_url": "reviews",
    "photosUrl": "photos", "photos_url": "photos",
}
_LINK_FIELDS = {
    "place_url": LinkField.PLACE_URL, "directions": LinkField.DIRECTIONS_URL,
    "review": LinkField.WRITE_REVIEW_URL, "reviews": LinkField.REVIEWS_URL,
    "photos": LinkField.PHOTOS_URL,
}


def _canonical(raw: object, aliases: Mapping[str, str]) -> dict[str, Any] | None:
    """Rename documented spellings to one canonical key; None on unknown keys or a mixed
    spelling of the same field."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        name = aliases.get(key) if isinstance(key, str) else None
        if name is None or name in out:
            return None
        out[name] = value
    return out


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def extract_search_places(payload: object) -> tuple[str, tuple[PlaceFields, ...], tuple[OtherLinks, ...]] | None:
    """Pull (summary, places, other links) out of a payload if it has exactly the documented
    shape, else None. Coordinates are parsed and dropped; links other than ``placeUrl`` are
    returned only so the parser can validate them. No content rules are applied here (hidden
    characters, url shapes): the ledger uses this too."""
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        return None
    summary, raw_places = payload["summary"], payload["places"]
    if not isinstance(summary, str) or not isinstance(raw_places, list):
        return None
    places: list[PlaceFields] = []
    others: list[OtherLinks] = []
    for raw in raw_places:
        place = _canonical(raw, _PLACE_ALIASES)
        if place is None or not {"id", "attribution", "links"} <= set(place):
            return None
        links = _canonical(place["links"], _LINK_ALIASES)
        attribution = place["attribution"]
        if links is None or "place_url" not in links or not isinstance(attribution, dict):
            return None
        if set(attribution) != {"title", "url"}:
            return None
        if not all(isinstance(v, str) for v in links.values()):
            return None
        location = place.get("location")
        if location is not None and not (
            isinstance(location, dict) and set(location) == {"latitude", "longitude"}
            and all(_is_number(v) for v in location.values())
        ):
            return None  # parsed, validated and DISCARDED
        pid, title, url = place["id"], attribution["title"], attribution["url"]
        if not all(isinstance(v, str) for v in (pid, title, url)):
            return None
        if "place" in place and place["place"] != f"places/{pid}":
            return None
        places.append((pid, title, url, links["place_url"]))
        others.append(tuple((_LINK_FIELDS[name], value) for name, value in links.items() if name != "place_url"))
    return summary, tuple(places), tuple(others)


def _build_answer(result: Any, ttls: RuntimeTTLs) -> GroundedAnswer:
    issued = result.issued
    if not isinstance(issued, IssuedToken) or issued.call_id != result.tool_call_id:
        raise ValueError("result was not issued by the discovery client")
    if result.tool != ToolId.SEARCH_PLACES:
        raise ValueError("only search_places produces a grounded answer")
    extracted = extract_search_places(result.payload)
    if extracted is None:
        raise ValueError("payload shape")
    summary, fields, others = extracted
    if has_lone_surrogate(summary) or any(has_lone_surrogate(f) for place in fields for f in place):
        raise ValueError("lone surrogate")
    for (_, _, _, place_url), extras in zip(fields, others, strict=True):
        for link_field, value in extras:  # every other link must match ITS field and the same place
            check_link(link_field, value)
            if data_token(value) != data_token(place_url):
                raise ValueError("a link belongs to a different place")
    flags = cited_flags(summary, len(fields))
    places = tuple(
        GroundedPlace(
            place_id=pid, attribution_title=title, attribution_url=url, place_url=place_url,
            tool_call_id=result.tool_call_id, cited=flags[index],
        )
        for index, (pid, title, url, place_url) in enumerate(fields)
    )
    return GroundedAnswer.build(
        text=summary, places=places, tool_call_id=result.tool_call_id, run_id=result.run_id,
        retrieved_at=result.retrieved_at, ttls=ttls,
    )


def parse_grounded_answer(result: Any, ttls: RuntimeTTLs) -> GroundedAnswer:
    """Turn an (untrusted) search_places ToolResult into a GroundedAnswer, or reject it whole.

    Only a result the client issued (private token) and only the search_places tool qualify.
    Every error is typed and static; no payload text or code point reaches a message."""
    answer: GroundedAnswer | None = None
    try:
        answer = _build_answer(result, ttls)
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        answer = None
    if answer is None:
        raise GroundedParseError()
    return answer


def grounded_evidence(
    answer: GroundedAnswer, *, evidence_id: str, tool_version: str, freshness: timedelta,
    tool: str = "maps_grounded_discovery",
) -> Evidence:
    """Evidence for a grounded answer: provenance and handling only, a hash and never text."""
    return Evidence(
        id=evidence_id, tool=tool, tool_version=tool_version, source="google_maps_grounding_lite",
        retrieved_at=answer.retrieved_at, expires_at=answer.retrieved_at + freshness,
        precision="grounded_text", kind=EvidenceKind.GROUNDED_ANSWER,
        content_class=ContentClass.MAPS_GROUNDED, llm_visible=True,
        storage_class=EvidenceStorageClass.DISPLAY_CACHE, attribution_required=True,
        cache_expires_at=answer.cache_expires_at, tool_call_id=answer.tool_call_id,
        content_hash=answer.content_hash,
    )
