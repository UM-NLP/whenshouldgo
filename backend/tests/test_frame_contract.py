"""The FRAME call: one bounded structured output, user-only context, a closed search schema."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from test_gateway_support import make_config, make_policy
from wiring_support import STATE_PREFIX, fact, search, wire_text

from app.domain import ContentClass, RuntimeConfig, TripWorkspace
from app.frame import (
    FRAME_OUTPUT_NAME,
    ArgumentRejected,
    FrameIntent,
    FrameProposal,
    SearchArgs,
    build_frame_request,
    search_arguments,
)
from app.gateway import CallRole, PrivacyClass, RunBudget, TaintedContextError
from app.gateway.policy import check_request

CONFIG = make_config()


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
    assert built.output_schema.model is FrameProposal and built.output_schema.name == FRAME_OUTPUT_NAME
    check_request(built, make_policy())


def test_largest_message_and_a_full_workspace_still_fit_the_planner_budget() -> None:
    from app.guard import estimate_tokens

    message = "x" * 8_000  # 2,000 estimated tokens: the user-message ceiling
    assert estimate_tokens(message) == 2_000
    facts = [fact(f"f{i}", "v" * 200) for i in range(50)]
    built = request(message, TripWorkspace(id="ws-1", owner_id="u1", facts=facts))
    check_request(built, make_policy())
    assert built.input_token_bound <= CONFIG.token_ceilings.planner_input_tokens


def test_workspace_state_message_has_user_facts_only_and_no_ids() -> None:
    base = TripWorkspace(id="ws-1", owner_id="owner-secret", version=2,
                         facts=[fact("party", "two adults")], goal="GOAL-TEXT")
    built = request(workspace=base)
    state_message = next(m for m in built.context if m.content.startswith(STATE_PREFIX))
    state = json.loads(state_message.content[len(STATE_PREFIX):])
    assert state == {"facts": [{"key": "party", "value": "two adults"}]}  # the model owns no id
    blob = wire_text(built)
    assert "owner-secret" not in blob and "GOAL-TEXT" not in blob and "ws-1" not in blob


def test_frame_proposal_is_closed_and_bounded() -> None:
    ok = FrameProposal(intent=FrameIntent.DISCOVERY, search=search())
    assert ok.search is not None and ok.search.language_code == "en" and ok.facts == []
    base = dict(intent=FrameIntent.DISCOVERY, search=search())
    for extra in ({"reply": "hello"}, {"urls": ["https://x"]}, {"allowed_tools": ["x"]},
                  {"patch": {}}, {"tool": "search_places"}, {"status": "CONFIRMED"}):
        with pytest.raises(ValidationError):
            FrameProposal(**base, **extra)
    # slips that carry no state are tolerated; the server degrades or ignores them
    FrameProposal(intent=FrameIntent.OTHER, search=search())
    FrameProposal(intent=FrameIntent.DISCOVERY, search=None)


@pytest.mark.parametrize("bad", [
    dict(text_query="x" * 501),  # the schema cap; the strict 80-character rule is the plan's
    dict(text_query="ok", region_code="CA"),  # Fix 9: no region is ever requested
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
                    "page_size": 3}
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
    class Sub(FrameProposal):
        pass

    from test_gateway_hardening import _schema_request

    check_request(_schema_request(CallRole.FRAME, FrameProposal), make_policy())
    for role in (CallRole.DISCOVER, CallRole.COMPOSE):
        with pytest.raises(TaintedContextError):
            check_request(_schema_request(role, FrameProposal), make_policy())
    with pytest.raises(TaintedContextError):
        check_request(_schema_request(CallRole.FRAME, Sub), make_policy())


def test_the_real_gateway_adapter_accepts_the_frame_schema_and_returns_a_proposal() -> None:
    from gateway_fakes import FakeTransport, completion_response
    from test_gateway_support import make_gateway

    answer = json.dumps({"intent": "DISCOVERY", "search": {"text_query": "brunch"}})
    transport = FakeTransport([completion_response(answer)])
    turn = make_gateway(transport).generate(request())
    assert isinstance(turn.output, FrameProposal) and turn.output.search is not None
    assert turn.output.search.text_query == "brunch" and turn.call_role == CallRole.FRAME
    sent = json.dumps(transport.requests[0].body)
    assert "tool_calls" not in sent and "WORKSPACE_STATE_JSON" in sent
