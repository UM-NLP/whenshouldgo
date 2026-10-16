"""The model-and-tool part of a turn (docs/model-hosting-decision.md; decision 17).

Phase 0 design decision (owner-level, reversible via ``RuntimeConfig.maps_content_to_model``):
THE MODEL NEVER SEES GOOGLE MAPS CONTENT. The model (1) interprets the user's message into a
``WorkspacePatch`` (FRAME) and (2) formulates the arguments of the discovery tool call. The
server runs the tool through the Grounding Lite client, validates the answer against the run
ledger, and returns Google's grounded text verbatim with fixed, server-authored framing. No tool
result is ever put into a gateway request.

Order: availability checks and cost ceilings (no provider call yet) -> FRAME call -> output
validation -> patch validation (nothing is stored yet) -> tool allowlist -> arguments -> tool
call under a per-run ``ToolRun`` -> parse -> ledger-bound validation -> verbatim render and check
-> display cache. A failure at any step raises a typed ``TurnFailure`` and nothing is stored;
work already done is recorded in the cost ledger, and a failure after a provider call was sent is
also counted as a PAID FAILED turn (the user allowance is refunded; the ledger is the abuse
boundary). The one partial result is tool-budget exhaustion (tool calls or the run's wall-clock
deadline): the validated patch is kept and the response carries a typed warning, never a
half-validated block.

Maps text exists in this module only inside the ``ValidatedAnswer`` and the ``DiscoveryBlock``
it returns; it reaches no log, event, exception, cost entry or gateway request.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter

from pydantic import ValidationError

from . import telemetry
from .costs import CostLedger
from .discovery import (
    DisplayCache,
    GroundedAnswerRejected,
    GroundedAnswerValidator,
    GroundedParseError,
    GroundedTextPart,
    GroundingLiteClient,
    InvalidToolArguments,
    SourcePart,
    ToolBudgetExceeded,
    ToolId,
    ToolNotAllowed,
    ToolRun,
    VerbatimViolation,
    parse_grounded_answer,
    render_grounded_block,
    verify_verbatim,
)
from .discovery.errors import (
    CacheError,
    DiscoveryError,
    DiscoveryRateLimited,
    DiscoveryTimeout,
)
from .discovery.render import FRAMING_TEXT, RenderedBlock
from .domain import (
    DiscoveryBlock,
    DiscoveryPlace,
    ErrorCode,
    ErrorReason,
    FramingKind,
    Principal,
    RuntimeConfig,
    TripWorkspace,
    TurnWarning,
)
from .entitlements import DailyTokenCeiling
from .failures import MapsContentToModelNotImplemented, TurnFailure
from .frame import (
    ArgumentRejected,
    FrameIntent,
    FrameResult,
    build_frame_request,
    search_arguments,
)
from .gateway import (
    BudgetExceededError,
    ContentFilterError,
    GatewayError,
    GenerateRequest,
    MalformedOutputError,
    ModelGateway,
    PrivacyViolationError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RunBudget,
    TaintedContextError,
    ToolNotAllowedError,
    TruncationError,
    UsageMismatchError,
)
from .gateway.types import ModelTurn, Usage
from .patching import PatchRejected, apply_patch

# Server-owned grants: which Grounding Lite tools each guard-level tool name allows. The model
# can request a tool by name; only a tool granted here is ever executed. resolve_names and
# resolve_maps_urls are NOT granted in Phase 0 (TODO: typed pass-through), and weather, route and
# walking checks are not wired into this step.
_TOOL_GRANTS: dict[str, frozenset[ToolId]] = {
    "maps_grounded_discovery": frozenset({ToolId.SEARCH_PLACES}),
}

REPLY_OTHER = (
    "I'll look for same-day Toronto/GTA options and keep Toronto/GTA as an editable "
    "assumption. What area are you starting from?"
)
REPLY_PARTIAL = (
    "I saved what you told me, but I couldn't look up places right now. Please try again "
    "in a moment."
)
# Rolling windows of the internal abuse boundary (docs/access-tiers.md: daily ceilings are
# separate from the user-facing allowance). The daily token ceilings come from the entitlement
# policy; the paid-failed-turn limit is ``RuntimeConfig.max_paid_failed_turns_per_hour``.
DAILY_WINDOW = timedelta(hours=24)
PAID_FAILURE_WINDOW = timedelta(hours=1)

# Failures that mean a provider attempt was made even when the adapter counted no call.
_ATTEMPTED = (
    ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError,
    ProviderRejectedError, ProviderQuotaError, TruncationError, ContentFilterError,
    MalformedOutputError, UsageMismatchError,
)


def tools_for_grants(tools: tuple[str, ...]) -> frozenset[ToolId]:
    """Grounding Lite tools the server allows for this turn's guard-level tool names."""
    granted: set[ToolId] = set()
    for name in tools:
        granted |= _TOOL_GRANTS.get(name, frozenset())
    return frozenset(granted)


@dataclass(frozen=True)
class DiscoveryRef:
    """What the replay record keeps of a discovery block: ids and hashes, never content."""

    cache_key: str
    framing: FramingKind
    content_hash: str
    display_hash: str
    cache_expires_at: datetime


@dataclass(frozen=True)
class PipelineOutcome:
    workspace: TripWorkspace
    reply: str
    discovery: DiscoveryBlock | None
    ref: DiscoveryRef | None
    warnings: tuple[TurnWarning, ...] = ()


@dataclass
class _Spend:
    """Whether this run sent a provider call (a later failure is then a paid failed turn)."""

    paid: bool = False


def failure_from_gateway(error: GatewayError) -> TurnFailure:
    if isinstance(error, ProviderRateLimitError):
        return TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_RATE_LIMITED)
    if isinstance(error, ProviderTimeoutError):
        return TurnFailure(504, ErrorCode.PROVIDER, ErrorReason.PROVIDER_TIMEOUT)
    if isinstance(error, TruncationError):
        return TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.OUTPUT_TRUNCATED)
    if isinstance(error, (MalformedOutputError, ContentFilterError)):
        return TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.OUTPUT_MALFORMED)
    if isinstance(error, BudgetExceededError):
        return TurnFailure(429, ErrorCode.QUOTA, ErrorReason.OVER_BUDGET)
    if isinstance(error, (PrivacyViolationError, ToolNotAllowedError, TaintedContextError)):
        return TurnFailure(500, ErrorCode.PROVIDER, None)  # a server-side fault: no cause shown
    return TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_UNAVAILABLE)


def failure_from_discovery(error: DiscoveryError) -> TurnFailure:
    if isinstance(error, DiscoveryRateLimited):
        return TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_RATE_LIMITED)
    if isinstance(error, DiscoveryTimeout):
        return TurnFailure(504, ErrorCode.PROVIDER, ErrorReason.PROVIDER_TIMEOUT)
    if isinstance(error, ToolNotAllowed):
        return TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.TOOL_NOT_ALLOWED)
    if isinstance(error, InvalidToolArguments):
        return TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.OUTPUT_MALFORMED)
    if error.code == ErrorCode.VALIDATION:  # malformed or oversize tool result, tool error
        return TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.TOOL_RESULT_INVALID)
    # Access denied, a tripped kill switch, a missing session, 5xx and the rest look the same to
    # a client (and never reveal our credentials); telemetry keeps the distinction.
    return TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.DISCOVERY_UNAVAILABLE)


def to_api_block(
    rendered: RenderedBlock, cache_expires_at: datetime, retrieved_at: datetime
) -> DiscoveryBlock:
    """The API shape of a verified verbatim block: raw text, then the sources in order."""
    text_part = rendered.parts[0]
    if not isinstance(text_part, GroundedTextPart) or rendered.framing is None:
        raise ValueError("block is not text-then-sources")
    sources = rendered.parts[1:]
    if not all(isinstance(p, SourcePart) for p in sources):
        raise ValueError("block is not text-then-sources")
    return DiscoveryBlock(
        text=text_part.text,
        places=tuple(
            DiscoveryPlace(title=p.attribution_title, attribution_url=p.attribution_url,
                           place_url=p.place_url)
            for p in sources if isinstance(p, SourcePart)
        ),
        framing=rendered.framing,
        retrieved_at=retrieved_at,
        cache_expires_at=cache_expires_at,
    )


class TurnPipeline:
    def __init__(
        self,
        *,
        gateway: ModelGateway,
        discovery: GroundingLiteClient | None,
        cache: DisplayCache | None,
        config: RuntimeConfig,
        costs: CostLedger,
        sink: telemetry.TraceSink,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float] = perf_counter,
    ) -> None:
        self._gateway = gateway
        self._discovery = discovery
        self._cache = cache
        self._config = config
        self._costs = costs
        self._sink = sink
        self._clock = clock
        self._monotonic = monotonic
        self._validator = GroundedAnswerValidator(config.ttls, sink=sink)
        self.assert_maps_policy()

    def assert_maps_policy(self) -> None:
        """Phase 0 never puts tool results in a gateway request (decision 17, question 2)."""
        if self._config.maps_content_to_model is not False:
            raise MapsContentToModelNotImplemented()

    # -- telemetry (typed fields only) ----------------------------------------------------------
    def _trace(self, event_cls: type, **fields: object) -> None:
        telemetry.trace(event_cls, self._sink, trace_id=telemetry.current_trace_id(),
                        runtime_config_version=self._config.version, **fields)

    def _reject(self, reason: telemetry.ValidationReason, phase: telemetry.Phase) -> None:
        self._trace(telemetry.ValidationRejection, reason=reason, phase=phase)

    # -- checks that need no provider call --------------------------------------------------------
    def _check_available(self, allowed: frozenset[ToolId]) -> None:
        """Before FRAME, in this order: discovery client configured, discovery not tripped,
        gateway quota latch not tripped.

        Intent is known only AFTER FRAME runs, so the rule is by class: every turn that reaches
        the pipeline is TRAVEL, and a TRAVEL turn is discovery-class whenever the server
        allowlist grants search_places. A discovery-class turn is short-circuited (zero gateway
        calls) when discovery is not configured or is switched off; a turn whose allowlist
        grants no discovery tool could proceed without it and is not. A latched gateway stops
        every turn: no FRAME call can succeed."""
        if ToolId.SEARCH_PLACES in allowed:
            if self._discovery is None or self._cache is None:
                raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.DISCOVERY_NOT_CONFIGURED)
            if self._discovery.tripped:
                self._trace(telemetry.ToolCall, tool=telemetry.ToolName.SEARCH_PLACES,
                            outcome=telemetry.ToolOutcome.DENIED, latency_ms=0, billable_units=0)
                raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.DISCOVERY_UNAVAILABLE)
        if getattr(self._gateway, "latched", False):
            raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_UNAVAILABLE)

    def _check_ceilings(
        self, principal: Principal, request: GenerateRequest, daily: DailyTokenCeiling | None
    ) -> None:
        """The cost ledger is the abuse boundary: the daily token ceiling (including the worst
        case of the coming FRAME call) and the paid-failed-turn limiter, both before FRAME."""
        over = TurnFailure(429, ErrorCode.QUOTA, ErrorReason.OVER_BUDGET)
        if daily is None:  # no entitlement for this tier: no paid calls
            raise over
        used = self._costs.usage_for(principal.uid, window=DAILY_WINDOW)
        worst = request.worst_case_usage
        if (
            used.input_tokens + worst.input_tokens > daily.input_tokens
            or used.output_tokens + worst.output_tokens > daily.output_tokens
        ):
            raise over
        failed = self._costs.failed_paid_turns(principal.uid, window=PAID_FAILURE_WINDOW)
        if failed >= self._config.max_paid_failed_turns_per_hour:
            raise over

    # -- the run ----------------------------------------------------------------------------------
    def run(
        self, principal: Principal, message: str, base: TripWorkspace, tools: tuple[str, ...],
        cache_key: str, *, daily_ceiling: DailyTokenCeiling | None,
    ) -> PipelineOutcome:
        self.assert_maps_policy()
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        allowed = tools_for_grants(tools)
        self._check_available(allowed)
        budget = RunBudget.from_ceilings(self._config.token_ceilings)
        request = build_frame_request(message, base, self._config, budget)
        self._check_ceilings(principal, request, daily_ceiling)
        spend = _Spend()
        try:
            return self._run(principal, message, base, allowed, run_id, cache_key, request,
                             budget, spend)
        except Exception:
            if spend.paid:  # a provider call was sent: the ledger counts the failed turn
                self._costs.record_failed_paid_turn(principal.uid, run_id)
            raise

    def _run(
        self, principal: Principal, message: str, base: TripWorkspace, allowed: frozenset[ToolId],
        run_id: str, cache_key: str, request: GenerateRequest, budget: RunBudget, spend: _Spend,
    ) -> PipelineOutcome:
        started = self._monotonic()
        frame = self._frame(principal, request, run_id, spend)
        workspace: TripWorkspace | None = None
        rejection: PatchRejected | None = None
        try:
            workspace = apply_patch(base, frame.patch)  # a copy: nothing is stored yet
        except PatchRejected as rejected:
            rejection = rejected
        if workspace is None or rejection is not None:
            reason = rejection.reason if rejection else telemetry.ValidationReason.OTHER
            self._reject(reason, telemetry.Phase.PATCH)
            raise TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.PATCH_REJECTED)
        if frame.intent == FrameIntent.OTHER:
            return PipelineOutcome(workspace, REPLY_OTHER, None, None)
        # One wall-clock deadline for FRAME plus the tool call (the run budget's seconds). The
        # tool call itself is bounded by the client's own timeout (at most 10 s).
        if self._monotonic() - started >= budget.seconds_remaining:
            return PipelineOutcome(workspace, REPLY_PARTIAL, None, None,
                                   (TurnWarning.TOOL_BUDGET_EXHAUSTED,))
        return self._discover(principal, message, frame, workspace, allowed, run_id, cache_key,
                              spend)

    def _frame(
        self, principal: Principal, request: GenerateRequest, run_id: str, spend: _Spend
    ) -> FrameResult:
        started = perf_counter()
        turn: ModelTurn | None = None
        failure: GatewayError | None = None
        try:
            turn = self._gateway.generate(request)
        except GatewayError as error:
            failure = error
        if failure is not None:
            self._record_gateway_error(principal, run_id, request, failure, started, spend)
            raise failure_from_gateway(failure)
        assert turn is not None
        spend.paid = True
        self._record_gateway_turn(principal, run_id, turn)
        frame = self._checked_frame(turn)
        if frame is None:
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.MODEL)
            raise TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.OUTPUT_MALFORMED)
        return frame

    @staticmethod
    def _checked_frame(turn: ModelTurn) -> FrameResult | None:
        """The model's output as a FrameResult, re-validated from its dump so an object built
        around the validators (or a different model class) cannot get through."""
        output = turn.output
        if not isinstance(output, FrameResult):
            return None
        try:
            return FrameResult.model_validate(output.model_dump())
        except ValidationError:
            return None

    def _record_gateway_turn(self, principal: Principal, run_id: str, turn: ModelTurn) -> None:
        meta = turn.telemetry
        self._trace(
            telemetry.GatewayCall,
            model_id=meta.model_id, provider=meta.provider,
            finish_reason=telemetry.FinishReason(meta.finish_reason.value),
            input_tokens=meta.input_tokens, output_tokens=meta.output_tokens,
            latency_ms=int(meta.latency_ms),
            cost_microusd=int(meta.estimated_cost_usd * 1_000_000),
            call_role=telemetry.CallRole(meta.call_role.value),
            model_calls=meta.model_calls, repairs=meta.repairs,
            price_table_version=meta.price_table_version,
        )
        self._costs.record_gateway(principal.uid, run_id, meta.call_role.value, turn.usage,
                                   turn.model_calls, turn.estimated_cost_usd)

    @staticmethod
    def _charge_for(error: GatewayError, request: GenerateRequest) -> tuple[Usage, int] | None:
        """What a failed gateway call costs. Trustworthy usage from the gateway is kept; a
        rejection before processing costs no tokens but is a call; any other failed attempt whose
        usage is unknown (or reported as zero) is charged at the request's worst case (estimated
        input bound plus max output tokens) with at least one call, even when the adapter
        counted none. Usage(0, 0) is never recorded for a call that was sent. A refusal before
        anything was sent is not charged."""
        usage = error.usage
        if usage is not None and (usage.input_tokens or usage.output_tokens):
            return usage, error.calls
        if usage is not None and error.rejected_before_processing:
            return usage, max(error.calls, 1)
        if error.calls > 0 or isinstance(error, _ATTEMPTED):
            return request.worst_case_usage, max(error.calls, 1)
        return None

    def _record_gateway_error(
        self, principal: Principal, run_id: str, request: GenerateRequest, error: GatewayError,
        started: float, spend: _Spend,
    ) -> None:
        charge = self._charge_for(error, request)
        usage, calls = charge if charge is not None else (Usage(input_tokens=0, output_tokens=0), 0)
        self._trace(
            telemetry.GatewayCall,
            model_id=self._config.model_id, provider=self._config.provider,
            finish_reason=telemetry.FinishReason.ERROR,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            latency_ms=int((perf_counter() - started) * 1000), cost_microusd=0,
            call_role=telemetry.CallRole.FRAME, model_calls=min(calls, 100), repairs=0,
            price_table_version=self._config.price_table_version,
        )
        if charge is not None:
            spend.paid = True
            self._costs.record_gateway(principal.uid, run_id, "FRAME", usage, calls)

    def _discover(
        self, principal: Principal, message: str, frame: FrameResult, workspace: TripWorkspace,
        allowed: frozenset[ToolId], run_id: str, cache_key: str, spend: _Spend,
    ) -> PipelineOutcome:
        tool = frame.tool
        if tool is None or tool not in allowed or tool != ToolId.SEARCH_PLACES:
            self._reject(telemetry.ValidationReason.TOOL_NOT_ALLOWED, telemetry.Phase.TOOL)
            if tool is not None:
                self._trace(telemetry.ToolCall, tool=telemetry.ToolName(tool.value),
                            outcome=telemetry.ToolOutcome.DENIED, latency_ms=0, billable_units=0)
            raise TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.TOOL_NOT_ALLOWED)
        if self._discovery is None or self._cache is None:
            raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.DISCOVERY_NOT_CONFIGURED)
        assert frame.search is not None
        try:
            arguments = search_arguments(frame.search, message)
            arguments_error = False
        except ArgumentRejected:
            arguments, arguments_error = {}, True
        if arguments_error:
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
            raise TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.OUTPUT_MALFORMED)
        try:
            run = ToolRun.from_config(self._config, principal_id=principal.uid, run_id=run_id)
            run_error = False
        except ValueError:  # a principal id outside the bounded identifier form
            run, run_error = None, True
        if run is None or run_error:
            raise TurnFailure(500, ErrorCode.PROVIDER, None)
        return self._search(principal, workspace, run, arguments, cache_key, spend)

    def _search(
        self, principal: Principal, workspace: TripWorkspace, run: ToolRun,
        arguments: dict[str, object], cache_key: str, spend: _Spend,
    ) -> PipelineOutcome:
        assert self._discovery is not None
        result = None
        failure: DiscoveryError | None = None
        try:
            result = self._discovery.call_tool(run, ToolId.SEARCH_PLACES.value, arguments)
        except DiscoveryError as error:
            failure = error
        if failure is not None:
            return self._tool_failed(principal, workspace, run, failure, spend)
        spend.paid = True
        self._costs.record_tool(principal.uid, run.run_id, ToolId.SEARCH_PLACES.value,
                                run.calls_used, result.billable_units)
        return self._validated(principal, workspace, run, result, cache_key)

    def _tool_failed(
        self, principal: Principal, workspace: TripWorkspace, run: ToolRun, failure: DiscoveryError,
        spend: _Spend,
    ) -> PipelineOutcome:
        if failure.calls:
            spend.paid = True
            self._costs.record_tool(principal.uid, run.run_id, ToolId.SEARCH_PLACES.value,
                                    failure.calls, 0)
        if isinstance(failure, ToolBudgetExceeded):
            # Best validated partial result: the patch was validated, discovery was refused.
            return PipelineOutcome(workspace, REPLY_PARTIAL, None, None,
                                   (TurnWarning.TOOL_BUDGET_EXHAUSTED,))
        mapped = failure_from_discovery(failure)
        if mapped.reason == ErrorReason.TOOL_NOT_ALLOWED:
            self._reject(telemetry.ValidationReason.TOOL_NOT_ALLOWED, telemetry.Phase.TOOL)
        elif mapped.reason == ErrorReason.OUTPUT_MALFORMED:
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
        raise mapped

    def _validated(
        self, principal: Principal, workspace: TripWorkspace, run: ToolRun, result: object,
        cache_key: str,
    ) -> PipelineOutcome:
        assert self._cache is not None
        framing = FramingKind.RESULTS_INTRO
        failure: TurnFailure | None = None
        block: DiscoveryBlock | None = None
        ref: DiscoveryRef | None = None
        try:
            answer = parse_grounded_answer(result, self._config.ttls)
            # The validator emits its own ValidationRejection event on refusal.
            validated = self._validator.validate(answer, run, self._clock(), principal_id=principal.uid)
            rendered = render_grounded_block(validated, framing)
            verify_verbatim(rendered, validated, run)
            self._cache.put(principal.uid, cache_key, validated)
            block = to_api_block(rendered, validated.answer.cache_expires_at,
                                 validated.answer.retrieved_at)
            ref = DiscoveryRef(cache_key, framing, validated.content_hash, validated.display_hash,
                               validated.answer.cache_expires_at)
        except GroundedParseError:
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
            failure = TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.TOOL_RESULT_INVALID)
        except (GroundedAnswerRejected, VerbatimViolation):
            failure = TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.TOOL_RESULT_INVALID)
        except CacheError:
            failure = TurnFailure(500, ErrorCode.PROVIDER, None)
        except ValueError:  # includes pydantic ValidationError from the block or a renderer check
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
            failure = TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.TOOL_RESULT_INVALID)
        if failure is not None:
            raise failure  # raised outside the except blocks: no exception context
        assert block is not None and ref is not None
        return PipelineOutcome(workspace, FRAMING_TEXT[framing], block, ref)
