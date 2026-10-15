"""GroundedAnswer contract, rebuilt around the documented search_places output."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from maps_simulator import (
    INJECTION,
    NOW,
    SYNTH_TEXT,
    SyntheticMapsClient,
    new_run,
    synthetic_payload,
    synthetic_place,
    synthetic_weather,
)
from test_gateway_support import make_config

from app.discovery import (
    GroundedAnswer,
    GroundedParseError,
    GroundedPlace,
    ToolResult,
    content_hash,
    display_hash,
    grounded_evidence,
    parse_grounded_answer,
)
from app.discovery._mint import IssuedToken
from app.discovery.errors import DiscoveryConfigError
from app.domain import (
    MAX_MAPS_CACHE,
    ContentClass,
    EvidenceKind,
    EvidenceStorageClass,
    evolve,
)

TTLS = make_config().ttls


def result_for(payload=None, tool="search_places", run=None, args=None):
    client = SyntheticMapsClient(payload=payload)
    return client.call_tool(run or new_run(), tool, args or {"text_query": "quiet cafe"})


def make_answer(payload=None, **kw) -> GroundedAnswer:
    """Test-only builder: mints a ToolResult through the simulator, then parses it."""
    return parse_grounded_answer(result_for(payload, **kw), TTLS)


def test_a_parsed_answer_keeps_the_summary_verbatim_and_hashes_what_the_user_sees() -> None:
    answer = make_answer()
    assert answer.text == SYNTH_TEXT
    assert answer.content_hash == content_hash(SYNTH_TEXT)
    assert answer.display_hash == display_hash(
        SYNTH_TEXT, [(p.place_id, p.attribution_title, p.attribution_url, p.place_url) for p in answer.places])
    assert answer.content_class == ContentClass.MAPS_GROUNDED
    assert answer.storage_class == EvidenceStorageClass.DISPLAY_CACHE
    with pytest.raises(ValueError):
        answer.text = "edited"  # type: ignore[misc]
    with pytest.raises(ValueError):
        GroundedAnswer.model_validate({**answer.model_dump(), "rating": 4.5})


def test_places_keep_only_id_attribution_and_the_place_link_everything_else_is_discarded() -> None:
    answer = make_answer()
    place = answer.places[0]
    assert (place.place_id, place.attribution_title) == ("SYNTH_PLACE_0001", "Invented spot 1")
    assert place.attribution_url == "https://maps.app.goo.gl/SYNTH0001"
    assert place.place_url == "https://www.google.com/maps/place/SYNTH_PLACE_0001"
    assert set(GroundedPlace.model_fields) == {
        "place_id", "attribution_title", "attribution_url", "place_url", "tool_call_id", "cited"}
    dumped = answer.model_dump_json() + repr(answer)
    for discarded in ("43.65", "-79.38", "directionsUrl", "reviewsUrl", "photosUrl", "writeAReview",
                      "/review", "/photos", "/dir/", "location", "latitude"):
        assert discarded not in dumped, discarded


def test_there_are_no_typed_fields_for_coordinates_ratings_hours_images_or_server_text() -> None:
    banned = {"latitude", "longitude", "lat", "lng", "location", "coordinates", "rating", "ratings",
              "hours", "opening_hours", "category", "categories", "types", "address", "framing",
              "server_text", "note", "reviews", "price_level", "image_uri", "preview", "description",
              "directions_url", "photos_url", "reviews_url"}
    for model in (GroundedAnswer, GroundedPlace):
        assert banned.isdisjoint(model.model_fields), model


def test_the_hashes_must_match_so_a_modified_answer_cannot_be_built() -> None:
    answer = make_answer()
    with pytest.raises(ValueError):
        evolve(answer, text=answer.text + " (edited)")
    with pytest.raises(ValueError):
        evolve(answer, content_hash="0" * 64)
    with pytest.raises(ValueError):
        evolve(answer, display_hash="0" * 64)
    forged_place = evolve(answer.places[0], attribution_title="Better title")
    with pytest.raises(ValueError):
        evolve(answer, places=(forged_place, answer.places[1]))


def test_class_and_storage_are_fixed() -> None:
    dumped = make_answer().model_dump()
    for change in ({"content_class": ContentClass.MAPS_PLACES}, {"content_class": ContentClass.USER},
                   {"storage_class": EvidenceStorageClass.EPHEMERAL},
                   {"storage_class": EvidenceStorageClass.PLACE_ID_ONLY}):
        with pytest.raises(ValueError):
            GroundedAnswer.model_validate({**dumped, **change})


def test_cache_expiry_defaults_to_24_hours_and_never_exceeds_30_days() -> None:
    answer = make_answer()
    assert answer.cache_expires_at == answer.retrieved_at + timedelta(hours=24)
    dumped = answer.model_dump()
    for delta in (timedelta(days=30, seconds=1), timedelta(days=90), timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError):
            GroundedAnswer.model_validate({**dumped, "cache_expires_at": answer.retrieved_at + delta})
    ok = GroundedAnswer.model_validate({**dumped, "cache_expires_at": answer.retrieved_at + MAX_MAPS_CACHE})
    assert ok.cache_expires_at - ok.retrieved_at == MAX_MAPS_CACHE
    short = parse_grounded_answer(result_for(), evolve(TTLS, grounded_cache_hours=2))
    assert short.cache_expires_at - short.retrieved_at == timedelta(hours=2)
    with pytest.raises(ValueError):
        evolve(TTLS, grounded_cache_hours=24 * 30 + 1)


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        GroundedAnswer.model_validate({**make_answer().model_dump(), "retrieved_at": NOW.replace(tzinfo=None)})


# --- citations -------------------------------------------------------------------------------------
def test_citation_indexes_must_be_in_range_and_are_flagged_per_place() -> None:
    answer = make_answer(synthetic_payload(text="Only the second one [1] is quiet."))
    assert [p.cited for p in answer.places] == [False, True]
    both = make_answer(synthetic_payload(text="Both [0, 1] are fine."))
    assert [p.cited for p in both.places] == [True, True]
    none = make_answer(synthetic_payload(text="No citations here at all."))
    assert [p.cited for p in none.places] == [False, False]  # zero citations is allowed


@pytest.mark.parametrize("text", ["Out of range [2].", "Way out [99] and [0].", "List [0, 5].", "[2]"])
def test_a_citation_beyond_the_places_rejects_the_whole_payload(text: str) -> None:
    with pytest.raises(GroundedParseError):
        make_answer(synthetic_payload(text=text))


def test_place_count_and_uniqueness_are_enforced() -> None:
    for payload in (synthetic_payload(places=0), synthetic_payload(places=21),
                    synthetic_payload() | {"places": [synthetic_place(1), synthetic_place(1)]}):
        with pytest.raises(GroundedParseError):
            make_answer(payload)
    assert len(make_answer(synthetic_payload(text="plain text", places=20)).places) == 20


# --- the documented payload and nothing else --------------------------------------------------------
@pytest.mark.parametrize("payload", [
    {}, {"summary": "t"}, {"places": []}, "string", None, [],
    synthetic_payload() | {"extra": "key"},
    {"summary": 5, "places": [synthetic_place(1)]},
    {"summary": "", "places": [synthetic_place(1)]},
    {"summary": "t", "places": [synthetic_place(1) | {"rating": 4.5}]},
    {"summary": "t", "places": [synthetic_place(1) | {"location": {"latitude": 1.0, "longitude": 2.0, "altitude": 3}}]},
    {"summary": "t", "places": [synthetic_place(1) | {"attribution": {"title": "x", "url": "https://www.google.com/maps", "icon": "i"}}]},
    {"summary": "t", "places": [synthetic_place(1) | {"googleMapsLinks": {**synthetic_place(1)["googleMapsLinks"], "shareUrl": "x"}}]},
    {"summary": "t", "places": [synthetic_place(1) | {"place": "places/OTHER"}]},
    {"summary": "t", "places": [synthetic_place(1) | {"id": "has space", "place": "places/has space"}]},
    {"summary": "t", "places": [synthetic_place(1) | {"id": "x" * 301, "place": "places/" + "x" * 301}]},
    {"summary": "t", "places": [synthetic_place(1) | {"attribution": {"title": "", "url": "https://www.google.com/maps"}}]},
    {"summary": "t", "places": [synthetic_place(1) | {"attribution": {"title": "x" * 201, "url": "https://www.google.com/maps"}}]},
    {"summary": "t", "places": [synthetic_place(1) | {"attribution": {"title": "x", "url": "https://evil.example/x"}}]},
    {"summary": "t", "places": [{**synthetic_place(1), "googleMapsLinks": {"placeUrl": "https://evil.example/x"}}]},
    {"summary": "x" * 8_001, "places": [synthetic_place(1)]},
])
def test_malformed_or_undocumented_payloads_are_rejected_whole(payload) -> None:
    with pytest.raises(GroundedParseError):
        make_answer(payload) if isinstance(payload, dict) else parse_grounded_answer(
            _result_with(payload), TTLS)


def _result_with(payload):
    return ToolResult.model_construct(
        tool="search_places", tool_call_id="tc-1", run_id="run-1", retrieved_at=NOW, payload=payload,
        size_bytes=1, billable_units=1, travel_mode=None, issued=IssuedToken.__new__(IssuedToken))


def test_both_documented_casings_of_a_field_are_accepted_but_not_mixed() -> None:
    snake = synthetic_place(1)
    snake["google_maps_links"] = {"place_url": snake["googleMapsLinks"]["placeUrl"]}
    del snake["googleMapsLinks"]
    assert make_answer({"summary": "t", "places": [snake]}).places[0].place_url.endswith("SYNTH_PLACE_0001")
    mixed = synthetic_place(1) | {"google_maps_links": {"place_url": "https://www.google.com/maps/place/X"}}
    with pytest.raises(GroundedParseError):
        make_answer({"summary": "t", "places": [mixed]})


def test_only_search_places_may_become_a_grounded_answer() -> None:
    for tool, args in (("lookup_weather", {"location": {"lat_lng": {"latitude": 1.0, "longitude": 2.0}}}),
                       ("compute_routes", {"origin": {"address": "a"}, "destination": {"address": "b"}}),
                       ("resolve_names", {"queries": [{"text": "x"}]}),
                       ("resolve_maps_urls", {"urls": ["https://maps.app.goo.gl/SYNTH0001"]})):
        with pytest.raises(GroundedParseError):
            parse_grounded_answer(result_for(synthetic_payload(), tool=tool, args=args), TTLS)
    with pytest.raises(GroundedParseError):
        parse_grounded_answer(result_for(synthetic_weather(), tool="lookup_weather", args={
            "location": {"place_id": "SYNTH_PLACE_0001"}}), TTLS)


def test_a_result_the_client_did_not_issue_is_refused() -> None:
    result = result_for()
    unissued = ToolResult.model_construct(**{**dict(result), "issued": None})
    with pytest.raises(GroundedParseError):
        parse_grounded_answer(unissued, TTLS)
    with pytest.raises(DiscoveryConfigError):
        IssuedToken(object(), "tc-1")  # the authority is private to the client module


def test_the_issued_token_must_belong_to_this_call() -> None:
    result = result_for()
    wrong = ToolResult.model_construct(**{**dict(result), "tool_call_id": "tc-other"})
    with pytest.raises(GroundedParseError):
        parse_grounded_answer(wrong, TTLS)


# --- hidden characters ---------------------------------------------------------------------------------
@pytest.mark.parametrize("title", ["Caf‮é", "Zero​width", "BOM﻿", "Joiner‍", "Iso⁦late",
                                   "Tab\there", "New\nline", "Nul\x00l", "Lone\ud800surrogate", "Soft­hyphen"])
def test_attribution_titles_reject_any_format_control_surrogate_or_control_character(title: str) -> None:
    payload = {"summary": "t", "places": [synthetic_place(1) | {"attribution": {
        "title": title, "url": "https://maps.app.goo.gl/SYNTH0001"}}]}
    with pytest.raises(GroundedParseError):
        make_answer(payload)


@pytest.mark.parametrize("text", ["abc‮def", "abc‫def", "abc⁦def", "abc⁧x⁩",
                                  "abc‎x", "abc‏x", "abc؜x", "zero​width", "bom﻿x",
                                  "word⁠joiner", "lone\ud800surrogate", "bell\x07", "nul\x00"])
def test_summary_text_with_bidi_overrides_zero_width_or_surrogates_is_rejected_not_rewritten(text: str) -> None:
    with pytest.raises(GroundedParseError) as caught:
        make_answer(synthetic_payload(text=text))
    assert "u202e" not in str(caught.value).lower() and "\ud800" not in str(caught.value)


def test_ordinary_joiners_are_allowed_in_the_summary_only_for_emoji_and_scripts() -> None:
    # ZWJ builds emoji sequences and ZWNJ is required by Persian and Indic scripts; both are
    # ordinary text. Every other Cf character (bidi, zero-width space, BOM, word joiner) is not.
    family = "Family-friendly \U0001F468‍\U0001F469‍\U0001F467 spot; می‌خواهم"
    answer = make_answer(synthetic_payload(text=family))
    assert answer.text == family  # verbatim


def test_a_lone_surrogate_never_raises_a_unicode_error_from_hashing_or_parsing() -> None:
    for payload in (synthetic_payload(text="a\ud800b"),
                    synthetic_payload() | {"places": [synthetic_place(1, id="SYNTH\ud800", place="places/SYNTH\ud800")]}):
        with pytest.raises(GroundedParseError):
            make_answer(payload)
    assert len(content_hash("a\ud800b")) == 64  # hashing is surrogate-safe


def test_text_never_appears_in_repr_or_validation_errors() -> None:
    answer = make_answer(synthetic_payload(text=INJECTION))
    assert INJECTION not in repr(answer) and "alert(1)" not in repr(answer)
    with pytest.raises(ValueError) as caught:
        GroundedAnswer.model_validate({**answer.model_dump(), "content_hash": "f" * 64})
    assert "alert(1)" not in str(caught.value) and "input_value" not in str(caught.value)


def test_the_summary_is_taken_verbatim_with_no_server_authored_content() -> None:
    answer = make_answer(synthetic_payload(text="  padded  \n[0]"))
    assert answer.text == "  padded  \n[0]"  # no strip, trim or normalisation
    assert answer.tool_call_id.startswith("tc-") and answer.run_id == "run-1"
    assert all(p.tool_call_id == answer.tool_call_id for p in answer.places)


def test_grounded_evidence_holds_only_a_hash_and_correct_handling_fields() -> None:
    answer = make_answer()
    evidence = grounded_evidence(answer, evidence_id="ev-1", tool_version="2026-10",
                                 freshness=timedelta(hours=1))
    assert evidence.kind == EvidenceKind.GROUNDED_ANSWER
    assert evidence.content_class == ContentClass.MAPS_GROUNDED
    assert evidence.storage_class == EvidenceStorageClass.DISPLAY_CACHE
    assert evidence.attribution_required is True and evidence.llm_visible is True
    assert evidence.content_hash == answer.content_hash
    assert evidence.cache_expires_at == answer.cache_expires_at
    assert SYNTH_TEXT not in evidence.model_dump_json()
    assert json.loads(evidence.model_dump_json())["tool_call_id"] == answer.tool_call_id
