"""The FRAME call: one bounded structured output, user-only context, a closed search schema."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from test_gateway_support import make_config, make_policy
from wiring_support import STATE_PREFIX, fact, search, wire_text

from app.discovery import ToolId
from app.domain import (
    ContentClass,
    NextAction,
    RuntimeConfig,
    TripWorkspace,
    WorkspacePatch,
)
from app.frame import (
    FRAME_OUTPUT_NAME,
    ArgumentRejected,
    FrameIntent,
    FrameResult,
    SearchArgs,
    build_frame_request,
    search_arguments,
)
from app.gateway import CallRole, PrivacyClass, RunBudget, TaintedContextError
from app.gateway.policy import check_request

CONFIG = make_config()


def patch() -> WorkspacePatch:
    return WorkspacePatch(workspace_id="ws-1", base_version=0, next_action=NextAction.SEARCH)


def request(message: str = "brunch near Leslieville", workspace: TripWorkspace | None = None):
    base = workspace or TripWorkspace(id="ws-1", owner_id="u1")
    return build_frame_request(
        message, base, CONFIG, RunBudget.from_ceilings(CONFIG.token_ceilings)
    )


def test_runtime_config_flag_defaults_to_false_and_is_frozen() -> None:
    assert CONFIG.maps_content_to_model is False
    with pytest.raises(ValidationError):
        CONFIG.maps_content_to_model = True  # type: ignore[misc]
    assert make_config(maps_content_to_model=True).maps_content_to_model is True
    assert RuntimeConfig.model_fields["maps_content_to_model"].default is False


def test_frame_request_is_user_only_frame_role_and_passes_the_gateway_checks() -> None:
    built = request()
    assert built.call_role == CallRole.FRAME
    assert built.privacy_class == PrivacyClass.USER_PRIVATE
    assert {m.content_class for m in built.context} <= {ContentClass.USER, ContentClass.NONE}
    assert built.allowed_tools == ()  # the model proposes arguments; it is never handed a tool
    assert built.output_schema.model is FrameResult and built.output_schema.name == FRAME_OUTPUT_NAME
    check_request(built, make_policy())


def test_largest_message_and_a_full_workspace_still_fit_the_planner_budget() -> None:
    from app.guard import estimate_tokens

    message = "x" * 8_000  # 2,000 estimated tokens: the user-message ceiling
    assert estimate_tokens(message) == 2_000
    facts = [fact(f"f{i}", "v" * 200) for i in range(50)]
    built = request(message, TripWorkspace(id="ws-1", owner_id="u1", facts=facts))
    check_request(built, make_policy())
    assert built.input_token_bound <= CONFIG.token_ceilings.planner_input_tokens


def test_workspace_state_message_has_ids_and_user_facts_only() -> None:
    base = TripWorkspace(id="ws-1", owner_id="owner-secret", version=2,
                         facts=[fact("party", "two adults")], goal="GOAL-TEXT")
    built = request(workspace=base)
    state_message = next(m for m in built.context if m.content.startswith(STATE_PREFIX))
    state = json.loads(state_message.content[len(STATE_PREFIX):])
    assert state["workspace_id"] == "ws-1" and state["base_version"] == 2
    assert [f["id"] for f in state["facts"]] == ["party"]
    blob = wire_text(built)
    assert "owner-secret" not in blob and "GOAL-TEXT" not in blob  # no owner id, no stored goal


def test_frame_result_is_closed_and_bounded() -> None:
    ok = FrameResult(patch=patch(), intent=FrameIntent.DISCOVERY, tool=ToolId.SEARCH_PLACES,
                     search=search())
    assert ok.search is not None and ok.search.region_code == "CA"
    base = dict(patch=patch(), intent=FrameIntent.DISCOVERY, tool=ToolId.SEARCH_PLACES,
                search=search())
    for extra in ({"reply": "hello"}, {"urls": ["https://x"]}, {"allowed_tools": ["x"]}):
        with pytest.raises(ValidationError):
            FrameResult(**base, **extra)
    for bad in (
        dict(intent=FrameIntent.OTHER),  # OTHER carries no tool
        dict(search=None),  # search_places needs arguments
        dict(tool=ToolId.RESOLVE_NAMES),  # other tools carry no search arguments here
    ):
        with pytest.raises(ValidationError):
            FrameResult(**{**base, **bad})


@pytest.mark.parametrize("bad", [
    dict(text_query=""),
    dict(text_query="x" * 201),
    dict(text_query="line one\nline two"),
    dict(text_query="zero​width"),
    dict(text_query="see https://evil.example/x"),
    dict(text_query="<script>alert(1)</script>"),
    dict(text_query="ok", region_code="US"),
    dict(text_query="ok", area="y" * 201),  # only a loose structural cap; the rest is forgiving
    dict(text_query="ok", page_size=0),
    dict(text_query="ok", page_size=6),
    dict(text_query="ok", location_bias={"circle": {}}),
    dict(text_query="ok", api_key="k"),
])
def test_search_args_are_a_closed_bounded_model(bad) -> None:
    with pytest.raises(ValidationError):
        SearchArgs(**bad)


def test_search_arguments_are_built_from_the_closed_model_only() -> None:
    args = search_arguments(search("brunch", area="Leslieville", page_size=3),
                            "any brunch in Leslieville please")
    assert args == {"text_query": "brunch near Leslieville", "language_code": "en",
                    "region_code": "CA", "page_size": 3}
    assert search_arguments(search("brunch"), "brunch")["text_query"] == "brunch"
    assert "page_size" not in search_arguments(search("brunch"), "brunch")


def test_area_must_come_from_the_users_own_words() -> None:
    # an area the user never wrote is DROPPED (never an error); the query still goes out
    assert search_arguments(search("brunch", area="Paris"), "brunch in Leslieville")[
        "text_query"] == "brunch"
    # case-insensitive, whitespace-tolerant match against the user's text
    assert search_arguments(search("brunch", area="leslieville"), "Brunch in  Leslieville")[
        "text_query"] == "brunch near leslieville"
    _ = ArgumentRejected


def test_frame_state_allowlist_admits_only_exact_classes() -> None:
    class Sub(FrameResult):
        pass

    from test_gateway_hardening import _schema_request

    check_request(_schema_request(CallRole.FRAME, FrameResult), make_policy())
    for role in (CallRole.DISCOVER, CallRole.COMPOSE):
        with pytest.raises(TaintedContextError):
            check_request(_schema_request(role, FrameResult), make_policy())
    with pytest.raises(TaintedContextError):
        check_request(_schema_request(CallRole.FRAME, Sub), make_policy())


def test_the_real_gateway_adapter_accepts_the_frame_schema_and_returns_a_frame_result() -> None:
    from gateway_fakes import FakeTransport, completion_response
    from test_gateway_support import make_gateway

    answer = json.dumps({
        "patch": {"workspace_id": "ws-1", "base_version": 0, "next_action": "SEARCH"},
        "intent": "DISCOVERY", "tool": "search_places", "search": {"text_query": "brunch"},
    })
    transport = FakeTransport([completion_response(answer)])
    turn = make_gateway(transport).generate(request())
    assert isinstance(turn.output, FrameResult) and turn.output.search is not None
    assert turn.output.search.text_query == "brunch" and turn.call_role == CallRole.FRAME
    sent = json.dumps(transport.requests[0].body)
    assert "tool_calls" not in sent and "WORKSPACE_STATE_JSON" in sent
