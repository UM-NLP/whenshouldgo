"""Provider-neutral gateway contracts (docs/model-hosting-decision.md, "Gateway").

Prompt text and provider output live only in fields declared ``repr=False`` so a stray
``repr`` or log line cannot leak them. ``ModelTurn`` exposes typed metadata through
``telemetry`` for the telemetry module; this package does not import telemetry.

Acceptance criterion for the orchestrator wiring (see ``TOOL_RESULT_ACCEPTANCE_CRITERION``).
In Phase 0 ``app.pipeline`` never puts a tool result in any gateway request, which the test
``test_orchestrator_path_never_sends_maps_text_to_frame_or_compose`` proves:
tool-result messages are created only by the tool layer, with the content class derived
from the tool registry by tool name, never supplied by the caller or the model.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..domain import (
    ContentClass,
    Frozen,
    Ident,
    RuntimeConfig,
    TokenCeilings,
    VersionText,
    evolve,
)
from .schema import schema_is_supported

_DEFAULT_CEILINGS = TokenCeilings()

TOOL_RESULT_ACCEPTANCE_CRITERION = (
    "Tool-result context messages must be created only by the tool layer, with the "
    "content class derived from the tool registry by tool name. Neither the orchestrator "
    "caller nor model output may choose the class. Until the orchestrator is wired this is "
    "a convention enforced only by the gateway's refusal of Maps classes for FRAME and COMPOSE."
)
# Conservative token bound: chars/3 (not 4) plus a fixed per-message overhead.
_CHARS_PER_TOKEN_FLOOR = 3
_MESSAGE_OVERHEAD_TOKENS = 8
_REQUEST_OVERHEAD_TOKENS = 16


class GatewayFrozen(Frozen):
    """Frozen contract whose validation errors never echo input (prompt or provider text)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FinishReason(StrEnum):
    """Normalized finish reasons (OpenRouter documents these five values)."""

    STOP = "STOP"
    TOOL_CALLS = "TOOL_CALLS"
    LENGTH = "LENGTH"
    CONTENT_FILTER = "CONTENT_FILTER"
    ERROR = "ERROR"


class CallRole(StrEnum):
    """Which step of the discovery contract this call is (docs/data-and-history-design.md)."""

    FRAME = "FRAME"  # call A: user facts only, no Maps content, may yield a WorkspacePatch
    DISCOVER = "DISCOVER"  # call B: Grounding Lite tool results; output is MAPS_GROUNDED
    COMPOSE = "COMPOSE"  # wording of server-validated state; no Maps content


class PrivacyClass(StrEnum):
    USER_PRIVATE = "USER_PRIVATE"
    MAPS_CONTENT = "MAPS_CONTENT"


class Usage(GatewayFrozen):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    def plus(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


ZERO_USAGE = Usage(input_tokens=0, output_tokens=0)


class ToolCall(GatewayFrozen):
    id: Ident
    name: Ident
    arguments: dict[str, Any] = Field(repr=False)


_UNTAINTED = {ContentClass.USER, ContentClass.NONE}


class ToolProvenance(GatewayFrozen):
    """Where a tool result came from. Built by the tool layer when it returns a result; a
    tool-role message cannot exist without one, so a result cannot be passed off as user
    text. The class must be a real tool class (never USER or NONE)."""

    tool: Ident
    tool_call_id: Ident
    content_class: ContentClass
    evidence_id: Ident | None = None

    @model_validator(mode="after")
    def _tool_content_is_tainted(self) -> ToolProvenance:
        if self.content_class in _UNTAINTED:
            raise ValueError("a tool result must carry a tool content class")
        return self


class ContextMessage(GatewayFrozen):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(repr=False, max_length=200_000)
    # REQUIRED, no default: a missing tag must be an error, not an implicit USER. A
    # Maps-derived message must be tagged MAPS_*; the gateway refuses it for calls that
    # may not carry Maps content.
    content_class: ContentClass
    provenance: ToolProvenance | None = None  # required for, and only for, tool messages
    tool_calls: tuple[ToolCall, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ContextMessage:
        if (self.role == "tool") != (self.provenance is not None):
            raise ValueError("provenance is required for, and only for, tool messages")
        if self.provenance is not None and self.provenance.content_class != self.content_class:
            raise ValueError("tool message class must equal its provenance class")
        if self.role == "tool" and self.content_class in _UNTAINTED:
            raise ValueError("a tool message cannot be USER or NONE content")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages carry tool_calls")
        return self

    @property
    def tool_call_id(self) -> str | None:
        return self.provenance.tool_call_id if self.provenance else None

    @classmethod
    def from_tool_result(cls, provenance: ToolProvenance, content: str) -> ContextMessage:
        return cls(
            role="tool", content=content, content_class=provenance.content_class,
            provenance=provenance,
        )

    @classmethod
    def from_turn(cls, turn: ModelTurn) -> ContextMessage:
        """Feed a prior turn back as context. It inherits the turn's content class, so a
        DISCOVER turn stays MAPS_GROUNDED and cannot reach FRAME or COMPOSE."""
        content = turn.output.model_dump_json() if turn.output is not None else ""
        return cls(
            role="assistant", content=content, content_class=turn.content_class,
            tool_calls=turn.tool_calls,
        )


class ToolSpec(GatewayFrozen):
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
    description: Annotated[str, StringConstraints(max_length=1_000)]
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def _supported_schema(self) -> ToolSpec:
        if self.parameters.get("type") != "object" or not schema_is_supported(self.parameters):
            raise ValueError("tool parameters must be an object schema using supported keywords")
        return self


class OutputSchema(GatewayFrozen):
    """The structured output the call must produce, as a strict Pydantic model."""

    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
    model: type[BaseModel]

    @property
    def json_schema(self) -> dict[str, Any]:
        return self.model.model_json_schema()


class RunBudget(GatewayFrozen):
    """Snapshot of what is left in the run. The caller owns accounting; the gateway only
    reads it and reports usage back on the turn (or on the error)."""

    calls_remaining: int = Field(ge=0)
    tool_calls_remaining: int = Field(ge=0)
    input_tokens_remaining: int = Field(ge=0)
    output_tokens_remaining: int = Field(ge=0)
    escalations_remaining: int = Field(ge=0)
    seconds_remaining: float = Field(ge=0)
    per_call_max_input_tokens: int = Field(default=_DEFAULT_CEILINGS.planner_input_tokens, ge=1)
    per_call_max_output_tokens: int = Field(default=_DEFAULT_CEILINGS.planner_output_tokens, ge=1)

    @classmethod
    def from_ceilings(cls, ceilings: TokenCeilings) -> RunBudget:
        return cls(
            calls_remaining=ceilings.run_model_calls,
            tool_calls_remaining=ceilings.run_tool_calls,
            input_tokens_remaining=ceilings.run_input_tokens,
            output_tokens_remaining=ceilings.run_output_tokens,
            escalations_remaining=ceilings.run_escalations,
            seconds_remaining=float(ceilings.interactive_budget_seconds),
            per_call_max_input_tokens=ceilings.planner_input_tokens,
            per_call_max_output_tokens=ceilings.planner_output_tokens,
        )

    def consume(
        self,
        *,
        calls: int = 0,
        tool_calls: int = 0,
        usage: Usage | None = None,
        escalation: bool = False,
        seconds: float = 0.0,
    ) -> RunBudget:
        """New snapshot with the charge applied, floored at zero (never negative)."""
        used = usage or ZERO_USAGE
        return evolve(
            self,
            calls_remaining=max(0, self.calls_remaining - calls),
            tool_calls_remaining=max(0, self.tool_calls_remaining - tool_calls),
            input_tokens_remaining=max(0, self.input_tokens_remaining - used.input_tokens),
            output_tokens_remaining=max(0, self.output_tokens_remaining - used.output_tokens),
            escalations_remaining=max(0, self.escalations_remaining - int(escalation)),
            seconds_remaining=max(0.0, self.seconds_remaining - seconds),
        )


class GenerateRequest(GatewayFrozen):
    call_role: CallRole
    context: tuple[ContextMessage, ...] = Field(min_length=1, max_length=200, repr=False)
    allowed_tools: tuple[ToolSpec, ...] = Field(default=(), max_length=20)  # server-selected
    output_schema: OutputSchema
    budget: RunBudget
    privacy_class: PrivacyClass
    runtime_config: RuntimeConfig
    # The compiled context is already budget-checked by the caller; this is its estimate.
    estimated_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(default=_DEFAULT_CEILINGS.planner_output_tokens, ge=1)
    escalation: bool = False
    allow_repair: bool = True

    @property
    def context_chars(self) -> int:
        return sum(
            len(m.content) + sum(len(json.dumps(c.arguments)) for c in m.tool_calls)
            for m in self.context
        )

    @property
    def input_token_bound(self) -> int:
        """Never below the caller's estimate, and never trusting it to be small: counts
        every character that will be sent (messages, tool specs, output schema)."""
        chars = self.context_chars + len(json.dumps(self.output_schema.json_schema))
        chars += sum(
            len(t.name) + len(t.description) + len(json.dumps(t.parameters))
            for t in self.allowed_tools
        )
        bound = (
            math.ceil(chars / _CHARS_PER_TOKEN_FLOOR)
            + _MESSAGE_OVERHEAD_TOKENS * len(self.context)
            + _REQUEST_OVERHEAD_TOKENS
        )
        return max(self.estimated_input_tokens, bound)

    @property
    def worst_case_usage(self) -> Usage:
        return Usage(input_tokens=self.input_token_bound, output_tokens=self.max_output_tokens)


class GatewayTelemetry(GatewayFrozen):
    """Typed, content-free metadata for the telemetry module. No free-form strings."""

    call_role: CallRole
    model_id: str
    provider: str
    runtime_config_version: VersionText
    finish_reason: FinishReason
    input_tokens: int
    output_tokens: int
    latency_ms: float
    estimated_cost_usd: Decimal
    price_table_version: VersionText
    price_is_placeholder: bool
    model_calls: int
    repairs: int
    escalation: bool


class ModelTurn(GatewayFrozen):
    """A validated result: structured output or tool calls, never partial output."""

    call_role: CallRole
    content_class: ContentClass
    output: BaseModel | None = Field(default=None, repr=False)
    tool_calls: tuple[ToolCall, ...] = Field(default=(), repr=False)
    finish_reason: FinishReason
    usage: Usage  # total across the first call and any repair
    latency_ms: float = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    price_table_version: VersionText
    price_is_placeholder: bool
    model_id: str
    provider: str
    runtime_config_version: VersionText
    model_calls: int = Field(default=1, ge=1)  # includes repairs and failed retries
    repairs: int = Field(default=0, ge=0)
    escalation: bool = False

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> ModelTurn:
        if (self.output is None) == (not self.tool_calls):
            raise ValueError("a turn carries structured output or tool calls, not both or neither")
        if self.finish_reason in (
            FinishReason.LENGTH,
            FinishReason.CONTENT_FILTER,
            FinishReason.ERROR,
        ):
            raise ValueError("a truncated, filtered or failed turn is never a valid ModelTurn")
        # Discovery output is Maps content; nothing else is.
        if (self.call_role == CallRole.DISCOVER) != (
            self.content_class == ContentClass.MAPS_GROUNDED
        ):
            raise ValueError("only discovery turns carry MAPS_GROUNDED content")
        return self

    @property
    def telemetry(self) -> GatewayTelemetry:
        return GatewayTelemetry(
            call_role=self.call_role,
            model_id=self.model_id,
            provider=self.provider,
            runtime_config_version=self.runtime_config_version,
            finish_reason=self.finish_reason,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
            latency_ms=self.latency_ms,
            estimated_cost_usd=self.estimated_cost_usd,
            price_table_version=self.price_table_version,
            price_is_placeholder=self.price_is_placeholder,
            model_calls=self.model_calls,
            repairs=self.repairs,
            escalation=self.escalation,
        )


class ModelGateway(Protocol):
    def generate(self, request: GenerateRequest) -> ModelTurn: ...
