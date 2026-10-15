"""The bounded schema validator's explicit, fail-closed "exactly one of" keyword."""
from __future__ import annotations

import pytest

from app.gateway.schema import schema_is_supported, validate

WAYPOINT = {
    "type": "object", "additionalProperties": False, "x_exactly_one_of": ["address", "place_id"],
    "properties": {"address": {"type": "string"}, "place_id": {"type": "string"}},
}


def test_exactly_one_of_the_listed_properties_must_be_present() -> None:
    assert schema_is_supported(WAYPOINT)
    assert validate(WAYPOINT, {"address": "a"}) is True
    assert validate(WAYPOINT, {"place_id": "p"}) is True
    assert validate(WAYPOINT, {}) is False
    assert validate(WAYPOINT, {"address": "a", "place_id": "p"}) is False


def test_it_applies_at_every_nesting_level() -> None:
    outer = {"type": "object", "additionalProperties": False, "required": ["w"], "properties": {"w": WAYPOINT}}
    assert validate(outer, {"w": {"address": "a"}}) is True
    assert validate(outer, {"w": {}}) is False
    assert validate(outer, {"w": {"address": "a", "place_id": "p"}}) is False


@pytest.mark.parametrize("bad", [
    {**WAYPOINT, "x_exactly_one_of": []},
    {**WAYPOINT, "x_exactly_one_of": ["address"]},  # one option is not a choice
    {**WAYPOINT, "x_exactly_one_of": ["address", "missing"]},
    {**WAYPOINT, "x_exactly_one_of": "address"},
    {**WAYPOINT, "x_exactly_one_of": ["address", 5]},
    {**WAYPOINT, "x_exactly_one_of": ["address", "address"]},
])
def test_malformed_constraints_fail_closed_at_schema_check_time(bad) -> None:
    assert not schema_is_supported(bad)


def test_unsupported_or_misspelled_combinators_still_fail_closed() -> None:
    for keyword in ("oneOf", "anyOf", "x_one_of", "exactlyOneOf"):
        assert not schema_is_supported({**WAYPOINT, keyword: ["address", "place_id"]})
