"""Strict WorkspacePatch validation: the model proposes, the server decides.

A model fact is only ever an unconfirmed, soft inference. Hard or confirmed facts come from
explicit structured user input (the request model has none yet), so a patch can never create
one. Every rejection is typed and leaves the workspace exactly as it was.
"""
from __future__ import annotations

import pytest
from wiring_support import fact

from app.domain import (
    Confirmation,
    FactSource,
    NextAction,
    TripWorkspace,
    WorkspacePatch,
    WorkspaceStatus,
)
from app.patching import MAX_FACT_VALUE_CHARS, MAX_UPSERTS, PatchRejected, apply_patch
from app.telemetry import ValidationReason

USER_FACT = fact("budget", "under 50", source=FactSource.USER_EXPLICIT,
                 confirmation=Confirmation.CONFIRMED, hard=True)
ASSUMPTION = fact("planning_area", "Toronto/GTA")


def workspace(*facts, version: int = 3, status=WorkspaceStatus.EXPLORING) -> TripWorkspace:
    return TripWorkspace(id="ws-1", owner_id="u1", version=version, status=status, facts=list(facts))


def patch(**overrides) -> WorkspacePatch:
    fields = dict(workspace_id="ws-1", base_version=3, next_action=NextAction.SEARCH)
    fields.update(overrides)
    return WorkspacePatch(**fields)


def rejected(base: TripWorkspace, proposal: WorkspacePatch) -> PatchRejected:
    before = base.model_dump_json()
    with pytest.raises(PatchRejected) as caught:
        apply_patch(base, proposal)
    assert base.model_dump_json() == before  # nothing changed
    return caught.value


def test_inferred_soft_fact_is_applied_to_a_copy_and_goal_is_untouched() -> None:
    base = workspace(ASSUMPTION)
    result = apply_patch(base, patch(upsert_facts=[fact("party", "two adults")]))
    assert result is not base and base.facts == [ASSUMPTION]
    assert {f.id for f in result.facts} == {"planning_area", "party"}  # a pre-existing fact stays
    assert result.goal is None and result.version == 4
    added = next(f for f in result.facts if f.id == "party")
    assert (added.source, added.confirmation, added.hard) == (
        FactSource.INFERENCE, Confirmation.UNCONFIRMED, False)


def test_no_workspace_is_given_a_planning_area_and_an_existing_one_is_kept() -> None:
    first = apply_patch(workspace(), patch())
    assert first.facts == []  # Fix 10: the product assumes no place
    kept = apply_patch(workspace(ASSUMPTION), patch())
    assert [f.id for f in kept.facts] == ["planning_area"]


@pytest.mark.parametrize("proposed", [
    fact(source=FactSource.USER_EXPLICIT),  # a model cannot claim the user said it
    fact(source=FactSource.USER_PROSE),
    fact(source=FactSource.TOOL),
    fact(source=FactSource.MEMORY),
    fact(confirmation=Confirmation.CONFIRMED),
    fact(hard=True),
    fact(source=FactSource.USER_EXPLICIT, confirmation=Confirmation.CONFIRMED, hard=True),
])
def test_model_cannot_create_confirmed_hard_or_non_inferred_facts(proposed) -> None:
    error = rejected(workspace(ASSUMPTION), patch(upsert_facts=[proposed]))
    assert error.reason == ValidationReason.HARD_CONSTRAINT


@pytest.mark.parametrize("overrides", [
    {"evidence_ids": ("ev-1",)},
    {"depends_on": ("other",)},
    {"expires_at": "2030-01-01T00:00:00+00:00"},
])
def test_model_facts_carry_no_evidence_dependency_or_expiry(overrides) -> None:
    error = rejected(workspace(), patch(upsert_facts=[fact(**overrides)]))
    assert error.reason == ValidationReason.SCHEMA


def test_user_facts_cannot_be_overwritten_or_removed_by_the_model() -> None:
    base = workspace(ASSUMPTION, USER_FACT)
    overwrite = fact("budget", "unlimited")  # soft inference over a confirmed hard fact
    assert rejected(base, patch(upsert_facts=[overwrite])).reason == ValidationReason.HARD_CONSTRAINT
    assert rejected(base, patch(remove_fact_ids=["budget"])).reason == ValidationReason.HARD_CONSTRAINT


def test_server_owned_planning_area_cannot_be_changed_by_the_model() -> None:
    base = workspace(ASSUMPTION)
    assert rejected(base, patch(upsert_facts=[fact("planning_area", "Paris")])).reason == (
        ValidationReason.HARD_CONSTRAINT)
    assert rejected(base, patch(remove_fact_ids=["planning_area"])).reason == (
        ValidationReason.HARD_CONSTRAINT)


def test_existing_inference_may_be_updated_or_removed() -> None:
    base = workspace(ASSUMPTION, fact("party", "two adults"))
    result = apply_patch(base, patch(upsert_facts=[fact("party", "three adults")], remove_fact_ids=[]))
    assert next(f for f in result.facts if f.id == "party").value == "three adults"
    result = apply_patch(base, patch(remove_fact_ids=["party"]))
    assert {f.id for f in result.facts} == {"planning_area"}


def test_size_and_count_limits() -> None:
    many = [fact(f"f{i}", "v") for i in range(MAX_UPSERTS + 1)]
    assert rejected(workspace(), patch(upsert_facts=many)).reason == ValidationReason.OVERSIZE
    big = fact("note", "x" * (MAX_FACT_VALUE_CHARS + 1))
    assert rejected(workspace(), patch(upsert_facts=[big])).reason == ValidationReason.OVERSIZE
    full = workspace(*[fact(f"e{i}", "v") for i in range(50)])
    assert rejected(full, patch(upsert_facts=[fact("one-more", "v")])).reason == ValidationReason.OVERSIZE


def test_duplicate_conflicting_and_unknown_operations_are_rejected() -> None:
    base = workspace(fact("party", "two adults"))
    dup = [fact("a", "1"), fact("a", "2")]
    assert rejected(base, patch(upsert_facts=dup)).reason == ValidationReason.SCHEMA
    both = patch(upsert_facts=[fact("party", "x")], remove_fact_ids=["party"])
    assert rejected(base, both).reason == ValidationReason.SCHEMA
    assert rejected(base, patch(remove_fact_ids=["no-such-fact"])).reason == ValidationReason.UNKNOWN_FIELD


def test_wrong_workspace_or_stale_base_version_is_rejected() -> None:
    base = workspace()
    assert rejected(base, patch(workspace_id="someone-elses")).reason == ValidationReason.OTHER
    assert rejected(base, patch(base_version=2)).reason == ValidationReason.OTHER
    assert rejected(base, patch(base_version=4)).reason == ValidationReason.OTHER


@pytest.mark.parametrize("action", [
    NextAction.CONFIRM, NextAction.PROPOSE_ACTION, NextAction.SOLVE, NextAction.ESTIMATE_TRAVEL,
    NextAction.VALIDATE, NextAction.LOOK_UP_DETAILS,
])
def test_next_actions_that_would_confirm_or_run_unwired_tools_are_rejected(action) -> None:
    assert rejected(workspace(), patch(next_action=action)).reason == ValidationReason.UNKNOWN_FIELD


@pytest.mark.parametrize("status", [
    WorkspaceStatus.PROPOSED, WorkspaceStatus.CONFIRMED, WorkspaceStatus.ARMED,
    WorkspaceStatus.ACTIVE, WorkspaceStatus.COMPLETED, WorkspaceStatus.CANCELLED,
])
def test_model_cannot_move_a_workspace_past_exploring(status) -> None:
    assert rejected(workspace(), patch(status=status)).reason == ValidationReason.UNKNOWN_FIELD


def test_model_cannot_change_the_status_of_a_workspace_past_exploring() -> None:
    base = workspace(status=WorkspaceStatus.CONFIRMED)
    assert rejected(base, patch(status=WorkspaceStatus.EXPLORING)).reason == (
        ValidationReason.UNKNOWN_FIELD)


def test_framing_and_exploring_are_the_only_statuses_a_model_may_ask_for() -> None:
    result = apply_patch(workspace(status=WorkspaceStatus.CAPTURED),
                         patch(status=WorkspaceStatus.FRAMING))
    assert result.status == WorkspaceStatus.EXPLORING  # the server decides the resulting state
    kept = apply_patch(workspace(status=WorkspaceStatus.CONFIRMED), patch())
    assert kept.status == WorkspaceStatus.CONFIRMED  # never demoted either


def test_rejection_message_is_static_and_carries_no_model_text() -> None:
    secret = "SECRET-MODEL-TEXT"
    error = rejected(workspace(), patch(upsert_facts=[fact(value=secret, hard=True)]))
    assert secret not in str(error) and secret not in repr(error)
