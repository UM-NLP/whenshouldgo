"""build_runtime: refusals, pinned config, secrets and transports (no network, no real keys)."""
from __future__ import annotations

import ast
import inspect
import re
from decimal import Decimal
from pathlib import Path

import pytest
from composition_support import (
    ENV,
    GM_CANARY,
    HEADERS,
    MESSAGE,
    OR_CANARY,
    ProviderModel,
    build,
    verifier,
)
from fastapi.testclient import TestClient
from gateway_fakes import FakeTransport
from maps_simulator import FakeMcpTransport, mcp_ok
from wiring_support import canary_payload, deep_text

from app import composition
from app.composition import (
    GOOGLE_MAPS_KEY_VAR,
    OPENROUTER_KEY_VAR,
    ConfigurationError,
    build_runtime,
    pinned_price_table,
    pinned_runtime_config,
    second_model_config,
)
from app.discovery import TOOL_SPECS, HttpxMcpTransport
from app.entitlements import PILOT_POLICY
from app.frame import FRAME_PROMPT_SHA256
from app.gateway import HttpxTransport, OpenRouterGateway, ResilientGateway, placeholder_price_table
from app.gateway.policy import self_check

BAD_VALUES = ["", " ", "key with space", "key\twith-tab", "key\nnewline", " lead", "trail ",
              "ctrl\x00char", "café"]


@pytest.mark.parametrize("name", [OPENROUTER_KEY_VAR, GOOGLE_MAPS_KEY_VAR])
def test_a_missing_key_is_refused(name: str) -> None:
    env = {k: v for k, v in ENV.items() if k != name}
    with pytest.raises(ConfigurationError, match=name):
        build(env)


@pytest.mark.parametrize("name", [OPENROUTER_KEY_VAR, GOOGLE_MAPS_KEY_VAR])
@pytest.mark.parametrize("value", BAD_VALUES)
def test_an_empty_or_malformed_key_is_refused_without_echoing_it(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        build({**ENV, name: value})
    assert value.strip() == "" or value not in str(caught.value)
    assert OR_CANARY not in deep_text(caught.value) and GM_CANARY not in deep_text(caught.value)


def test_a_malformed_key_that_embeds_a_canary_never_leaks_it() -> None:
    secret = "CANARY-SHOULD-NOT-LEAK 42"
    with pytest.raises(ConfigurationError) as caught:
        build({**ENV, OPENROUTER_KEY_VAR: secret})
    assert "CANARY" not in deep_text(caught.value) and "CANARY" not in repr(caught.value)


def test_the_environment_is_never_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OPENROUTER_KEY_VAR, OR_CANARY)
    monkeypatch.setenv(GOOGLE_MAPS_KEY_VAR, GM_CANARY)
    with pytest.raises(ConfigurationError):
        build({})


def test_composition_never_reads_files_or_exposes_a_tls_option() -> None:
    tree = ast.parse(Path(composition.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert not imported & {"os", "ssl", "pathlib", "io"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"open", "environ", "getenv", "read_text", "verify", "SSLContext"}
    assert "verify=" not in {k.arg for n in ast.walk(tree) if isinstance(n, ast.keyword)
                             for k in [n]}


def test_a_verifier_is_required() -> None:
    with pytest.raises(ConfigurationError, match="verifier"):
        build(identity_verifier=None)


def test_in_memory_state_must_be_acknowledged() -> None:
    for kwargs in ({"allow_in_memory_state": False}, {"allow_in_memory_state": None}):
        with pytest.raises(ConfigurationError, match="shared state not implemented"):
            build(**kwargs)
    with pytest.raises(ConfigurationError, match="shared state not implemented"):
        build_runtime(dict(ENV), identity_verifier=verifier())


def test_a_placeholder_price_table_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    config = pinned_runtime_config()
    monkeypatch.setattr(composition, "pinned_price_table", lambda: placeholder_price_table(
        config.model_id, config.provider, version=config.price_table_version))
    with pytest.raises(ConfigurationError, match="gateway"):
        build()


def test_a_price_table_version_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    table = pinned_price_table().model_copy(update={"version": "other-1"})
    monkeypatch.setattr(composition, "pinned_price_table", lambda: table)
    with pytest.raises(ConfigurationError):
        build()


def test_pinned_config_matches_the_reviewed_decision() -> None:
    config = pinned_runtime_config()
    assert config.model_id == "google/gemma-4-31b-it"
    assert config.provider == "deepinfra" and config.provider_allowlist == ("deepinfra",)
    assert config.zdr is True and config.data_collection == "deny"
    assert config.allow_fallbacks is False and config.maps_content_to_model is False
    assert config.price_table_version == "live-2026-10-17"
    assert [(h.name, h.sha256) for h in config.prompt_version_hashes] == [
        ("frame", FRAME_PROMPT_SHA256)]
    assert {v.name for v in config.tool_schema_versions} == {t.value for t in TOOL_SPECS}
    assert config.entitlement_version == PILOT_POLICY.version == config.policy_version
    assert config.flags.phase0_discovery and not config.flags.phase1_history
    assert pinned_runtime_config() == config  # deterministic


def test_pinned_prices_and_self_check_in_production_mode() -> None:
    config, prices = pinned_runtime_config(), pinned_price_table()
    self_check(config, prices, production=True)
    assert prices.placeholder is False and prices.version == config.price_table_version
    price = prices.lookup("google/gemma-4-31b-it", "deepinfra")
    assert (price.input_usd_per_million, price.output_usd_per_million) == (
        Decimal("0.20"), Decimal("0.40"))
    assert [u.usd_per_request for u in prices.unit_prices] == [Decimal(0)]
    assert len(prices.model_prices) == 1


def test_the_second_model_is_described_but_not_wired() -> None:
    second = second_model_config()
    assert second.model_id == "qwen/qwen3-next-80b-a3b-instruct" and second.provider == "deepinfra"
    assert composition.SECOND_MODEL_PRICE.input_usd_per_million == Decimal("0.09")
    assert composition.SECOND_MODEL_PRICE.output_usd_per_million == Decimal("1.10")
    assert build().config.model_id == "google/gemma-4-31b-it"
    with pytest.raises(Exception):  # the primary price table does not admit it
        pinned_price_table().lookup(second.model_id, second.provider)


def test_the_gateway_is_the_resilient_production_stack() -> None:
    runtime = build()
    assert isinstance(runtime.gateway, ResilientGateway)
    inner = runtime.gateway._inner  # noqa: SLF001
    assert isinstance(inner, OpenRouterGateway) and inner._production is True  # noqa: SLF001
    assert inner.quota_latch is runtime.quota_latch
    assert runtime.discovery_client._kill_switch is runtime.kill_switch  # noqa: SLF001
    assert runtime.orchestrator.display_cache is runtime.display_cache
    assert runtime.orchestrator.costs is runtime.costs
    assert runtime.orchestrator.quotas is runtime.quotas
    assert runtime.orchestrator._dev_scaffold is False  # noqa: SLF001


def test_secrets_are_in_no_repr_str_or_field() -> None:
    runtime = build()
    for text in (repr(runtime), str(runtime), repr(runtime.gateway), repr(runtime.discovery_client)):
        assert OR_CANARY not in text and GM_CANARY not in text
    walked = deep_text(runtime)
    assert OR_CANARY not in walked and GM_CANARY not in walked


def test_keys_reach_only_the_two_transports_through_credentials() -> None:
    model = ProviderModel()
    maps = FakeMcpTransport([mcp_ok(canary_payload())])
    runtime = build(model_transport=model, discovery_transport=maps)
    response = TestClient(runtime.app).post("/v1/agent/turns", json=MESSAGE, headers=HEADERS)
    assert response.status_code == 200
    assert model.credentials == [OR_CANARY] and maps.keys == [GM_CANARY]
    assert OR_CANARY not in response.text and GM_CANARY not in response.text


def test_transports_default_to_the_locked_classes() -> None:
    runtime = build(model_transport=None, discovery_transport=None)
    assert type(runtime.gateway._inner._transport) is HttpxTransport  # noqa: SLF001
    assert type(runtime.discovery_client._transport) is HttpxMcpTransport  # noqa: SLF001


def test_transports_can_be_injected_only_through_the_two_parameters() -> None:
    model, maps = FakeTransport([]), FakeMcpTransport([])
    runtime = build(model_transport=model, discovery_transport=maps)
    assert runtime.gateway._inner._transport is model  # noqa: SLF001
    assert runtime.discovery_client._transport is maps  # noqa: SLF001
    params = inspect.signature(build_runtime).parameters
    assert not [p for p in params if re.search(r"ssl|tls|verify|context|cert", p)]
