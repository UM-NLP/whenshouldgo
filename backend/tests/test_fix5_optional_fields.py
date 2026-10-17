"""Fix pass 5: the SERVER owns the tool. A live model returned
{"patch": ..., "intent": "DISCOVERY", "search": {...}} with NO "tool" and FrameResult rejected it
("discovery needs a tool"), costing a repair call. Now an omitted tool is filled in with
search_places (the only tool Phase 0 grants); a tool the model NAMES is still checked as before.
A schema-driven guard omits every optional field one at a time so this class of bug is caught
offline."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from wiring_support import Env, real_stack

import app.frame as frame
from app.discovery import ToolId
from app.frame import FrameIntent, FrameResult

PATCH = {"workspace_id": "ws-1", "base_version": 0, "next_action": "SEARCH"}
# The exact shape captured from the live model (synthetic text).
LIVE_DISCOVERY = {
    "patch": PATCH, "intent": "DISCOVERY",
    "search": {"text_query": "quiet vegetarian place for dinner", "area": "Union Station",
               "language_code": "en"},
}
LIVE_OTHER = {"patch": PATCH, "intent": "OTHER"}


def validates(payload: dict) -> bool:
    try:
        FrameResult.model_validate(payload)
    except ValidationError:
        return False
    return True


def test_a_discovery_reply_without_a_tool_gets_search_places() -> None:
    result = FrameResult.model_validate(LIVE_DISCOVERY)
    assert result.tool == ToolId.SEARCH_PLACES
    assert result.search is not None and result.search.area == "Union Station"


def test_a_null_tool_is_filled_in_too() -> None:
    assert FrameResult.model_validate({**LIVE_DISCOVERY, "tool": None}).tool == ToolId.SEARCH_PLACES


def test_an_other_reply_without_tool_or_search_validates() -> None:
    result = FrameResult.model_validate(LIVE_OTHER)
    assert (result.intent, result.tool, result.search) == (FrameIntent.OTHER, None, None)


@pytest.mark.parametrize("tool", ["resolve_names", "resolve_maps_urls", "lookup_weather",
                                  "compute_routes"])
def test_a_discovery_reply_naming_another_tool_still_rejects(tool) -> None:
    assert not validates({**LIVE_DISCOVERY, "tool": tool})  # search belongs to search_places only


def test_a_named_tool_other_than_search_places_without_search_still_parses_for_the_server_check() -> None:
    # unchanged: the model may name resolve_names without arguments; the ORCHESTRATOR refuses it
    result = FrameResult.model_validate(
        {"patch": PATCH, "intent": "DISCOVERY", "tool": "resolve_names"})
    assert result.tool == ToolId.RESOLVE_NAMES


def test_an_unknown_tool_name_rejects() -> None:
    assert not validates({**LIVE_DISCOVERY, "tool": "admin_console"})


def test_other_with_any_tool_rejects() -> None:
    assert not validates({**LIVE_OTHER, "tool": "search_places"})
    assert not validates({**LIVE_OTHER, "tool": "resolve_names"})


def test_discovery_without_search_still_rejects() -> None:
    assert not validates({"patch": PATCH, "intent": "DISCOVERY"})


# --- through the real stack ---------------------------------------------------------------------
def test_a_reply_without_tool_is_one_provider_call_and_a_good_turn() -> None:
    env, transport, _ = real_stack(
        intent="DISCOVERY", omit_tool=True,
        search={"text_query": "quiet vegetarian restaurant", "area": "Union Station",
                "language_code": "en"})
    response = env.post({"message": "I want a quiet vegetarian restaurant for dinner near "
                                    "Union Station tonight."})
    assert response.status_code == 200, response.text
    assert len(transport.requests) == 1, "the omitted tool cost a repair call"
    assert env.discovery.calls[0][1]["text_query"] == "quiet vegetarian restaurant near Union Station"
    assert response.json()["discovery"] is not None


def test_the_sent_body_shows_the_model_the_real_schema() -> None:
    env, transport, _ = real_stack(intent="OTHER")
    env.post()
    body = transport.requests[0].body
    sent = body["response_format"]["json_schema"]["schema"]
    assert sent == FrameResult.model_json_schema()


# --- the schema shape the provider receives (strict json_schema) ----------------------------------
def test_the_provider_schema_keeps_optional_fields_optional() -> None:
    schema = FrameResult.model_json_schema()
    assert set(schema["required"]) == {"patch", "intent"}
    assert set(schema["properties"]) == {"patch", "intent", "tool", "search"}
    defs = schema["$defs"]
    assert set(defs["SearchArgs"]["required"]) == {"text_query"}
    assert set(defs["SearchArgs"]["properties"]) == {
        "text_query", "area", "language_code", "region_code", "page_size"}
    assert "default" not in schema["properties"]["tool"] or schema["properties"]["tool"][
        "default"] is None  # the same optional-null pattern as search and area
    assert set(defs["WorkspacePatch"]["required"]) == {"workspace_id", "base_version",
                                                       "next_action"}
    dumped = json.dumps(schema)
    for keyword in ("oneOf", "allOf", "patternProperties", "not"):
        assert f'"{keyword}"' not in dumped, keyword


# --- a schema-driven guard: omit each OPTIONAL field, one at a time --------------------------------
FULL = {
    "patch": {
        "workspace_id": "ws-1", "base_version": 0, "next_action": "SEARCH",
        "upsert_facts": [{
            "schema_version": 1, "kind": "fact", "id": "party", "key": "party", "value": "two",
            "source": "INFERENCE", "confirmation": "UNCONFIRMED", "hard": False, "scope": "trip",
            "evidence_ids": [], "expires_at": None, "depends_on": [],
        }],
        "remove_fact_ids": ["old"], "status": "EXPLORING",
    },
    "intent": "DISCOVERY", "tool": "search_places",
    "search": {"text_query": "quiet restaurant", "area": "Union Station", "language_code": "en",
               "region_code": "CA", "page_size": 3},
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


SCHEMA = FrameResult.model_json_schema()
PATHS = list(optional_paths(SCHEMA, FULL, SCHEMA.get("$defs", {})))


def test_the_full_payload_validates_and_the_walker_finds_the_optional_fields() -> None:
    assert validates(FULL)
    names = {p[-1] for p in PATHS}
    assert {"tool", "search", "area", "language_code", "region_code", "page_size",
            "upsert_facts", "remove_fact_ids", "status", "value", "evidence_ids", "expires_at",
            "depends_on", "schema_version", "kind"} <= names, names


@pytest.mark.parametrize("path", PATHS, ids=lambda p: ".".join(map(str, p)))
def test_omitting_one_optional_field_never_costs_a_repair(path) -> None:
    payload = without(FULL, path)
    if path == ("search",):
        # intended: a discovery reply without search arguments is not usable
        assert not validates(payload)
        assert validates({**payload, "intent": "OTHER", "tool": None})
        return
    assert validates(payload), f"omitting {'.'.join(map(str, path))} fails validation"


@pytest.mark.parametrize("key", ["patch", "intent"])
def test_omitting_a_required_field_does_fail(key) -> None:
    payload = dict(FULL)
    del payload[key]
    assert not validates(payload)


def test_the_prompt_does_not_ask_for_what_the_server_derives() -> None:
    assert "search_places" not in frame.FRAME_SYSTEM_PROMPT
    assert "tool" not in frame.FRAME_SYSTEM_PROMPT.lower()
    assert len(frame.FRAME_SYSTEM_PROMPT) < 1_300
    _ = Env
