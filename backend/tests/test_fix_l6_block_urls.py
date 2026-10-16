"""L6: DiscoveryPlace validates its own urls (https, allowlisted Google host, the field rules of
discovery/uris), and DiscoveryBlock carries a typed ``retrieved_at`` so a replay shows its age."""
from __future__ import annotations

from datetime import timedelta

import pytest
from maps_simulator import NOW, place_link
from pydantic import ValidationError
from wiring_support import CANARY, happy_env

from app.domain import DiscoveryBlock, DiscoveryPlace, FramingKind

GOOD = place_link(1)


def place(**kw) -> DiscoveryPlace:
    fields = dict(title="Invented - Google Maps", attribution_url=GOOD, place_url=GOOD)
    fields.update(kw)
    return DiscoveryPlace(**fields)


def test_a_valid_google_place_link_is_accepted() -> None:
    assert place().place_url == GOOD


@pytest.mark.parametrize("bad", [
    "http://www.google.com/maps/place//data=!4m2!3m1!1sSYNTHTOK0000000001:0x0000000000000001",
    "https://evil.example/maps/place//data=!4m2!3m1!1sSYNTHTOK0000000001:0x0000000000000001",
    "javascript:alert(1)",
    "https://www.google.com.evil.example/maps",
    "https://user@www.google.com/maps/place//data=!4m2!3m1!1sSYNTHTOK0000000001:0x1",
    "https://www.google.com/url?q=https://evil.example",
    "//www.google.com/maps/place",
    "",
])
def test_place_urls_must_be_https_google_links_of_the_documented_shape(bad) -> None:
    with pytest.raises(ValidationError):
        place(place_url=bad)
    with pytest.raises(ValidationError):
        place(attribution_url=bad)


def test_the_block_carries_a_typed_retrieved_at() -> None:
    fields = DiscoveryBlock.model_fields
    assert "retrieved_at" in fields
    assert list(fields) == ["render_mode", "text", "places", "branding", "translate_no",
                            "framing", "retrieved_at", "cache_expires_at"]
    with pytest.raises(ValidationError):
        DiscoveryBlock(text="t", places=(place(),), framing=FramingKind.RESULTS_INTRO,
                       retrieved_at="not a date", cache_expires_at=NOW)  # type: ignore[arg-type]


def test_a_replay_shows_the_original_retrieval_time() -> None:
    env, _, _ = happy_env()
    first = env.post(key="dup").json()
    env.clock.advance(hours=3)
    second = env.post(key="dup").json()
    assert second["discovery"]["retrieved_at"] == first["discovery"]["retrieved_at"]
    assert first["discovery"]["retrieved_at"].startswith("2026-10-15T12:00:00")
    assert CANARY in second["discovery"]["text"]
    assert NOW + timedelta(hours=24) > NOW


def test_openapi_lists_retrieved_at_on_the_block() -> None:
    env, _, _ = happy_env()
    props = env.client.get("/openapi.json").json()["components"]["schemas"]["DiscoveryBlock"]
    assert "retrieved_at" in props["properties"]
    assert "retrieved_at" in props["required"]
