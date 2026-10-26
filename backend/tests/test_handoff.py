"""Maps handoff URL: built from the place ID only, host pinned, nothing else in it."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.handoff import HandoffError, build_maps_url


def test_url_has_pinned_host_and_only_place_id() -> None:
    url = build_maps_url("ChIJabc_-123")
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.google.com", "/maps/search/")
    assert parse_qs(parts.query) == {"api": ["1"], "query": ["Google"],
                                     "query_place_id": ["ChIJabc_-123"]}
    assert "origin" not in url and not parts.fragment


@pytest.mark.parametrize("bad", ["", "a b", "a/b", "a&origin=1,2", "a?b", "a#b", "x" * 301,
                                 "http://evil", "ChIJé", "a\nb", "43.65,-79.38"])
def test_bad_place_ids_are_refused(bad: str) -> None:
    with pytest.raises(HandoffError):
        build_maps_url(bad)


def test_non_string_refused() -> None:
    with pytest.raises(HandoffError):
        build_maps_url(None)  # type: ignore[arg-type]
