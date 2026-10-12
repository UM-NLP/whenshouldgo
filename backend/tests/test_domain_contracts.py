from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain import (
    Confirmation,
    Evidence,
    Fact,
    FactSource,
    NextAction,
    PendingAction,
    PrincipalKind,
    WorkspacePatch,
)
from app.entitlements import PILOT_POLICY, Period


def test_pilot_token_policy_matches_decisions() -> None:
    t = PILOT_POLICY.tokens
    assert (t.max_user_message_tokens, t.planner_call_input_tokens, t.planner_call_output_tokens) == (
        2_000,
        12_000,
        1_200,
    )
    assert (t.run_input_tokens, t.run_output_tokens) == (40_000, 4_000)
    assert (t.run_model_calls, t.run_tool_calls, t.run_escalations) == (6, 12, 1)
    assert (t.classifier_input_tokens, t.classifier_output_tokens) == (2_048, 64)
    assert (t.visible_reply_tokens, t.spoken_reply_tokens) == (450, 160)
    guest = PILOT_POLICY.daily_tokens[PrincipalKind.GUEST]
    registered = PILOT_POLICY.daily_tokens[PrincipalKind.REGISTERED_FREE]
    assert (guest.input_tokens, guest.output_tokens) == (60_000, 6_000)
    assert (registered.input_tokens, registered.output_tokens) == (120_000, 12_000)
    assert PrincipalKind.DISABLED not in PILOT_POLICY.daily_tokens
    assert PILOT_POLICY.for_kind(PrincipalKind.REGISTERED_FREE).period == Period.ROLLING_30_DAYS


def test_patch_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WorkspacePatch(
            workspace_id="w", base_version=0, next_action=NextAction.ASK, owner_id="attacker"
        )


def _fact(**overrides) -> Fact:
    values = dict(
        id="f",
        key="return_by",
        value="18:00",
        source=FactSource.INFERENCE,
        confirmation=Confirmation.UNCONFIRMED,
        hard=False,
        scope="trip",
    )
    return Fact(**{**values, **overrides})


def test_inference_cannot_become_a_confirmed_hard_fact() -> None:
    with pytest.raises(ValidationError):
        _fact(hard=True, confirmation=Confirmation.CONFIRMED)
    # The same constraint is valid once the user states it explicitly.
    assert _fact(source=FactSource.USER_EXPLICIT, hard=True, confirmation=Confirmation.CONFIRMED)


def test_model_patch_cannot_smuggle_an_inferred_hard_fact() -> None:
    with pytest.raises(ValidationError):
        WorkspacePatch.model_validate(
            {
                "workspace_id": "w",
                "base_version": 0,
                "next_action": "VALIDATE",
                "upsert_facts": [
                    {
                        "id": "f",
                        "key": "return_by",
                        "source": "INFERENCE",
                        "confirmation": "CONFIRMED",
                        "hard": True,
                        "scope": "trip",
                    }
                ],
            }
        )


@pytest.mark.parametrize("missing", ["preview", "nonce", "idempotency_key", "expires_at", "target"])
def test_pending_action_requires_preview_nonce_expiry_and_idempotency(missing) -> None:
    values = dict(
        id="a",
        workspace_id="w",
        plan_version=1,
        effect="SET_REMINDER",
        target="device",
        preview="Remind me at 17:15 to leave",
        nonce="n",
        idempotency_key="i",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    values.pop(missing)
    with pytest.raises(ValidationError):
        PendingAction(**values)


def test_evidence_freshness_is_evaluated_against_a_supplied_clock() -> None:
    now = datetime(2026, 10, 11, 12, tzinfo=UTC)
    evidence = Evidence(
        id="e",
        tool="place_details",
        tool_version="1",
        source="places",
        retrieved_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=1),
        precision="exact",
    )
    assert evidence.is_fresh(now)
    assert not evidence.is_fresh(now + timedelta(minutes=2))
