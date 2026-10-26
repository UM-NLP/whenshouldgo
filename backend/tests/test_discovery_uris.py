"""Field-pinned link validators. Every link field has ONE pattern in ONE table, taken from the
shapes seen in a live capture (2026-10-15) with every value invented here. Earlier protections all
remain: https only, exact host, no userinfo, port or fragment, no controls, whitespace, non-ASCII
or backslash, no dot segments even after percent-decoding, no encoded slash, backslash or NUL,
valid UTF-8 escapes, and no open-redirector paths."""
from __future__ import annotations

import pytest
from maps_simulator import (
    directions_link,
    photos_link,
    place_link,
    reviews_link,
    route_link,
    save_link,
    synth_id,
    synth_token,
    weather_link,
    write_review_link,
)

from app.discovery import LINK_HOSTS, LINK_RULES, LinkField, check_input_maps_url, check_link, data_token

BS = chr(92)  # backslash
ID = synth_id(1)
GOOD: dict[LinkField, list[str]] = {
    LinkField.PLACE_URL: [place_link(1), place_link(7)],
    LinkField.PLACE_ATTRIBUTION_URL: [place_link(1)],
    LinkField.DIRECTIONS_URL: [directions_link(1)],
    LinkField.PHOTOS_URL: [photos_link(1)],
    LinkField.REVIEWS_URL: [reviews_link(1)],
    LinkField.WRITE_REVIEW_URL: [write_review_link(1)],
    LinkField.ROUTE_ATTRIBUTION_URL: [route_link(), route_link("A", "B"), route_link("Café 合成, Toronto", "X Y")],
    LinkField.WEATHER_ATTRIBUTION_URL: [weather_link(), weather_link("Café 合成, Toronto, ON")],
    LinkField.SAVE_TO_MAPS_URL: [save_link(ID), save_link(ID, synth_id(2), synth_id(3))],
    LinkField.WEATHER_ICON_URI: ["https://maps.gstatic.com/weather/v1/synth_cloudy"],
}


def test_every_link_field_has_exactly_one_rule_in_the_table() -> None:
    assert set(LINK_RULES) == set(LinkField) == set(GOOD)
    assert LINK_HOSTS == frozenset({"www.google.com", "maps.gstatic.com"})  # verified live 2026-10-15


@pytest.mark.parametrize(("field", "uri"), [(f, u) for f, urls in GOOD.items() for u in urls])
def test_every_real_shape_is_accepted_by_its_own_field(field: LinkField, uri: str) -> None:
    check_link(field, uri)


@pytest.mark.parametrize("field", list(LinkField))
def test_a_shape_is_accepted_only_by_the_fields_it_belongs_to(field: LinkField) -> None:
    belongs = {f for f, urls in GOOD.items() if any(_ok(field, u) for u in urls)}
    for other, urls in GOOD.items():
        for uri in urls:
            if other == field:
                continue
            if _ok(field, uri):
                # only the two place-link fields share one shape on purpose
                assert {field, other} <= {LinkField.PLACE_URL, LinkField.PLACE_ATTRIBUTION_URL}, (field, other, uri)
    assert field in belongs


def _ok(field: LinkField, uri: str) -> bool:
    try:
        check_link(field, uri)
    except ValueError:
        return False
    return True


# --- hostile variants of EVERY good url ------------------------------------------------------------
def variants(uri: str) -> list[str]:
    host = uri.split("/")[2]
    path_start = len(uri.split("/")[0]) + 2 + len(host)
    rest = uri[path_start:]
    return [
        uri.replace("https://", "http://", 1),
        uri.replace(host, "evil.example", 1),
        uri.replace(host, host + ".evil.example", 1),
        uri.replace(host, host + ".", 1),
        uri.replace(host, host.upper(), 1),
        uri.replace(host, "user@" + host, 1),
        uri.replace(host, "user:pw@" + host, 1),
        uri.replace(host, host + ":8443", 1),
        uri.replace(host, host + ":443", 1),
        uri + "#frag",
        uri + "\n", uri + "\t", uri + " ", " " + uri, uri + "\x00", uri[:-1] + "é",
        uri.replace(host, host.replace(".", "。"), 1),
        "//" + host + rest,
        "javascript:alert(1)", "data:text/html,x", "",
        uri + "a" * 2_100,
        uri.replace("/", BS, 1) if "?" not in uri else uri + BS + "x",
        uri + ";jsessionid=1",
        uri + "%2f", uri + "%5c", uri + "%00", uri + "%2F..%2Furl", uri + "%FF", uri + "%C3%28", uri + "%252e%252e",
        uri + "/../url?q=https://evil.example", uri + "/./x",
        uri + "?q=https://evil.example", uri + "?continue=//evil.example",
    ]


@pytest.mark.parametrize(("field", "uri"), [(f, u) for f, urls in GOOD.items() for u in urls])
def test_hostile_variants_of_every_real_shape_are_rejected(field: LinkField, uri: str) -> None:
    survivors = [v for v in variants(uri) if _ok(field, v)]
    assert survivors == [], survivors[:3]


REDIRECTORS_AND_OLD_TRICKS = [
    "https://www.google.com/maps/../url?q=https://evil.example",
    "https://www.google.com/maps/./place/x", "https://www.google.com/maps/%2e%2e/url?q=x",
    "https://www.google.com/maps/..%2furl", "https://www.google.com/maps/%2E%2E/url",
    "https://www.google.com/maps" + BS + "..url", "https://www.google.com/maps/place/x" + BS + "y",
    "https://www.google.com/url?q=https://evil.example", "https://www.google.com/imgres?imgurl=x",
    "https://www.google.com/amp/s/evil.example", "https://www.google.com/maps/place/x?q=a%20b",
    "https://www.google.com/mapsx/place", "https://www.google.com/", "https://www.google.com",
    "https://maps.google.com/url?q=https://evil.example", "https://maps.google.com/maps/place/x",
    "https://goo.gl/maps/abc", "https://maps.app.goo.gl/SYNTH0001", "https://www.google.cn/maps",
    "https://www.gоogle.com/maps", "https://xn--www-google-5kd.com/maps",
    "https://www.google.com/maps/place//data=!4m2!3m1!1sTOO:0xAB", "https://www.google.com/maps/place//data=!4m2!3m1!",
    "https://www.google.com/maps/place/data=!4m2!3m1!" + synth_token(1),  # the empty segment is part of the shape
    "https://www.google.com/maps/place///data=!4m2!3m1!" + synth_token(1),
    "https://www.google.com/maps/place//data=!4m2!3m1!" + synth_token(1) + "/../url",
    "https://www.google.com/maps/place//../url",
]


@pytest.mark.parametrize("uri", REDIRECTORS_AND_OLD_TRICKS)
@pytest.mark.parametrize("field", list(LinkField))
def test_redirectors_and_earlier_hostile_forms_are_rejected_by_every_field(field: LinkField, uri: str) -> None:
    with pytest.raises(ValueError):
        check_link(field, uri)


# --- the data= token and per-place consistency -------------------------------------------------------
def test_the_data_token_is_extracted_and_is_shared_by_all_links_of_one_place() -> None:
    token = synth_token(3)
    for uri in (place_link(3), photos_link(3), reviews_link(3), write_review_link(3), directions_link(3)):
        assert data_token(uri) == token
    assert data_token(place_link(3)) != data_token(place_link(4))
    assert data_token("https://www.google.com/maps/place//data=!4m2!3m1!") is None
    assert data_token(route_link()) is None and data_token(save_link(ID)) is None


def test_the_token_is_not_the_place_id() -> None:
    # Live captures show the data= token is a DIFFERENT identifier from places[].id (the review
    # brief assumed they were equal). Consistency is therefore token-to-token across one place's
    # links, never token-to-id.
    assert data_token(place_link(1)) != synth_id(1) and synth_id(1) not in place_link(1)


# --- field specifics -------------------------------------------------------------------------------------
@pytest.mark.parametrize("uri", [
    "https://www.google.com/search?q=Weather+for+X", "https://www.google.com/search?q=Weather+for+X&btnI=1",
    "https://www.google.com/search?btnI=1&q=Weather+for+X", "https://www.google.com/search?q=Weather+for+a&q=Weather+for+b",
    "https://www.google.com/search?q=", "https://www.google.com/search", "https://www.google.com/search?Q=Weather+for+X",
    "https://www.google.com/search?q=Weather+for+X&", "https://www.google.com/search?q=other+text",
    "https://www.google.com/search?q=Weather+for+%2f", "https://www.google.com/search?q=Weather+for+%5c",
    "https://www.google.com/search?q=Weather+for+%00", "https://www.google.com/search?q=Weather+for+%FF",
    "https://www.google.com/search?q=Weather+for+%0A", "https://www.google.com/search?q=Weather+for+a%26b%3Dc",
    "https://www.google.com/search/x?q=Weather+for+X", "https://www.google.com/search?q=Weather+for+" + "a" * 600,
])
def test_a_weather_attribution_is_exactly_search_with_one_weather_q(uri: str) -> None:
    if uri == "https://www.google.com/search?q=Weather+for+X":
        check_link(LinkField.WEATHER_ATTRIBUTION_URL, uri)
        return
    with pytest.raises(ValueError):
        check_link(LinkField.WEATHER_ATTRIBUTION_URL, uri)


@pytest.mark.parametrize("uri", [
    "https://www.google.com/maps/@?api=1&map_action=shortlist&place_ids=" + ID + "&extra=1",
    "https://www.google.com/maps/@?api=2&map_action=shortlist&place_ids=" + ID,
    "https://www.google.com/maps/@?api=1&map_action=search&place_ids=" + ID,
    "https://www.google.com/maps/@?api=1&map_action=shortlist",
    "https://www.google.com/maps/@?api=1&map_action=shortlist&place_ids=",
    "https://www.google.com/maps/@?api=1&api=1&map_action=shortlist&place_ids=" + ID,
    "https://www.google.com/maps/@?api=1&map_action=shortlist&place_ids=bad%20id",
    "https://www.google.com/maps/@?api=1&map_action=shortlist&place_ids=" + ",".join([ID] * 21),
    "https://www.google.com/maps/@?api=1&map_action=shortlist&place_ids=" + ID + ",,x",
    "https://www.google.com/maps/@?api=1&map_action=shortlist&place_ids=" + ID + "&",
    "https://www.google.com/maps/@/x?api=1&map_action=shortlist&place_ids=" + ID,
    "https://www.google.com/maps/@",
    "https://www.google.com/maps/@?api=1&map_action=shortlist&place_ids=https://evil.example",
])
def test_a_save_link_must_have_exactly_the_three_documented_keys(uri: str) -> None:
    with pytest.raises(ValueError):
        check_link(LinkField.SAVE_TO_MAPS_URL, uri)


def test_save_link_order_of_keys_does_not_matter_but_the_values_do() -> None:
    check_link(LinkField.SAVE_TO_MAPS_URL, f"https://www.google.com/maps/@?place_ids={ID}&map_action=shortlist&api=1")


@pytest.mark.parametrize("uri", [
    "https://www.google.com/maps/dir/a", "https://www.google.com/maps/dir/a/b/c", "https://www.google.com/maps/dir//b",
    "https://www.google.com/maps/dir/a/", "https://www.google.com/maps/dir/../b", "https://www.google.com/maps/dir/a/..",
    "https://www.google.com/maps/dir/a/%2e%2e", "https://www.google.com/maps/dir/%2E/b", "https://www.google.com/maps/dir/a%2Fb/c",
    "https://www.google.com/maps/dir/a/b%5Cc", "https://www.google.com/maps/dir/a/b%00", "https://www.google.com/maps/dir/a/%FF",
    "https://www.google.com/maps/dir/a/%C3", "https://www.google.com/maps/dir/a/%4", "https://www.google.com/maps/dir/a/%zz",
    "https://www.google.com/maps/dir/a/b%0A", "https://www.google.com/maps/dir/a/b%252e",
    "https://www.google.com/maps/dir/" + "a" * 401 + "/b", "https://www.google.com/maps/dir/a/b?x=1",
    "https://www.google.com/maps/dir/a b/c",
])
def test_a_route_attribution_is_exactly_two_percent_encoded_segments(uri: str) -> None:
    with pytest.raises(ValueError):
        check_link(LinkField.ROUTE_ATTRIBUTION_URL, uri)


@pytest.mark.parametrize("uri", [
    "https://maps.gstatic.com/weather/v1/", "https://maps.gstatic.com/weather/v1/a/b", "https://maps.gstatic.com/weather/v2/x",
    "https://maps.gstatic.com/weather/v1/x?y=1", "https://maps.gstatic.com/weather/v1/..", "https://maps.gstatic.com/weather/v1/%2e%2e",
    "https://maps.gstatic.com/other/v1/x", "https://www.google.com/weather/v1/x", "https://maps.gstatic.com/weather/v1/" + "a" * 70,
])
def test_the_weather_icon_must_be_a_gstatic_weather_v1_name(uri: str) -> None:
    with pytest.raises(ValueError):
        check_link(LinkField.WEATHER_ICON_URI, uri)


# --- INPUT urls for resolve_maps_urls keep their own narrow rules ----------------------------------------------
@pytest.mark.parametrize("uri", [
    "https://maps.app.goo.gl/SYNTH0001",
    "https://www.google.com/maps/place/Synth_Place_One",
    "https://maps.google.com/maps/place/Synth_Place_One",
])
def test_resolve_input_urls_accept_the_three_documented_forms(uri: str) -> None:
    check_input_maps_url(uri)


@pytest.mark.parametrize("uri", [
    "https://www.google.com/maps", "https://www.google.com/maps/dir/a/b", "https://maps.google.com/url?q=x",
    "https://maps.google.com/maps/../url", "https://evil.example/maps/place/x", "http://maps.app.goo.gl/SYNTH0001",
    "https://www.google.com/maps/place/x" + BS + "y", "https://www.google.com/maps/place/%2e%2e/url",
])
def test_resolve_input_urls_reject_everything_else(uri: str) -> None:
    with pytest.raises(ValueError):
        check_input_maps_url(uri)
