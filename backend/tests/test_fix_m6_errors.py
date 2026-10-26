"""M6: a tight typed error contract. No client-visible DISCOVERY_DISABLED or
SERVER_POLICY_FAULT, no bare 500 from a conversion error, one source of truth for the caps."""
from __future__ import annotations

from maps_simulator import NOW, place_link, synth_id
from test_gateway_support import make_config
from wiring_support import (
    Env,
    RaisingClient,
    ScriptedGateway,
    frame_turn,
    happy_env,
)

from app import domain
from app.discovery import (
    DiscoveryDisabledError,
    GroundedAnswer,
    GroundedPlace,
    contracts,
    render_grounded_block,
)
from app.domain import ErrorReason
from app.gateway import PrivacyViolationError
from app.gateway.types import Usage


def test_a_tripped_discovery_client_is_reported_as_unavailable() -> None:
    config = make_config()
    env = Env(gateway=ScriptedGateway([frame_turn(config)], config),
              discovery=RaisingClient(DiscoveryDisabledError()), config=config)
    response = env.post()
    assert response.status_code == 503
    assert response.json()["reason"] == "DISCOVERY_UNAVAILABLE"


def test_internal_reasons_are_not_part_of_the_client_contract() -> None:
    assert "DISCOVERY_DISABLED" not in ErrorReason.__members__
    assert "SERVER_POLICY_FAULT" not in ErrorReason.__members__


def test_a_server_side_policy_fault_has_no_reason_and_a_generic_message() -> None:
    config = make_config()
    gateway = ScriptedGateway([PrivacyViolationError(usage=Usage(input_tokens=1, output_tokens=1),
                                                     calls=1)], config)
    env = Env(gateway=gateway, discovery=RaisingClient(DiscoveryDisabledError()), config=config)
    response = env.post()
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "PROVIDER" and "reason" not in body
    assert "privacy" not in body["message"].lower() and "policy" not in body["message"].lower()


def test_a_conversion_error_while_building_the_block_is_a_typed_502(monkeypatch) -> None:
    import app.pipeline as pipeline

    def broken(*args, **kwargs):
        raise ValueError("cannot convert")

    monkeypatch.setattr(pipeline, "to_api_block", broken)
    env, _, _ = happy_env()
    response = env.post()
    assert response.status_code == 502, response.text
    assert response.json()["reason"] == "OUTPUT_REJECTED"
    assert env.turns() == 0 and env.reserved() == set() and len(env.cache) in (0, 1)


def test_a_conversion_error_while_replaying_is_a_typed_conflict(monkeypatch) -> None:
    import app.orchestrator as orchestrator

    env, _, _ = happy_env()
    assert env.post(key="dup").status_code == 200

    def broken(*args, **kwargs):
        raise ValueError("cannot convert")

    monkeypatch.setattr(orchestrator, "to_api_block", broken)
    response = env.post(key="dup")
    assert response.status_code == 409, response.text
    assert response.json()["reason"] == "RESULT_EXPIRED"


def test_the_api_block_caps_are_the_grounded_answer_caps() -> None:
    assert getattr(domain, "GROUNDED_TEXT_MAX_CHARS", None) is contracts.GROUNDED_TEXT_MAX_CHARS
    assert getattr(domain, "MAX_PLACES", None) is contracts.MAX_PLACES
    assert getattr(domain, "MAX_TITLE_CHARS", None) is contracts.MAX_TITLE_CHARS

    def cap(model, name, key):
        for meta in model.model_fields[name].metadata:
            if hasattr(meta, key):
                return getattr(meta, key)
        return None

    assert cap(domain.DiscoveryBlock, "text", "max_length") == contracts.GROUNDED_TEXT_MAX_CHARS
    assert cap(domain.DiscoveryBlock, "places", "max_length") == contracts.MAX_PLACES
    assert cap(domain.DiscoveryPlace, "title", "max_length") == contracts.MAX_TITLE_CHARS


def test_a_maximal_valid_grounded_answer_always_converts() -> None:
    from app.pipeline import to_api_block

    count = contracts.MAX_PLACES
    cites = " ".join(f"[{i}]" for i in range(count))
    text = (cites + " " + "x" * contracts.GROUNDED_TEXT_MAX_CHARS)[: contracts.GROUNDED_TEXT_MAX_CHARS]
    places = tuple(
        GroundedPlace(place_id=synth_id(i), attribution_title="T" * contracts.MAX_TITLE_CHARS,
                      attribution_url=place_link(i), place_url=place_link(i),
                      tool_call_id="tc-1", cited=True)
        for i in range(count)
    )
    answer = GroundedAnswer.build(text=text, places=places, tool_call_id="tc-1", run_id="run-1",
                                  retrieved_at=NOW, ttls=make_config().ttls)
    from app.discovery._validated import mint_validated

    validated = mint_validated(answer, "u1")
    block = to_api_block(render_grounded_block(validated, domain.FramingKind.RESULTS_INTRO),
                         answer.cache_expires_at, answer.retrieved_at)
    assert len(block.places) == count and len(block.text) == contracts.GROUNDED_TEXT_MAX_CHARS
