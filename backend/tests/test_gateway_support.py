"""Shared fixtures for the gateway tests. Holds no tests."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain import (
    ContentClass,
    PhaseFlags,
    RuntimeConfig,
    RuntimeTTLs,
    TokenCeilings,
)
from app.gateway import (
    CallRole,
    ContextMessage,
    GatewayPolicy,
    GenerateRequest,
    OpenRouterGateway,
    OutputSchema,
    PrivacyClass,
    RunBudget,
    ToolSpec,
    placeholder_price_table,
)
from gateway_fakes import FakeTransport

MODEL = "vendor/pinned-model-1"
PROVIDER = "hostone"
SECRET = "sk-test-NOT-A-REAL-KEY-123456"


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    count: int


GOOD = '{"answer": "ok", "count": 2}'


def make_config(**overrides: object) -> RuntimeConfig:
    fields: dict[str, object] = dict(
        version="cfg-1",
        model_id=MODEL,
        provider=PROVIDER,
        provider_allowlist=(PROVIDER, "hosttwo"),
        price_table_version="placeholder-0",
        prompt_version_hashes=(),
        tool_schema_versions=(),
        classifier_version="c1",
        policy_version="p1",
        entitlement_version="e1",
        token_ceilings=TokenCeilings(),
        ttls=RuntimeTTLs(),
        flags=PhaseFlags(),
    )
    fields.update(overrides)
    return RuntimeConfig(**fields)  # type: ignore[arg-type]


def make_policy(**overrides: object) -> GatewayPolicy:
    fields: dict[str, object] = dict(tool_allowlist=("search_places",))
    fields.update(overrides)
    return GatewayPolicy(**fields)  # type: ignore[arg-type]


SEARCH_TOOL = ToolSpec(
    name="search_places", description="synthetic",
    parameters={"type": "object", "additionalProperties": False,
                "properties": {"q": {"type": "string"}}},
)


def make_request(**overrides: object) -> GenerateRequest:
    config = overrides.pop("runtime_config", None) or make_config()
    fields: dict[str, object] = dict(
        call_role=CallRole.FRAME,
        context=(
            ContextMessage(role="system", content="SYSTEM-CANARY", content_class=ContentClass.NONE),
            ContextMessage(role="user", content="USER-CANARY", content_class=ContentClass.USER),
        ),
        allowed_tools=(),
        output_schema=OutputSchema(name="reply", model=Reply),
        budget=RunBudget.from_ceilings(config.token_ceilings),  # type: ignore[attr-defined]
        privacy_class=PrivacyClass.USER_PRIVATE,
        runtime_config=config,
        estimated_input_tokens=1_000,
        max_output_tokens=600,
    )
    fields.update(overrides)
    return GenerateRequest(**fields)  # type: ignore[arg-type]


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gateway(
    transport: FakeTransport,
    *,
    policy: GatewayPolicy | None = None,
    secret: str = SECRET,
    clock: Clock | None = None,
    production: bool = False,
    prices=None,
    config: RuntimeConfig | None = None,
) -> OpenRouterGateway:
    config = config or make_config()
    return OpenRouterGateway(
        transport,
        lambda: secret,
        config,
        policy or make_policy(),
        prices or placeholder_price_table(config.model_id, config.provider),
        clock=clock or Clock(),
        production=production,
    )


def tool_message(content: str = "MAPS-CANARY", cls: ContentClass = ContentClass.MAPS_GROUNDED):
    from app.gateway import ToolProvenance

    return ContextMessage.from_tool_result(
        ToolProvenance(tool="search_places", tool_call_id="c1", content_class=cls), content
    )


def make_ladder(inner, policy, **kwargs):
    from app.gateway import ResilientGateway

    return ResilientGateway(
        inner, policy, price_table=placeholder_price_table(MODEL, PROVIDER), **kwargs
    )
