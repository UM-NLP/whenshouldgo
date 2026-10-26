"""Gateway policy, startup self-check and the request checks every gateway shares.

``RuntimeConfig`` is the source of truth for the model, provider, provider allowlist,
privacy settings, price-table version and token ceilings (it is recorded on every turn).
``GatewayPolicy`` keeps only gateway-local settings: the tool ceiling, timeouts, repair
and resilience constants. Everything fails closed before any request is sent.
"""
from __future__ import annotations

import re
import typing
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from ..domain import (
    ContentClass,
    Evidence,
    Fact,
    Frozen,
    OutingRecord,
    PendingAction,
    PlaceRef,
    PlanDestination,
    PlanVersion,
    RuntimeConfig,
    TripWorkspace,
    WorkspacePatch,
)
from .errors import (
    BudgetExceededError,
    GatewayConfigError,
    PrivacyViolationError,
    TaintedContextError,
    ToolNotAllowedError,
)
from .pricing import PriceTable
from .types import CallRole, GenerateRequest, PrivacyClass

# Provider slugs, as used by RuntimeConfig.provider_allowlist; telemetry imports this name.
ProviderSlug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]

# A pinned model is "vendor/name" with no routing variant suffix. Variants such as
# ":online" (adds web search), ":free", ":nitro" or ":floor" change routing or enable
# plugins, and "openrouter/*" ids are routers. Public so other modules (telemetry) share
# one definition and cannot diverge from the gateway.
MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
_ROUTER_NAMES = {"auto", "router", "openrouter/auto", "default"}

MAX_CONTEXT_CHARS = 120_000  # hard cap on characters in one request's context

_MAPS_CLASSES = {ContentClass.MAPS_GROUNDED, ContentClass.MAPS_PLACES, ContentClass.MAPS_ROUTE}
# Direct Places/Routes payloads are never LLM-visible until decision 17 changes.
_NEVER_LLM_VISIBLE = {ContentClass.MAPS_PLACES, ContentClass.MAPS_ROUTE}

# Types that carry or change workspace state. A model output schema may contain one only
# for call A (FRAME) and only if its root model is explicitly allowlisted below.
STATE_BEARING_TYPES: tuple[type[BaseModel], ...] = (
    WorkspacePatch, Fact, TripWorkspace, PendingAction, Evidence, PlanVersion, OutingRecord,
    PlaceRef, PlanDestination,
)  # fmt: skip
FRAME_STATE_SCHEMA_ALLOWLIST: frozenset[type[BaseModel]] = frozenset({WorkspacePatch})


def _frame_roots() -> frozenset[type[BaseModel]]:
    """Every root model FRAME may use: the bare patch plus the orchestrator's FrameProposal (an
    intent, closed search arguments and a few soft facts: no state fields at all). Imported
    lazily because app.frame depends on this package; membership is by exact class, a subclass
    or a wrapper is still refused."""
    from ..frame import FrameProposal

    return FRAME_STATE_SCHEMA_ALLOWLIST | {FrameProposal}


class ResiliencePolicy(Frozen):
    max_attempts: int = Field(default=3, ge=1, le=5)
    backoff_base_seconds: float = Field(default=0.25, gt=0)
    backoff_cap_seconds: float = Field(default=2.0, gt=0)
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_open_seconds: float = Field(default=30.0, gt=0)


class GatewayPolicy(Frozen):
    """Gateway-local settings only. Privacy settings and the provider allowlist come from
    ``RuntimeConfig``."""

    tool_allowlist: tuple[str, ...] = ()  # ceiling over every request's allowed_tools
    request_timeout_cap_seconds: float = Field(default=20.0, gt=0)
    repair_instruction_tokens: int = Field(default=64, ge=1)
    max_repairs: int = Field(default=1, ge=0, le=1)  # docs: at most one bounded repair
    resilience: ResiliencePolicy = Field(default_factory=ResiliencePolicy)


def is_pinned_model_id(model_id: str) -> bool:
    return (
        model_id == model_id.strip().lower()
        and model_id not in _ROUTER_NAMES
        and not model_id.startswith("openrouter/")
        and bool(MODEL_ID_PATTERN.fullmatch(model_id))
    )


def self_check(
    config: RuntimeConfig, price_table: PriceTable, *, production: bool = False
) -> None:
    """Startup validation of the config and price table. Raises a typed error."""
    if not config.provider.strip() or config.provider != config.provider.lower():
        raise GatewayConfigError("runtime config lacks a pinned provider")
    if not config.provider_allowlist:
        raise GatewayConfigError("provider allowlist is empty")
    if config.provider not in config.provider_allowlist:
        raise GatewayConfigError("pinned provider is not on the reviewed allowlist")
    if not is_pinned_model_id(config.model_id):
        raise GatewayConfigError("model id is not pinned")
    # RuntimeConfig's types already forbid these; re-check so a future relaxation or an
    # object built around the validators still fails closed.
    if config.zdr is not True:
        raise PrivacyViolationError("zero data retention must be required")
    if config.data_collection != "deny":
        raise PrivacyViolationError("data collection must be denied")
    if config.allow_fallbacks is not False:
        raise PrivacyViolationError("provider fallbacks must be disabled")
    if price_table.version != config.price_table_version:
        raise GatewayConfigError("price table version does not match the runtime config")
    price_table.lookup(config.model_id, config.provider)
    if production and price_table.placeholder:
        raise GatewayConfigError("placeholder prices are not allowed in production")


def check_request(request: GenerateRequest, policy: GatewayPolicy) -> None:
    """Checks shared by every ModelGateway (real, fake, wrapped). No I/O."""
    if request.context_chars > MAX_CONTEXT_CHARS:
        raise BudgetExceededError("context is too large")
    _check_context(request)
    _check_output_schema(request)
    _check_tools(request, policy)
    _check_budget(request)


def _check_context(request: GenerateRequest) -> None:
    classes = {message.content_class for message in request.context}
    if classes & _NEVER_LLM_VISIBLE:
        raise TaintedContextError("Places and Routes payloads are never model-visible")
    carries_maps = bool(classes & _MAPS_CLASSES)
    if request.call_role != CallRole.DISCOVER:
        # Call A (FRAME) and COMPOSE must not see Maps content. The model patch comes only
        # from user facts (docs/data-and-history-design.md).
        if carries_maps or request.privacy_class == PrivacyClass.MAPS_CONTENT:
            raise TaintedContextError("Maps content is not permitted for this call")
        return
    if carries_maps and request.privacy_class != PrivacyClass.MAPS_CONTENT:
        raise PrivacyViolationError("Maps content requires the MAPS_CONTENT privacy class")


def _model_types(annotation: Any, seen: set[type]) -> set[type]:
    """Every BaseModel class reachable from a field annotation (unions, containers, nested)."""
    found: set[type] = set()
    stack = [annotation]
    while stack:
        item = stack.pop()
        if isinstance(item, type) and issubclass(item, BaseModel):
            if item in seen:
                continue
            seen.add(item)
            found.add(item)
            stack.extend(field.annotation for field in item.model_fields.values())
        else:  # unions, tuple[...], list[...], dict[...], Annotated[...]
            stack.extend(typing.get_args(item))
    return found


def _check_output_schema(request: GenerateRequest) -> None:
    root = request.output_schema.model
    reachable = _model_types(root, set())
    if any(found.model_config.get("extra") != "forbid" for found in reachable):
        raise GatewayConfigError("output schema models must forbid extra fields")
    from ..frame import FrameProposal

    # The FRAME proposal carries no state type, but it is FRAME's output only: any other call, a
    # subclass of it or a model that wraps it is refused.
    if any(issubclass(found, FrameProposal) for found in reachable) and not (
        request.call_role == CallRole.FRAME and root is FrameProposal
    ):
        raise TaintedContextError("output schema may not carry the FRAME proposal for this call")
    state_bearing = any(issubclass(found, STATE_BEARING_TYPES) for found in reachable)
    if not state_bearing:
        return
    if request.call_role == CallRole.FRAME and root in _frame_roots():
        return
    # DISCOVER output is MAPS_GROUNDED and never produces a patch; COMPOSE only words
    # state the server already validated.
    raise TaintedContextError("output schema may not carry workspace state for this call")


def _check_tools(request: GenerateRequest, policy: GatewayPolicy) -> None:
    names = [tool.name for tool in request.allowed_tools]
    if len(names) != len(set(names)):
        raise ToolNotAllowedError("duplicate tool in request")
    if any(name not in policy.tool_allowlist for name in names):
        raise ToolNotAllowedError()


def _check_budget(request: GenerateRequest) -> None:
    budget = request.budget
    ceilings = request.runtime_config.token_ceilings
    # The caller-supplied budget can never be looser than the audited config.
    if (
        budget.per_call_max_input_tokens > ceilings.planner_input_tokens
        or budget.per_call_max_output_tokens > ceilings.planner_output_tokens
    ):
        raise BudgetExceededError("per-call ceilings exceed the runtime config")
    if budget.calls_remaining < 1:
        raise BudgetExceededError("model call budget exhausted")
    if request.escalation and budget.escalations_remaining < 1:
        raise BudgetExceededError("escalation budget exhausted")
    if budget.seconds_remaining <= 0:
        raise BudgetExceededError("wall-clock budget exhausted")
    # The caller's estimate is only a floor; the bound counts every character sent.
    if request.input_token_bound > min(
        budget.per_call_max_input_tokens, budget.input_tokens_remaining
    ):
        raise BudgetExceededError("input token budget would be exceeded")
    if request.max_output_tokens > min(
        budget.per_call_max_output_tokens, budget.output_tokens_remaining
    ):
        raise BudgetExceededError("output token budget would be exceeded")
