"""Optional-field guard (fix pass 5, re-pointed at the FrameProposal in fix pass 6).

A live model once omitted a field the schema said was optional and the contract rejected it,
costing a repair call. This schema-driven guard omits every optional field of the real
model-facing JSON schema one at a time and asserts the proposal still validates, so a model that
legitimately leaves one out can never cost a paid failure. The server owns the tool, the
workspace id, the base version, the status and the next action: none of them is in the schema."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from wiring_support import real_stack

from app.frame import FrameProposal


def validates(payload: dict) -> bool:
    try:
        FrameProposal.model_validate(payload)
    except ValidationError:
        return False
    return True


def test_a_reply_without_the_old_tool_field_is_the_normal_shape() -> None:
    result = FrameProposal.model_validate({
        "intent": "DISCOVERY",
        "search": {"text_query": "quiet vegetarian place for dinner", "area": "Union Station",
                   "language_code": "en"}})
    assert result.search is not None and result.search.area == "Union Station"
    assert FrameProposal.model_validate({"intent": "OTHER"}).search is None


def test_a_reply_without_tool_is_one_provider_call_and_a_good_turn() -> None:
    env, transport, _ = real_stack(
        intent="DISCOVERY",
        search={"text_query": "quiet vegetarian restaurant", "area": "Union Station",
                "language_code": "en"})
    response = env.post({"message": "I want a quiet vegetarian restaurant for dinner near "
                                    "Union Station tonight."})
    assert response.status_code == 200, response.text
    assert len(transport.requests) == 1, "an omitted field cost a repair call"
    assert env.discovery.calls[0][1]["text_query"] == "quiet vegetarian restaurant near Union Station"
    assert response.json()["discovery"] is not None


def test_the_sent_body_shows_the_model_the_real_schema() -> None:
    env, transport, _ = real_stack(intent="OTHER")
    env.post()
    sent = transport.requests[0].body["response_format"]["json_schema"]["schema"]
    assert sent == FrameProposal.model_json_schema()


# --- the schema the provider receives (strict json_schema) ----------------------------------------
def test_the_provider_schema_keeps_optional_fields_optional() -> None:
    schema = FrameProposal.model_json_schema()
    assert schema["required"] == ["intent"]
    assert set(schema["properties"]) == {"intent", "search", "facts"}
    defs = schema["$defs"]
    assert set(defs["SearchArgs"]["required"]) == {"text_query"}
    assert set(defs["SearchArgs"]["properties"]) == {
        "text_query", "area", "language_code", "region_code", "page_size"}
    assert set(defs["ProposedFact"]["required"]) == {"key"}
    assert set(defs["ProposedFact"]["properties"]) == {"key", "value", "scope"}
    assert schema["properties"]["search"]["default"] is None  # the optional-null pattern
    dumped = json.dumps(schema)
    for keyword in ("oneOf", "allOf", "patternProperties", '"not"'):
        assert keyword not in dumped, keyword
    assert "description" not in dumped, "docstrings must not be sent to the provider"


# --- a schema-driven guard: omit each OPTIONAL field, one at a time -------------------------------
FULL = {
    "intent": "DISCOVERY",
    "search": {"text_query": "quiet restaurant", "area": "Union Station", "language_code": "en",
               "region_code": "CA", "page_size": 3},
    "facts": [{"key": "party", "value": "two", "scope": "trip"}],
}


def resolve(schema: dict, defs: dict) -> dict:
    while "$ref" in schema:
        schema = defs[schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            branch = resolve(branch, defs) if "$ref" in branch else branch
            if branch.get("type") not in (None, "null"):
                return branch
    return schema


def optional_paths(node: dict, value, defs: dict, path: tuple = ()):
    node = resolve(node, defs)
    if isinstance(value, dict) and "properties" in node:
        required = set(node.get("required", ()))
        for key, sub in node["properties"].items():
            if key in value:
                if key not in required:
                    yield (*path, key)
                yield from optional_paths(sub, value[key], defs, (*path, key))
    elif isinstance(value, list) and value and "items" in node:
        yield from optional_paths(node["items"], value[0], defs, (*path, 0))


def without(payload: dict, path: tuple) -> dict:
    clone = json.loads(json.dumps(payload))
    target = clone
    for step in path[:-1]:
        target = target[step]
    del target[path[-1]]
    return clone


SCHEMA = FrameProposal.model_json_schema()
PATHS = list(optional_paths(SCHEMA, FULL, SCHEMA.get("$defs", {})))


def test_the_full_payload_validates_and_the_walker_finds_the_optional_fields() -> None:
    assert validates(FULL)
    assert {p[-1] for p in PATHS} == {
        "search", "facts", "area", "language_code", "region_code", "page_size", "value", "scope"}


@pytest.mark.parametrize("path", PATHS, ids=lambda p: ".".join(map(str, p)))
def test_omitting_one_optional_field_never_costs_a_repair(path) -> None:
    assert validates(without(FULL, path)), f"omitting {'.'.join(map(str, path))} fails validation"


@pytest.mark.parametrize("key", ["intent"])
def test_omitting_a_required_field_does_fail(key) -> None:
    payload = dict(FULL)
    del payload[key]
    assert not validates(payload)
