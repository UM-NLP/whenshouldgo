import pytest
from pydantic import ValidationError

from app.contracts import NextAction, WorkspacePatch
from app.policy import CURRENT_POLICY


def test_policy_matches_decisions():
    t = CURRENT_POLICY.tokens
    assert (t.max_user_message_tokens, t.planner_call_input_tokens, t.planner_call_output_tokens) == (2_000, 12_000, 1_200)
    assert (t.run_input_tokens, t.run_output_tokens, t.run_model_calls, t.run_tool_calls, t.run_escalations) == (40_000, 4_000, 6, 12, 1)
    assert (CURRENT_POLICY.guest.user_turns, CURRENT_POLICY.guest.activated_trips) == (50, 10)
    assert CURRENT_POLICY.registered_free.period_days == 30


def test_patch_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        WorkspacePatch(workspace_id="w", base_version=0, next_action=NextAction.ASK, owner_id="attacker")
