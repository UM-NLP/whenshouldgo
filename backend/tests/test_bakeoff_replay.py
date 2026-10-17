"""Bake-off production replay: what production does with a FrameProposal, scored offline with the
real plan_search, build_patch and apply_patch_checked. Plus the regression for the earlier flaw
(the model writing workspace state), which the strict schema now turns into MALFORMED."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.frame import FrameIntent, FrameProposal, ProposedFact, SearchArgs
from app.gateway import HttpxTransport
from evals.frame_bakeoff import run

FX = {"id": "t-1", "category": "discovery", "message": "Quiet cafe near Union Station tonight",
      "expected_intent": "DISCOVERY", "expected_area": "Union Station",
      "required_query_words": [["cafe"]], "forbidden_query_words": ["password"],
      "expected_language": "en", "must_not": [], "notes": ""}


def proposal(**over: Any) -> FrameProposal:
    base: dict[str, Any] = {"intent": FrameIntent.DISCOVERY,
                            "search": SearchArgs(text_query="quiet cafe", area="Union Station")}
    base.update(over)
    return FrameProposal(**base)


def test_discovery_search_with_the_final_arguments_and_requirements() -> None:
    r = run.replay_production(proposal(), FX["message"], FX)
    assert r["turn_outcome"] == "DISCOVERY_SEARCH"
    assert r["plan"]["outcome"] == "accepted" and r["plan"]["area_dropped"] is False
    assert r["plan"]["arguments"]["text_query"] == "quiet cafe near Union Station"
    assert r["patch"]["outcome"] == "applied" and r["patch"]["facts_dropped"] == 0
    assert r["requirements"] == {"required_words": True, "forbidden_words": True,
                                 "area_in_message": True, "area_expected": True,
                                 "area_absent_ok": True}


def test_requirements_flag_missing_words_forbidden_words_and_a_forbidden_area() -> None:
    fx = {**FX, "required_query_words": [["bakery"]], "must_not": ["any_area"]}
    r = run.replay_production(proposal(), FX["message"], fx)
    assert r["requirements"]["required_words"] is False
    assert r["requirements"]["area_absent_ok"] is False
    leak = proposal(search=SearchArgs(text_query="quiet cafe password", area="Union Station"))
    msg = "Quiet cafe near Union Station tonight password"
    assert run.replay_production(leak, msg, FX)["requirements"]["forbidden_words"] is False


def test_discovery_without_any_area_asks_for_one_and_does_not_search() -> None:
    p = proposal(search=SearchArgs(text_query="quiet cafe"))
    r = run.replay_production(p, "a quiet cafe please", FX)
    assert r["turn_outcome"] == "CLARIFY_NEEDS_AREA" and r["plan"]["outcome"] == "accepted"
    assert r["plan"]["has_area"] is False
    assert r["patch"]["outcome"] == "applied" and r["requirements"] is None


def test_other_ignores_a_search_and_clarifies() -> None:
    r = run.replay_production(proposal(intent=FrameIntent.OTHER), FX["message"], FX)
    assert r["turn_outcome"] == "CLARIFY_OTHER" and r["plan"]["outcome"] == "ignored"
    assert r["requirements"] is None
    plain = run.replay_production(proposal(intent=FrameIntent.OTHER, search=None), "hello", FX)
    assert plain["turn_outcome"] == "CLARIFY_OTHER" and plain["plan"]["outcome"] == "none"


def test_discovery_with_a_bad_or_missing_query_degrades_to_the_fixed_clarification() -> None:
    bad = proposal(search=SearchArgs(text_query="museum bitcoin wallet seed"))
    r = run.replay_production(bad, FX["message"], FX)
    assert r["turn_outcome"] == "CLARIFY_QUERY_REJECTED" and r["plan"]["outcome"] == "rejected"
    assert r["patch"]["outcome"] == "applied" and r["requirements"] is None
    none = run.replay_production(proposal(search=None), FX["message"], FX)
    assert none["turn_outcome"] == "CLARIFY_QUERY_REJECTED" and none["plan"]["outcome"] == "rejected"


def test_a_dropped_area_and_dropped_facts_are_recorded_but_never_values() -> None:
    p = proposal(search=SearchArgs(text_query="quiet cafe", area="30 minute drive"),
                 facts=[ProposedFact(key="note", value="see http://evil.invalid/x"),
                        ProposedFact(key="party", value="SECRET-VALUE-4")])
    r = run.replay_production(p, "Quiet cafe within a 30 minute drive", FX)
    # Fix 9: a dropped area leaves no place, so production asks for one instead of searching.
    assert r["turn_outcome"] == "CLARIFY_NEEDS_AREA" and r["plan"]["area_dropped"] is True
    assert r["plan"]["has_area"] is False and r["requirements"] is None
    assert "near" not in r["plan"]["arguments"]["text_query"]
    assert r["patch"]["facts_dropped"] == 1 and r["patch"]["fact_keys"] == ["note", "party"]
    assert "SECRET-VALUE-4" not in json.dumps(r) and "evil.invalid" not in json.dumps(r)


def test_patch_rejected_is_its_own_turn_outcome_and_wins_over_intent(monkeypatch) -> None:
    from app.patching import PatchRejected
    from app.telemetry import ValidationReason

    def boom(workspace: Any, patch: Any) -> Any:
        raise PatchRejected(ValidationReason.HARD_CONSTRAINT)

    monkeypatch.setattr(run, "apply_patch_checked", boom)
    r = run.replay_production(proposal(), FX["message"], FX)
    assert r["turn_outcome"] == "PATCH_REJECTED" and r["patch"]["outcome"] == "rejected"
    assert r["patch"]["reason"] == ValidationReason.HARD_CONSTRAINT.value
    other = run.replay_production(proposal(intent=FrameIntent.OTHER, search=None), "hi", FX)
    assert other["turn_outcome"] == "PATCH_REJECTED"


def test_the_real_patch_validator_runs_on_a_fresh_workspace() -> None:
    # a server-owned fact key cannot be shadowed by a proposed fact
    p = proposal(facts=[ProposedFact(key="planning_area", value="Mars")])
    r = run.replay_production(p, FX["message"], FX)
    assert r["patch"]["outcome"] == "rejected" or r["patch"]["facts_dropped"] == 1


# --- regression: the earlier flaw (model-written state) can no longer pass ------------------------
def _stack(content: str) -> run.Stack:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 250, "completion_tokens": 55, "total_tokens": 305},
                "provider": "prov1"}
        return httpx.Response(200, json=body)

    transport = HttpxTransport.for_tests(httpx.MockTransport(handler))
    return run.build_stack("vendor/model-x", "prov1", Decimal("1"), Decimal("2"), "sk-test-key-123456",
                           transport)


@pytest.mark.parametrize("content", [
    '{"intent": "OTHER", "patch": {"status": "CONFIRMED"}}',
    '{"intent": "OTHER", "status": "CONFIRMED"}',
    '{"intent": "DISCOVERY", "search": {"text_query": "cafe"}, "patch": {"next_action": "SEARCH"}}',
    '{"intent": "DISCOVERY", "search": {"text_query": "cafe"}, "tool": "search_places"}',
    '{"intent": "OTHER", "workspace_id": "ws-1", "base_version": 0}',
])
def test_state_fields_the_schema_no_longer_has_are_malformed(content: str) -> None:
    record = run.run_one(_stack(content), FX, 1)
    assert record["outcome"] == "VALIDATION_FAILED" and record["turn_outcome"] == "MALFORMED"
    assert record["intent"] is None and record["model_calls"] == 1
    assert Decimal(record["cost_usd"]) > 0  # the failed call keeps its cost


@pytest.mark.parametrize("fact", [
    '{"key": "k", "value": "v", "hard": true}',
    '{"key": "k", "value": "v", "confirmation": "CONFIRMED"}',
    '{"key": "k", "value": "v", "source": "USER_EXPLICIT"}',
])
def test_a_hard_or_confirmed_fact_cannot_be_proposed(fact: str) -> None:
    record = run.run_one(_stack('{"intent": "OTHER", "facts": [' + fact + "]}"), FX, 1)
    assert record["turn_outcome"] == "MALFORMED" and record["patch"] is None


def test_a_clean_proposal_through_the_real_gateway_is_replayed_with_keys_only() -> None:
    content = ('{"intent": "DISCOVERY", "search": {"text_query": "quiet cafe", "area": "Union Station"},'
               ' "facts": [{"key": "mood", "value": "quiet"}]}')
    record = run.run_one(_stack(content), FX, 1)
    assert record["outcome"] == "OK" and record["intent"] == "DISCOVERY"
    assert record["turn_outcome"] == "DISCOVERY_SEARCH" and record["repairs"] == 0
    assert record["patch"]["fact_keys"] == ["mood"] and "quiet" not in json.dumps(record["patch"])
    assert record["search"]["text_query"] == "quiet cafe" and record["search"]["area"] == "Union Station"
    assert "message" not in record and "facts" not in record


@pytest.mark.parametrize("error_kind_status,turn", [(500, "PROVIDER_ERROR")])
def test_an_infrastructure_failure_is_a_provider_error_not_malformed(error_kind_status: int, turn: str) -> None:
    transport = HttpxTransport.for_tests(
        httpx.MockTransport(lambda r: httpx.Response(error_kind_status, json={"error": {}})))
    stack = run.build_stack("vendor/model-x", "prov1", Decimal("1"), Decimal("2"), "sk-test-key-123456",
                            transport)
    assert run.run_one(stack, FX, 1)["turn_outcome"] == turn
