"""Phase 0 contract freeze: evidence handling, plan/history/consent schemas, runtime config.

All fixtures are synthetic; no Maps content appears anywhere.
"""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth import StaticTokenVerifier, VerifiedIdentity
from app.domain import (
    ConsentRecord,
    ErrorBody,
    Evidence,
    OutingRecord,
    PendingAction,
    PendingActionEffect,
    PlanVersion,
    PhaseFlags,
    RuntimeConfig,
    RuntimeTTLs,
    TokenCeilings,
    TripWorkspace,
    TurnResponse,
)
from app.main import create_app

T0 = datetime(2026, 10, 10, 15, tzinfo=UTC)  # a Saturday


def evidence(**overrides) -> Evidence:
    values = dict(
        id="e1",
        tool="grounded_search",
        tool_version="1",
        source="synthetic",
        retrieved_at=T0,
        expires_at=T0 + timedelta(hours=1),
        precision="approx",
        kind="GROUNDED_ANSWER",
        content_class="MAPS_GROUNDED",
        llm_visible=True,
        storage_class="DISPLAY_CACHE",
        attribution_required=True,
        cache_expires_at=T0 + timedelta(hours=24),
        tool_call_id="call-1",
        content_hash="a" * 64,
    )
    return Evidence(**{**values, **overrides})


def test_evidence_cache_expiry_is_bounded_to_30_days() -> None:
    assert evidence(cache_expires_at=T0 + timedelta(days=30))
    with pytest.raises(ValidationError):
        evidence(cache_expires_at=T0 + timedelta(days=30, seconds=1))
    with pytest.raises(ValidationError):
        evidence(cache_expires_at=T0 - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        evidence(cache_expires_at=None)  # DISPLAY_CACHE needs an expiry


@pytest.mark.parametrize(
    ("kind", "content_class"),
    [("PLACES_DIRECT", "MAPS_PLACES"), ("ROUTE_DIRECT", "MAPS_ROUTE")],
)
def test_direct_maps_evidence_is_never_llm_visible(kind, content_class) -> None:
    ok = dict(
        kind=kind,
        content_class=content_class,
        storage_class="EPHEMERAL",
        cache_expires_at=None,
        place_id="synthetic-place",
    )
    assert evidence(llm_visible=False, **ok)
    with pytest.raises(ValidationError):
        evidence(llm_visible=True, **ok)


@pytest.mark.parametrize(
    ("content_class", "kind", "storage_class"),
    [
        ("MAPS_PLACES", "PLACES_DIRECT", "DISPLAY_CACHE"),
        ("MAPS_ROUTE", "ROUTE_DIRECT", "DISPLAY_CACHE"),
        ("MAPS_ROUTE", "ROUTE_DIRECT", "PLACE_ID_ONLY"),
    ],
)
def test_maps_content_cannot_use_a_persisting_storage_class(content_class, kind, storage_class) -> None:
    with pytest.raises(ValidationError):
        evidence(
            kind=kind,
            content_class=content_class,
            storage_class=storage_class,
            llm_visible=False,
            place_id="synthetic-place",
        )


def test_maps_evidence_needs_attribution_and_consistent_class() -> None:
    with pytest.raises(ValidationError):
        evidence(attribution_required=False)
    with pytest.raises(ValidationError):
        evidence(content_class="USER")  # GROUNDED_ANSWER must be MAPS_GROUNDED
    with pytest.raises(ValidationError):
        evidence(tool_call_id=None)
    with pytest.raises(ValidationError):
        evidence(content_hash="not-a-hash")


def test_evidence_has_no_content_field_and_rejects_extras() -> None:
    names = set(Evidence.model_fields)
    assert not names & {"content", "text", "body", "answer", "payload", "raw"}
    with pytest.raises(ValidationError):
        evidence(text="synthetic words")


def plan_version(**overrides) -> PlanVersion:
    values = dict(
        id="p2",
        workspace_id="w",
        version=2,
        parent_version=1,
        replaced_reason="USER_EDIT",
        destination={"place_id": "synthetic-place", "user_label": "my usual"},
        origin_ref="origin-0001",
        user_constraint_fact_ids=("f1",),
        grounded_evidence_id="e1",
        assumption_fact_ids=("f2",),
        warnings=("ROUTE_UNVERIFIED",),
        created_at=T0,
    )
    return PlanVersion(**{**values, **overrides})


def test_plan_version_is_immutable_and_lineage_checked() -> None:
    plan = plan_version()
    with pytest.raises(ValidationError):
        plan.version = 3
    with pytest.raises(ValidationError):
        plan_version(parent_version=2)
    with pytest.raises(ValidationError):
        plan_version(parent_version=None)  # replaced_reason without a parent
    with pytest.raises(ValidationError):
        plan_version(name="Synthetic Cafe")


def test_plan_version_field_set_cannot_hold_maps_content() -> None:
    assert set(PlanVersion.model_fields) == {
        "schema_version", "kind", "id", "workspace_id", "version", "parent_version",
        "replaced_reason", "destination", "origin_ref", "user_constraint_fact_ids",
        "grounded_evidence_id", "route_evidence_id", "assumption_fact_ids", "warnings",
        "created_at",
    }  # fmt: skip
    from app.domain import PlanDestination

    assert set(PlanDestination.model_fields) == {"place_id", "user_label"}
    with pytest.raises(ValidationError):
        plan_version(warnings=("free text that could carry provider content",))


def outing(**overrides) -> OutingRecord:
    values = dict(
        id="o1",
        owner_id="u1",
        workspace_id="w",
        plan_version=1,
        occurred_at=T0,
        tz="America/Toronto",
        local_dow=5,
        local_daypart="AFTERNOON",
        place_ref={"place_id": "synthetic-place", "id_checked_at": T0},
        user_label="my usual",
        intent_tag="FOOD_DRINK",
        events=({"type": "SELECTED", "at": T0},),
        strength="HANDOFF",
        consent_id="c1",
        retention_expires_at=T0 + timedelta(days=180),
    )
    return OutingRecord(**{**values, **overrides})


def test_outing_record_field_set_is_frozen_and_extras_rejected() -> None:
    assert set(OutingRecord.model_fields) == {
        "schema_version", "kind", "id", "owner_id", "workspace_id", "plan_version",
        "occurred_at", "tz", "local_dow", "local_daypart", "place_ref", "user_label",
        "intent_tag", "events", "feedback", "strength", "consent_id", "retention_expires_at",
    }  # fmt: skip
    record = outing()
    assert record.schema_version == 1 and record.kind == "outing_record"
    for extra in ("name", "address", "category", "latitude", "lat", "lng", "rating", "hours"):
        with pytest.raises(ValidationError):
            outing(**{extra: "synthetic"})
    with pytest.raises(ValidationError):
        outing(place_ref={"place_id": "p", "name": "Synthetic Cafe"})
    with pytest.raises(ValidationError):
        record.user_label = "changed"


def test_outing_record_derivations_and_visit_rule() -> None:
    with pytest.raises(ValidationError):
        outing(local_dow=2)  # Saturday in Toronto
    with pytest.raises(ValidationError):
        outing(tz="Not/AZone")
    with pytest.raises(ValidationError):
        outing(occurred_at=datetime(2026, 10, 10, 11))  # naive
    with pytest.raises(ValidationError):
        outing(retention_expires_at=T0)
    # A handoff is not proof of a visit; a visit exists only when user-confirmed.
    with pytest.raises(ValidationError):
        outing(strength="SELF_REPORTED_VISIT")
    with pytest.raises(ValidationError):
        outing(events=({"type": "VISITED_CONFIRMED", "at": T0},), strength="HANDOFF")
    assert outing(events=({"type": "VISITED_CONFIRMED", "at": T0},), strength="SELF_REPORTED_VISIT")


def consent(**overrides) -> ConsentRecord:
    values = dict(
        id="c1",
        owner_id="u1",
        purpose="HISTORY_STORE",
        notice_version="n1",
        processors=("GOOGLE", "OPENROUTER"),
        scope={"retention_days": 180, "channels": ()},
        granted_at=T0,
        context="settings",
    )
    return ConsentRecord(**{**values, **overrides})


def test_consent_record_contract() -> None:
    record = consent()
    assert record.kind == "consent_record" and record.schema_version == 1
    with pytest.raises(ValidationError):
        record.withdrawn_at = T0
    with pytest.raises(ValidationError):
        consent(purpose="BACKGROUND_LOCATION")
    with pytest.raises(ValidationError):
        consent(processors=())
    with pytest.raises(ValidationError):
        consent(withdrawn_at=T0 - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        consent(unexpected="x")


def runtime_config(**overrides) -> RuntimeConfig:
    values = dict(
        version="rc-1",
        model_id="synthetic/model",
        provider="synthetic-host",
        provider_allowlist=("synthetic-host",),
        price_table_version="prices-0",
        prompt_version_hashes=({"name": "frame", "sha256": "b" * 64},),
        tool_schema_versions=({"name": "search", "version": "1"},),
        classifier_version="kw-1",
        policy_version="2026-10-11",
        entitlement_version="2026-10-11",
        token_ceilings=TokenCeilings(),
        ttls=RuntimeTTLs(),
        flags=PhaseFlags(),
    )
    return RuntimeConfig(**{**values, **overrides})


def test_runtime_config_defaults_match_decisions_and_later_phases_are_off() -> None:
    config = runtime_config()
    ceilings = config.token_ceilings
    assert (ceilings.run_model_calls, ceilings.run_tool_calls, ceilings.run_escalations) == (6, 12, 1)
    assert (ceilings.run_input_tokens, ceilings.run_output_tokens) == (40_000, 4_000)
    assert config.ttls.grounded_cache_hours == 24 and config.ttls.outing_retention_days == 180
    assert not (
        config.flags.phase1_history or config.flags.phase2_patterns or config.flags.phase3_notifications
    )
    with pytest.raises(ValidationError):
        config.version = "rc-2"
    with pytest.raises(ValidationError):
        runtime_config(prompt_version_hashes=({"name": "frame", "sha256": "short"},))
    with pytest.raises(ValidationError):
        runtime_config(ttls={"grounded_cache_hours": 24 * 30 + 1})


def test_pending_action_effects_reject_unknown() -> None:
    assert {e.value for e in PendingActionEffect} == {"SET_REMINDER", "LAUNCH_HANDOFF"}
    values = dict(
        id="a", workspace_id="w", plan_version=1, target="t", preview="p", nonce="n" * 16,
        idempotency_key="idem-key-1", created_at=T0, expires_at=T0 + timedelta(minutes=5),
    )  # fmt: skip
    assert PendingAction(effect="SET_REMINDER", **values).kind == "pending_action"
    with pytest.raises(ValidationError):
        PendingAction(effect="BUY_TICKETS", **values)


def test_persisted_contracts_carry_schema_version_and_kind() -> None:
    workspace = TripWorkspace(id="w", owner_id="u")
    assert (workspace.schema_version, workspace.kind) == (1, "trip_workspace")
    assert (plan_version().schema_version, plan_version().kind) == (1, "plan_version")
    assert (runtime_config().schema_version, runtime_config().kind) == (1, "runtime_config")
    with pytest.raises(ValidationError):
        TripWorkspace(id="w", owner_id="u", kind="outing_record")


def test_turn_endpoint_is_typed_in_openapi_and_errors_use_codes() -> None:
    verifier = StaticTokenVerifier({"t": VerifiedIdentity("u1", "anonymous")})
    client = TestClient(create_app(verifier=verifier, dev_scaffold=True))
    schema = client.get("/openapi.json").json()
    ok = schema["paths"]["/v1/agent/turns"]["post"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"]["$ref"].endswith("/TurnResponse")
    headers = {"Authorization": "Bearer t", "Idempotency-Key": "k1"}
    first = client.post("/v1/agent/turns", json={"message": "Plan a day trip"}, headers=headers)
    payload = TurnResponse.model_validate(first.json())
    assert payload.kind == "turn_response"
    clash = client.post("/v1/agent/turns", json={"message": "Different request"}, headers=headers)
    assert clash.status_code == 409
    body = ErrorBody.model_validate(clash.json())
    assert body.code == "CONFLICT"
