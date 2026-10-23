"""Fix pass after the security review of the contract freeze. Synthetic fixtures only."""
import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import domain as domain_module
from app.auth import PrincipalResolver, StaticTokenVerifier, VerifiedIdentity
from app.domain import ErrorBody, Fact, PendingAction, RuntimeConfig, TripWorkspace, TurnResponse
from app.main import create_app
from test_api_identity import TRAVEL, small_registry
from test_contract_freeze import (
    T0,
    consent,
    evidence,
    outing,
    plan_version,
    runtime_config,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"
LATER = T0 + timedelta(minutes=30)
NAIVE = datetime(2026, 10, 10, 15)


# --- 1. kind <-> content_class laundering --------------------------------------
@pytest.mark.parametrize(
    ("kind", "content_class"),
    [(k, c) for k in ("WEATHER", "USER") for c in ("MAPS_PLACES", "MAPS_ROUTE", "MAPS_GROUNDED")
     if (k, c) != ("WEATHER", "MAPS_GROUNDED")],  # weather via Grounding Lite is MAPS_GROUNDED
)
def test_f1_weather_and_user_cannot_carry_maps_classes(kind, content_class) -> None:
    with pytest.raises(ValidationError):
        evidence(
            kind=kind, content_class=content_class, llm_visible=True,
            storage_class="EPHEMERAL", cache_expires_at=None, place_id="synthetic-place",
        )  # fmt: skip


@pytest.mark.parametrize("kind", ["WEATHER", "USER"])
def test_f1_non_maps_kinds_cannot_use_display_cache(kind) -> None:
    with pytest.raises(ValidationError):
        evidence(kind=kind, content_class="NONE", attribution_required=False)  # DISPLAY_CACHE


def test_f1_llm_visibility_follows_content_class_and_mapping_is_bidirectional() -> None:
    with pytest.raises(ValidationError):
        evidence(
            kind="GROUNDED_ANSWER", content_class="MAPS_PLACES", llm_visible=True,
            storage_class="EPHEMERAL", cache_expires_at=None,
        )  # fmt: skip
    with pytest.raises(ValidationError):
        evidence(kind="PLACES_DIRECT", content_class="MAPS_GROUNDED", llm_visible=False)
    assert evidence(
        kind="WEATHER", content_class="NONE", llm_visible=True, attribution_required=False,
        storage_class="EPHEMERAL", cache_expires_at=None,
    )  # fmt: skip
    for cls in ("USER", "NONE"):
        with pytest.raises(ValidationError):
            evidence(kind="USER", content_class=cls, attribution_required=False)


# --- 2. mutability ----------------------------------------------------------------
def pending(**overrides) -> PendingAction:
    values = dict(
        id="a1", workspace_id="w", plan_version=1, effect="SET_REMINDER", target="device-1",
        preview="Remind me at 17:15", nonce="n" * 16, idempotency_key="idem-key-1",
        created_at=T0, expires_at=T0 + timedelta(minutes=10),
    )  # fmt: skip
    return PendingAction(**{**values, **overrides})


def test_f2_evidence_fact_pending_action_are_immutable() -> None:
    with pytest.raises(ValidationError):
        evidence().llm_visible = True
    fact = Fact(
        id="f", key="k", source="USER_EXPLICIT", confirmation="CONFIRMED", hard=True, scope="trip"
    )
    with pytest.raises(ValidationError):
        fact.hard = False
    with pytest.raises(ValidationError):
        pending().confirmed_at = T0


def test_f2_workspace_assignment_is_revalidated() -> None:
    workspace = TripWorkspace(id="w", owner_id="u")
    with pytest.raises(ValidationError):
        workspace.status = "NOT_A_STATUS"


def test_f2_confirmation_goes_through_a_server_function() -> None:
    action = pending()
    confirmed = domain_module.confirm_pending_action(action, now=T0 + timedelta(minutes=1))
    assert confirmed is not action and action.confirmed_at is None
    assert confirmed.confirmed_at == T0 + timedelta(minutes=1)
    with pytest.raises(ValueError):
        domain_module.confirm_pending_action(confirmed, now=T0 + timedelta(minutes=2))
    with pytest.raises(ValueError):
        domain_module.confirm_pending_action(action, now=T0 + timedelta(minutes=11))
    with pytest.raises(ValueError):
        domain_module.confirm_pending_action(action, now=NAIVE)


def test_f2_no_model_construct_or_model_copy_update_in_app() -> None:
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "model_construct":
                offenders.append(f"{path.name}:{node.lineno} model_construct")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "model_copy"
                and any(k.arg == "update" for k in node.keywords)
            ):
                offenders.append(f"{path.name}:{node.lineno} model_copy(update=)")
    assert offenders == []


# --- 3. PendingAction bounds --------------------------------------------------------
def test_f3_phase_2_3_effects_are_not_in_the_enum_yet() -> None:
    effects = {e.value for e in domain_module.PendingActionEffect}
    assert effects == {"SET_REMINDER", "LAUNCH_HANDOFF"}
    for effect in ("CREATE_ROUTINE", "ENABLE_NOTIFICATIONS"):
        with pytest.raises(ValidationError):
            pending(effect=effect)


def test_f3_pending_action_bounds() -> None:
    assert domain_module.MAX_PENDING_ACTION_TTL == timedelta(minutes=15)
    assert pending(expires_at=T0 + timedelta(minutes=15))
    with pytest.raises(ValidationError):
        pending(expires_at=T0 + timedelta(minutes=15, seconds=1))
    for bad in (
        dict(expires_at=T0 + timedelta(minutes=16)),
        dict(expires_at=T0),
        dict(nonce="short"),
        dict(idempotency_key="x"),
        dict(confirmed_at=T0 - timedelta(seconds=1)),
        dict(confirmed_at=T0 + timedelta(minutes=11)),
        dict(target="12 Main Street, Toronto"),
        dict(target="43.65,-79.38"),
        dict(preview="p" * 501),
        dict(created_at=None),
    ):
        with pytest.raises(ValidationError):
            pending(**bad)
    assert pending(confirmed_at=T0 + timedelta(minutes=5))


# --- 4. free-text carriers and bounds -------------------------------------------------
@pytest.mark.parametrize("origin", ["43.65,-79.38", "12 Main St", "short", "a" * 65, "has space1"])
def test_f4_origin_ref_must_be_opaque(origin) -> None:
    with pytest.raises(ValidationError):
        plan_version(origin_ref=origin)
    assert plan_version(origin_ref="abcd1234_-XYZ")


@pytest.mark.parametrize(
    "label", ["43.65,-79.38", "43.6532, -79.3832", "123 Queen Street West", "M5V 2T6", "x" * 81]
)
def test_f4_user_labels_reject_coordinates_and_addresses(label) -> None:
    with pytest.raises(ValidationError):
        plan_version(destination={"place_id": "synthetic-place", "user_label": label})
    with pytest.raises(ValidationError):
        outing(user_label=label)
    assert outing(user_label="Saturday brunch spot")


def test_f4_identifier_and_text_bounds() -> None:
    for bad in (
        dict(place_id="p" * 301), dict(source="s" * 101), dict(precision="x" * 41),
        dict(tool="bad tool!"), dict(tool_version="v" * 41), dict(tool_call_id="c" * 200),
        dict(id="bad id"),
    ):  # fmt: skip
        with pytest.raises(ValidationError):
            evidence(**bad)
    with pytest.raises(ValidationError):
        plan_version(workspace_id="w" * 200)
    with pytest.raises(ValidationError):
        plan_version(grounded_evidence_id="bad id!")
    with pytest.raises(ValidationError):
        outing(owner_id="o" * 200)
    base = dict(
        id="f", key="k", source="USER_EXPLICIT", confirmation="CONFIRMED", hard=False, scope="trip"
    )
    with pytest.raises(ValidationError):
        Fact(**{**base, "value": "v" * 501})
    with pytest.raises(ValidationError):
        Fact(**{**base, "scope": "s" * 100})
    with pytest.raises(ValidationError):
        TripWorkspace(id="w", owner_id="u", goal="g" * 20_001)


def test_f4_runtime_config_is_deeply_immutable_and_sub_objects_explicit() -> None:
    config = runtime_config()
    assert not any(isinstance(v, dict) for v in dict(config).values())
    assert not hasattr(config.prompt_version_hashes, "append")
    assert isinstance(config.prompt_version_hashes, tuple)
    hash(config)  # fully immutable, therefore hashable
    with pytest.raises(ValidationError):
        RuntimeConfig(
            version="v", model_id="m", provider="p", prompt_version_hashes=(),
            tool_schema_versions=(), classifier_version="c", policy_version="p",
            entitlement_version="e",
        )  # fmt: skip
    with pytest.raises(ValidationError):
        runtime_config(prompt_version_hashes=({"name": "a", "sha256": "c" * 64},) * 2)


def test_f4_consent_processors_channels_context_are_constrained() -> None:
    with pytest.raises(ValidationError):
        consent(processors=("SomeRandomVendor",))
    with pytest.raises(ValidationError):
        consent(scope={"retention_days": 30, "channels": ("CARRIER_PIGEON",)})
    with pytest.raises(ValidationError):
        consent(context="free text with spaces and an address 12 Main St")
    assert consent(processors=("GOOGLE", "OPENROUTER", "UPSTREAM_MODEL_HOST"))


# --- 5. aware datetimes ---------------------------------------------------------------
def test_f5_naive_and_mixed_datetimes_raise_validation_error_not_type_error() -> None:
    for bad in (
        dict(retrieved_at=NAIVE),
        dict(expires_at=NAIVE),
        dict(cache_expires_at=NAIVE),
        dict(retrieved_at=NAIVE, expires_at=T0 + timedelta(hours=1)),
    ):
        with pytest.raises(ValidationError):
            evidence(**bad)
    with pytest.raises(ValidationError):
        evidence(expires_at=T0)  # must follow retrieved_at
    with pytest.raises(ValidationError):
        plan_version(created_at=NAIVE)
    with pytest.raises(ValidationError):
        consent(granted_at=NAIVE)
    with pytest.raises(ValidationError):
        consent(withdrawn_at=NAIVE)
    with pytest.raises(ValidationError):
        pending(created_at=NAIVE)
    with pytest.raises(ValidationError):
        outing(events=({"type": "SELECTED", "at": NAIVE},))
    with pytest.raises(ValidationError):
        Fact(
            id="f", key="k", source="USER_EXPLICIT", confirmation="CONFIRMED", hard=False,
            scope="s", expires_at=NAIVE,
        )  # fmt: skip
    with pytest.raises(ValueError):
        evidence().is_fresh(NAIVE)


# --- 6. evidence cache rules ------------------------------------------------------------
def test_f6_cache_rules() -> None:
    with pytest.raises(ValidationError):
        evidence(storage_class="EPHEMERAL")  # cache_expires_at set but not DISPLAY_CACHE
    for extra in (dict(content_hash="d" * 64), dict(cache_expires_at=LATER)):
        with pytest.raises(ValidationError):
            evidence(
                storage_class="PLACE_ID_ONLY", place_id="synthetic-place",
                **{"cache_expires_at": None, "content_hash": None, **extra},
            )  # fmt: skip
    assert evidence(
        storage_class="PLACE_ID_ONLY", place_id="synthetic-place",
        cache_expires_at=None, content_hash=None,
    )  # fmt: skip


def test_f6_runtime_ttl_can_never_exceed_the_evidence_cap() -> None:
    top = domain_module.RuntimeTTLs(grounded_cache_hours=720)
    assert timedelta(hours=top.grounded_cache_hours) == domain_module.MAX_MAPS_CACHE
    assert evidence(cache_expires_at=T0 + timedelta(hours=top.grounded_cache_hours))
    assert domain_module.RuntimeTTLs().grounded_cache_hours == 24
    with pytest.raises(ValidationError):
        domain_module.RuntimeTTLs(grounded_cache_hours=721)


# --- 7. error contract ---------------------------------------------------------------------
SECRET = "my secret trip text 12 Main St"
H = {"Authorization": "Bearer t", "Idempotency-Key": "k1"}


def make_client(**kw) -> TestClient:
    verifier = StaticTokenVerifier(
        {"t": VerifiedIdentity("u1", "anonymous"), "t2": VerifiedIdentity("u2", "anonymous")}
    )
    if "orchestrator" not in kw:
        kw.setdefault("dev_scaffold", True)  # these tests exercise the development scaffold
    return TestClient(create_app(verifier=verifier, **kw), raise_server_exceptions=False)


def assert_typed(response, status, code) -> dict:
    assert response.status_code == status
    assert ErrorBody.model_validate(response.json()).code == code
    return response.json()


def test_f7_401_is_typed_and_ordered_before_validation() -> None:
    client = make_client()
    r = client.post("/v1/agent/turns", json={"message": SECRET})
    assert_typed(r, 401, "POLICY")
    assert r.headers.get("www-authenticate") == "Bearer"
    r = client.post("/v1/agent/turns", json={"bad": SECRET}, headers={"Authorization": "Bearer nope"})
    assert_typed(r, 401, "POLICY")  # auth failure wins over a malformed body
    assert SECRET not in r.text


def test_f7_422_is_redacted() -> None:
    client = make_client()
    r = client.post("/v1/agent/turns", json={"message": SECRET, "owner_id": "evil"}, headers=H)
    assert_typed(r, 422, "VALIDATION")
    assert SECRET not in r.text and "evil" not in r.text and "owner_id" not in r.text
    r = client.post("/v1/agent/turns", json={"message": "hi"}, headers={"Authorization": "Bearer t"})
    assert_typed(r, 422, "VALIDATION")  # missing Idempotency-Key


def test_f7_idempotency_reuse_is_409_and_never_echoes_message() -> None:
    client = make_client()
    ok = client.post("/v1/agent/turns", json=TRAVEL, headers=H)
    assert ok.status_code == 200
    r = client.post("/v1/agent/turns", json={"message": SECRET}, headers=H)
    assert_typed(r, 409, "CONFLICT")
    assert SECRET not in r.text
    same = client.post("/v1/agent/turns", json=TRAVEL, headers=H)
    assert same.json() == ok.json()
    other = client.post(
        "/v1/agent/turns", json=TRAVEL,
        headers={"Authorization": "Bearer t2", "Idempotency-Key": "k1"},
    )  # fmt: skip
    assert other.json()["workspace_id"] != ok.json()["workspace_id"]


def test_f7_403_shape_and_openapi_lists_error_responses() -> None:
    client = make_client(resolver=PrincipalResolver(disabled_uids={"u1"}))
    body = assert_typed(
        client.post("/v1/agent/turns", json={"message": "hi"}, headers=H), 403, "POLICY"
    )
    assert "allowed_tools" not in body and "workspace_id" not in body
    responses = client.app.openapi()["paths"]["/v1/agent/turns"]["post"]["responses"]
    for status in ("200", "401", "403", "409", "422", "429"):
        assert status in responses
    for status in ("401", "403", "409", "422", "429"):
        ref = responses[status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorBody")


def test_f7_unhandled_and_out_of_contract_results_are_typed() -> None:
    from app.orchestrator import TurnOrchestrator, TurnResult

    class Boom(TurnOrchestrator):
        def run_turn(self, *args, **kwargs):
            raise RuntimeError(SECRET)

    r = make_client(orchestrator=Boom()).post("/v1/agent/turns", json={"message": "hi"}, headers=H)
    assert_typed(r, 500, "PROVIDER")
    assert SECRET not in r.text

    class Oversized(TurnOrchestrator):
        def run_turn(self, *args, **kwargs):
            return TurnResult("x" * 10_000, None, (), None)

    r = make_client(orchestrator=Oversized()).post(
        "/v1/agent/turns", json={"message": "hi"}, headers=H
    )
    assert_typed(r, 500, "VALIDATION")
    with pytest.raises(ValidationError):
        TurnResponse(
            reply="x" * 10_000, domain=None, allowed_tools=(), workspace_id=None,
            allowance_exhausted=False, policy_version="p",
        )  # fmt: skip


def test_f7_quota_exhausted_stays_200_with_flag() -> None:
    from app.orchestrator import TurnOrchestrator
    from app.quota import QuotaService

    client = make_client(orchestrator=TurnOrchestrator(QuotaService(small_registry()), dev_scaffold=True))
    flags = []
    for i in range(3):
        r = client.post(
            "/v1/agent/turns", json=TRAVEL,
            headers={"Authorization": "Bearer t", "Idempotency-Key": f"key-{i}"},
        )  # fmt: skip
        assert r.status_code == 200
        flags.append(r.json()["allowance_exhausted"])
    assert flags == [False, False, True]  # QUOTA/PROVIDER codes are not emitted for this


# --- owner decisions 2026-10-15: weather ----------------------------------------------------
def weather(**overrides):
    values = dict(
        kind="WEATHER", content_class="MAPS_GROUNDED", llm_visible=True,
        storage_class="DISPLAY_CACHE", attribution_required=True,
    )  # fmt: skip
    return evidence(**{**values, **overrides})


def test_weather_from_grounding_lite_is_maps_grounded_content() -> None:
    assert weather()
    with pytest.raises(ValidationError):
        weather(attribution_required=False)
    with pytest.raises(ValidationError):
        weather(cache_expires_at=T0 + timedelta(days=30, seconds=1))  # 30-day cap
    assert weather(cache_expires_at=T0 + timedelta(days=30))
    for storage in ("PLACE_ID_ONLY",):
        assert weather(storage_class=storage, place_id="synthetic-place", cache_expires_at=None,
                       content_hash=None)  # fmt: skip
    with pytest.raises(ValidationError):
        weather(storage_class="EPHEMERAL")  # cache_expires_at only for DISPLAY_CACHE


def test_non_google_weather_is_none_and_not_attribution_required_maps_content() -> None:
    assert weather(content_class="NONE", attribution_required=False, storage_class="EPHEMERAL",
                   cache_expires_at=None)  # fmt: skip
    with pytest.raises(ValidationError):
        weather(content_class="NONE", attribution_required=True, storage_class="EPHEMERAL",
                cache_expires_at=None)  # fmt: skip
    with pytest.raises(ValidationError):
        weather(content_class="NONE", attribution_required=False)  # no DISPLAY_CACHE for NONE


def test_maps_grounded_is_only_for_grounded_answer_and_weather() -> None:
    assert evidence(kind="GROUNDED_ANSWER", content_class="MAPS_GROUNDED")
    for kind in ("USER", "PLACES_DIRECT", "ROUTE_DIRECT"):
        with pytest.raises(ValidationError):
            evidence(kind=kind, content_class="MAPS_GROUNDED")


def test_direct_maps_evidence_stays_hidden_from_the_model() -> None:
    for kind, cls in (("PLACES_DIRECT", "MAPS_PLACES"), ("ROUTE_DIRECT", "MAPS_ROUTE")):
        with pytest.raises(ValidationError):
            evidence(kind=kind, content_class=cls, llm_visible=True, storage_class="EPHEMERAL",
                     cache_expires_at=None)  # fmt: skip


# --- 8. retention cap -------------------------------------------------------------------------
def test_f8_outing_retention_is_capped_at_180_days() -> None:
    assert domain_module.MAX_OUTING_RETENTION == timedelta(days=180)
    assert outing(retention_expires_at=T0 + timedelta(days=180))
    with pytest.raises(ValidationError):
        outing(retention_expires_at=T0 + timedelta(days=181))
    assert domain_module.RuntimeTTLs(outing_retention_days=180)
    with pytest.raises(ValidationError):
        domain_module.RuntimeTTLs(outing_retention_days=181)  # shorten only, never extend
