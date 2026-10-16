"""Link allowlists: strict path shapes on EVERY host, no redirectors (H1).

TODO(verify-live): the real link shapes Grounding Lite returns. These are deliberately
narrow, so an unexpected real shape fails closed until a live capture widens them.
"""
from __future__ import annotations

import pytest

from app.discovery import (
    MAPS_LINK_HOSTS,
    check_attribution_url,
    check_input_maps_url,
    check_maps_link,
)

BS = chr(92)  # backslash

GOOD_LINKS = [
    "https://maps.app.goo.gl/SYNTH0001",
    "https://maps.app.goo.gl/abcDEF123_-x",
    "https://www.google.com/maps",
    "https://www.google.com/maps/place/SYNTH_PLACE_0001",
    "https://www.google.com/maps/place/Invented+Spot/@43.65,-79.38,17z/data=!4m5",
    "https://www.google.com/maps/place?q=place_id:SYNTH_PLACE_0001",
    "https://google.com/maps/place/SYNTH_PLACE_0001",
]

REDIRECTS_AND_TRICKS = [
    "https://www.google.com/maps/../url?q=https://evil.example",
    "https://www.google.com/maps/./place/x",
    "https://www.google.com/maps/%2e%2e/url?q=x",
    "https://www.google.com/maps/..%2furl",
    "https://www.google.com/maps/%2E%2E/url",
    "https://www.google.com/maps" + BS + "..url",
    "https://www.google.com/maps/place/x" + BS + "y",
    "https://www.google.com/maps/place/x;jsessionid=1",
    "https://www.google.com/maps/place//x",
    "https://www.google.com/maps//url",
    "https://www.google.com//maps/place/x",
    "https://www.google.com/maps/place/x/",
    "https://www.google.com/url?q=https://evil.example",
    "https://www.google.com/imgres?imgurl=x",
    "https://www.google.com/amp/s/evil.example",
    "https://www.google.com/maps/place/x?q=https://evil.example",
    "https://www.google.com/maps/place/x?continue=//evil.example",
    "https://www.google.com/maps/place/x?q=a%20b",
    "https://www.google.com/maps/place/x#fragment",
    "https://www.google.com/mapsx/place",
    "https://www.google.com/maps.evil/place",
    "https://www.google.com/",
    "https://www.google.com",
    "https://maps.google.com/url?q=https://evil.example",
    "https://maps.google.com/maps/place/x",  # not an output host at all
    "https://goo.gl/maps/abc",
    "https://maps.app.goo.gl/",
    "https://maps.app.goo.gl/a/b",
    "https://maps.app.goo.gl/x" + BS + "y",
    "https://maps.app.goo.gl/..",
    "https://maps.app.goo.gl/ab",  # too short for a token
    "https://maps.app.goo.gl/SYNTH0001/",
    "https://user@www.google.com/maps",
    "https://user:pw@maps.app.goo.gl/SYNTH0001",
    "https://www.google.com:8443/maps",
    "https://www.google.com:443/maps",
    "https://www.google.com./maps",
    "https://WWW.google.com/maps",
    "https://www.GOOGLE.com/maps",
    "https://www.google.com.evil.example/maps",
    "https://evil.example/maps",
    "https://www.google.cn/maps",
    "https://www。google.com/maps",
    "https://www.gоogle.com/maps",  # Cyrillic o
    "https://xn--www-google-5kd.com/maps",
    "https://www.google.com/maps/pláce",
    "http://www.google.com/maps",
    "//www.google.com/maps",
    "javascript:alert(1)",
    "data:text/html,x",
    "https://www.google.com/maps\n",
    "https://www.google.com/maps\t",
    "https://www.google.com/ maps",
    "https://www.google.com/maps\x00",
    "https://www.google.com/maps/place/" + "a" * 2_100,
    "",
]


@pytest.mark.parametrize("uri", GOOD_LINKS)
def test_known_link_shapes_are_accepted(uri: str) -> None:
    check_maps_link(uri)
    check_attribution_url(uri)


@pytest.mark.parametrize("uri", REDIRECTS_AND_TRICKS)
def test_redirectors_dot_segments_encodings_and_host_tricks_are_rejected_everywhere(uri: str) -> None:
    for check in (check_maps_link, check_attribution_url):
        with pytest.raises(ValueError):
            check(uri)


def test_the_output_link_hosts_are_pinned_not_guessed() -> None:
    # TODO(verify-live): neither maps.google.com nor goo.gl/maps is documented as an output host.
    assert MAPS_LINK_HOSTS == frozenset({"maps.app.goo.gl", "www.google.com", "google.com"})


def test_attribution_urls_may_also_be_a_search_page_but_place_links_may_not() -> None:
    # TODO(verify-live): "www.google.com/search" is documented only as a possible attribution url.
    for uri in ("https://www.google.com/search?q=place_id:SYNTH_PLACE_0001", "https://www.google.com/search?q=x"):
        check_attribution_url(uri)
        with pytest.raises(ValueError):
            check_maps_link(uri)
    for uri in ("https://www.google.com/search/../url?q=x", "https://www.google.com/search/x",
                "https://www.google.com/search?q=https://evil.example", "https://www.google.com/search?q=%2e",
                "https://www.google.com/search",
                "https://google.com/url?q=x"):
        with pytest.raises(ValueError):
            check_attribution_url(uri)


@pytest.mark.parametrize("uri", [
    "https://maps.app.goo.gl/SYNTH0001",
    "https://www.google.com/maps/place/SYNTH_PLACE_0001",
    "https://maps.google.com/maps/place/SYNTH_PLACE_0001",
])
def test_resolve_input_urls_accept_the_three_documented_forms(uri: str) -> None:
    check_input_maps_url(uri)


@pytest.mark.parametrize("uri", [
    "https://www.google.com/maps",  # not a single-place URL
    "https://www.google.com/maps/dir/a/b",
    "https://maps.google.com/url?q=x",
    "https://maps.google.com/maps/../url",
    "https://evil.example/maps/place/x",
    "http://maps.app.goo.gl/SYNTH0001",
    "https://www.google.com/maps/place/x" + BS + "y",
    "https://www.google.com/maps/place/%2e%2e/url",
])
def test_resolve_input_urls_reject_everything_else(uri: str) -> None:
    with pytest.raises(ValueError):
        check_input_maps_url(uri)
