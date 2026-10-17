"""The model-and-tool part of a turn (docs/model-hosting-decision.md; decision 17).

Phase 0 design decision (owner-level, reversible via ``RuntimeConfig.maps_content_to_model``):
THE MODEL NEVER SEES GOOGLE MAPS CONTENT. The model (1) interprets the user's message into a
``WorkspacePatch`` (FRAME) and (2) formulates the arguments of the discovery tool call. The
server runs the tool through the Grounding Lite client, validates the answer against the run
ledger, and returns Google's grounded text verbatim with fixed, server-authored framing. No tool
result is ever put into a gateway request.

Order: identifier checks, availability checks and cost ceilings (no provider call yet) -> FRAME
call -> output validation -> patch validation (nothing is stored yet) -> tool allowlist ->
arguments -> tool call under a per-run ``ToolRun`` with the remaining seconds as its timeout ->
parse -> ledger-bound validation -> verbatim render and check -> display cache. A failure at any
step is a typed ``TurnFailure`` and nothing is stored; work already done is recorded in the cost
ledger. A failure of an ABUSE-SHAPED kind after a provider call was sent (rejected or malformed
model output, a rejected patch or tool arguments, an invalid tool result) is also counted for the
paid-failed-turn limiter; provider outages, rate limits, quota or latch refusals, a tripped kill
switch and deadline partials are never counted. The user allowance is refunded either way; the
ledger is the abuse boundary. The one partial result is tool-budget exhaustion (tool calls or the
run's wall-clock deadline): the validated patch is kept and the response carries a typed warning.

Maps text exists in this module only inside the ``ValidatedAnswer`` and the ``DiscoveryBlock``
it returns; it reaches no log, event, exception, cost entry or gateway request. The discovery
path RETURNS failures instead of raising them, so no frame on a raised exception's traceback
holds a Maps-carrying local.

Not covered here (separate work in a shared store, tracked elsewhere): guest identity churn, a
whole-pilot spend counter, IP-bucket limits, App Check and the Firestore-backed ledger.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import perf_counter

from pydantic import ValidationError

from . import telemetry
from .costs import Attribution, CostLedger
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
    plan_search,
)
from .gateway import (
    BudgetExceededError,
    ContentFilterError,
    GatewayError,
    GenerateRequest,
    MalformedOutputError,
    ModelGateway,
    PrivacyViolationError,
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
from .patching import PatchRejected, apply_patch_checked

# Server-owned grants: which Grounding Lite tools each guard-level tool name allows. The model
# can request a tool by name; only a tool granted here is ever executed. resolve_names and
# resolve_maps_urls are NOT granted in Phase 0 (TODO: typed pass-through), and weather, route and
# walking checks are not wired into this step.
_TOOL_GRANTS: dict[str, frozenset[ToolId]] = {
    "maps_grounded_discovery": frozenset({ToolId.SEARCH_PLACES}),
}

# FRAME returned intent OTHER for a message the conservative guard let through: not an outing
# request the model can search for. One fixed, travel-focused clarification (no tool, no
# discovery call, never general conversation). It is a completed turn: the model was called and
# the user got a response, so it counts like any other (docs/access-tiers.md).
REPLY_OTHER = (
    "I can help plan same-day outings in Toronto/GTA. Tell me what you'd like to do or where "
    "you'd like to go, and I'll keep Toronto/GTA as an editable assumption. "
    "What area are you starting from?"
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
MAX_RETRY_AFTER_SECONDS = 3600
# PLACEHOLDER (owner-tunable): below this many seconds left in the run the tool is skipped and
# the validated partial result is returned (a search needs at least about a second).
MIN_TOOL_SECONDS = 1.0
# Only these public reasons count toward the paid-failed-turn limiter (abuse-shaped failures).
_ABUSE_REASONS = frozenset({ErrorReason.OUTPUT_REJECTED, ErrorReason.OUTPUT_TRUNCATED})

# Failures that mean a provider attempt was made even when the adapter counted no call. A
# ProviderQuotaError is NOT here: with no call counted it is the pre-send latch refusal.
_ATTEMPTED = (
    ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError,
    ProviderRejectedError, TruncationError, ContentFilterError, MalformedOutputError,
    UsageMismatchError,
)

_SERVER_FAULT = (500, ErrorCode.PROVIDER, None)  # a fault a client must not learn the cause of


def _fault() -> TurnFailure:
    return TurnFailure(*_SERVER_FAULT)


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
    """Whether this run sent a provider call (a later failure may then be a paid failure), and
    the cost rows it produced. Rows are written to the ledger only when the turn's outcome is
    known, because WHO the spend is attributed to depends on that outcome."""

    paid: bool = False
    gateway: list = field(default_factory=list)  # (label, usage, calls, cost)
    tools: list = field(default_factory=list)  # (label, calls, units)


def _rejected(status: int = 502) -> TurnFailure:
    """The one public validation-stage refusal (a 503 keeps the PROVIDER code: unavailable)."""
    code = ErrorCode.PROVIDER if status == 503 else ErrorCode.VALIDATION
    return TurnFailure(status, code, ErrorReason.OUTPUT_REJECTED)


def failure_from_gateway(error: GatewayError) -> TurnFailure:
    if isinstance(error, ProviderRateLimitError):
        return TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_RATE_LIMITED)
    if isinstance(error, ProviderTimeoutError):
        return TurnFailure(504, ErrorCode.PROVIDER, ErrorReason.PROVIDER_TIMEOUT)
    if isinstance(error, TruncationError):
        return TurnFailure(502, ErrorCode.VALIDATION, ErrorReason.OUTPUT_TRUNCATED)
    if isinstance(error, (MalformedOutputError, ContentFilterError)):
        return _rejected()
    if isinstance(error, BudgetExceededError):
        return TurnFailure(429, ErrorCode.QUOTA, ErrorReason.OVER_BUDGET)
    if isinstance(error, (PrivacyViolationError, ToolNotAllowedError, TaintedContextError)):
        return _fault()
    return TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_UNAVAILABLE)


def failure_from_discovery(error: DiscoveryError) -> TurnFailure:
    if isinstance(error, DiscoveryRateLimited):
        return TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_RATE_LIMITED)
    if isinstance(error, DiscoveryTimeout):
        return TurnFailure(504, ErrorCode.PROVIDER, ErrorReason.PROVIDER_TIMEOUT)
    if isinstance(error, (ToolNotAllowed, InvalidToolArguments)):
        return _rejected()
    if error.code == ErrorCode.VALIDATION:  # malformed or oversize tool result, tool error
        return _rejected()
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

    def _deny(self, tool: telemetry.ToolName) -> None:
        self._trace(telemetry.ToolCall, tool=tool, outcome=telemetry.ToolOutcome.DENIED,
                    latency_ms=0, billable_units=0)

    # -- checks that need no provider call --------------------------------------------------------
    def _make_run(self, principal: Principal, run_id: str, cache_key: str) -> ToolRun:
        """Validate the identifiers BEFORE any paid work: a principal id or cache key the tool run
        or the display cache would refuse later must fail now, with no provider call spent."""
        try:
            run = ToolRun.from_config(self._config, principal_id=principal.uid, run_id=run_id)
            if self._cache is not None:
                self._cache.get(principal.uid, cache_key)  # validates owner and key; a read only
        except (ValueError, CacheError):
            raise _fault() from None
        return run

    def _check_available(self, allowed: frozenset[ToolId]) -> None:
        """Before FRAME, in this order: discovery client configured, discovery not tripped,
        gateway quota latch not tripped.

        Intent is known only AFTER FRAME runs, so the rule is by class: every turn that reaches
        the pipeline is TRAVEL, and a TRAVEL turn is discovery-class whenever the server
        allowlist grants search_places. A discovery-class turn is short-circuited (zero gateway
        calls) when discovery is not configured or is switched off; a turn whose allowlist
        grants no discovery tool could proceed without it and is not. A latched gateway stops
        every turn: no FRAME call can succeed, and a gateway that does not report its latch is
        treated as latched (fail closed)."""
        if ToolId.SEARCH_PLACES in allowed:
            if self._discovery is None or self._cache is None:
                self._deny(telemetry.ToolName.SEARCH_PLACES)
                raise _rejected(503)
            if self._discovery.tripped:
                self._deny(telemetry.ToolName.SEARCH_PLACES)
                raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.DISCOVERY_UNAVAILABLE)
        if getattr(self._gateway, "latched", True):
            raise TurnFailure(503, ErrorCode.PROVIDER, ErrorReason.PROVIDER_UNAVAILABLE)

    def _retry_after(self, oldest: datetime | None, window: timedelta, default: int) -> int:
        if oldest is None:
            return default
        seconds = (oldest + window - self._costs.now()).total_seconds()
        return int(min(MAX_RETRY_AFTER_SECONDS, max(1, math.ceil(seconds))))

    def _check_ceilings(
        self, principal: Principal, request: GenerateRequest, daily: DailyTokenCeiling | None
    ) -> None:
        """The cost ledger is the abuse boundary: the daily token ceiling (including the worst
        case of the coming FRAME call) and the paid-failed-turn limiter, both before FRAME. A
        denial is 429 RATE_LIMITED with a bounded Retry-After."""
        if daily is None:  # no entitlement for this tier: no paid calls
            raise TurnFailure(429, ErrorCode.QUOTA, ErrorReason.RATE_LIMITED,
                              retry_after=MAX_RETRY_AFTER_SECONDS)
        # Only the user's own spend counts; provider-side failures are recorded but not charged.
        used = self._costs.usage_for(principal.uid, window=DAILY_WINDOW,
                                     user_attributable_only=True)
        worst = request.worst_case_usage
        if (
            used.input_tokens + worst.input_tokens > daily.input_tokens
            or used.output_tokens + worst.output_tokens > daily.output_tokens
        ):
            oldest = self._costs.oldest_usage_at(principal.uid, window=DAILY_WINDOW)
            raise TurnFailure(429, ErrorCode.QUOTA, ErrorReason.RATE_LIMITED,
                              retry_after=self._retry_after(oldest, DAILY_WINDOW,
                                                            MAX_RETRY_AFTER_SECONDS))
        failed = self._costs.failed_paid_turns(principal.uid, window=PAID_FAILURE_WINDOW)
        if failed >= self._config.max_paid_failed_turns_per_hour:
            oldest = self._costs.oldest_failed_paid_turn_at(principal.uid, window=PAID_FAILURE_WINDOW)
            raise TurnFailure(429, ErrorCode.QUOTA, ErrorReason.RATE_LIMITED,
                              retry_after=self._retry_after(oldest, PAID_FAILURE_WINDOW, 60))

    # -- the run ----------------------------------------------------------------------------------
    def run(
        self, principal: Principal, message: str, base: TripWorkspace, tools: tuple[str, ...],
        cache_key: str, *, daily_ceiling: DailyTokenCeiling | None,
    ) -> PipelineOutcome:
        self.assert_maps_policy()
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        tool_run = self._make_run(principal, run_id, cache_key)
        allowed = tools_for_grants(tools)
        self._check_available(allowed)
        budget = RunBudget.from_ceilings(self._config.token_ceilings)
        request = build_frame_request(message, base, self._config, budget)
        self._check_ceilings(principal, request, daily_ceiling)
        spend = _Spend()
        try:
            outcome = self._run(principal, message, base, allowed, tool_run, cache_key, request,
                                budget, spend)
        except TurnFailure as failure:
            self._flush(principal, run_id, spend, self._failure_attribution(failure))
            self._note_failure(principal, run_id, failure, spend)
            raise
        except Exception:
            self._flush(principal, run_id, spend, Attribution.PROVIDER)  # not the user's doing
            raise
        if isinstance(outcome, TurnFailure):
            self._flush(principal, run_id, spend, self._failure_attribution(outcome))
            self._note_failure(principal, run_id, outcome, spend)
            raise outcome
        # A deadline or tool-budget partial is a provider-side shortfall, not the user's spend.
        partial = TurnWarning.TOOL_BUDGET_EXHAUSTED in outcome.warnings
        self._flush(principal, run_id, spend, Attribution.PROVIDER if partial else Attribution.USER)
        return outcome

    @staticmethod
    def _failure_attribution(failure: TurnFailure) -> Attribution:
        """USER only for failures the user's own input can cause (rejected or malformed output,
        a rejected patch or arguments, truncation). Truncation stays USER-attributed because a
        cut-off answer cannot be told apart from a model that rambled on hostile input (the output
        cap is ours); every provider-side failure, including a call that failed without abuse-
        shaped input, is PROVIDER-attributed."""
        return Attribution.USER if failure.reason in _ABUSE_REASONS else Attribution.PROVIDER

    def _flush(
        self, principal: Principal, run_id: str, spend: _Spend, attribution: Attribution
    ) -> None:
        """Write the run's cost rows with the turn's attribution. Spend is always recorded
        (worst-case rules unchanged); attribution only decides whether it counts toward the
        user's ceiling."""
        for label, usage, calls, cost in spend.gateway:
            self._costs.record_gateway(principal.uid, run_id, label, usage, calls, cost,
                                       attribution=attribution)
        for label, calls, units in spend.tools:
            self._costs.record_tool(principal.uid, run_id, label, calls, units,
                                    attribution=attribution)
        spend.gateway.clear()
        spend.tools.clear()

    def _note_failure(
        self, principal: Principal, run_id: str, failure: TurnFailure, spend: _Spend
    ) -> None:
        """Count an abuse-shaped failure after a provider call was sent. Outages, rate limits,
        quota or latch refusals, a kill switch and the like are never counted."""
        if spend.paid and failure.reason in _ABUSE_REASONS:
            self._costs.record_failed_paid_turn(principal.uid, run_id)

    def _run(
        self, principal: Principal, message: str, base: TripWorkspace, allowed: frozenset[ToolId],
        tool_run: ToolRun, cache_key: str, request: GenerateRequest, budget: RunBudget,
        spend: _Spend,
    ) -> PipelineOutcome | TurnFailure:
        started = self._monotonic()
        frame = self._frame(principal, request, tool_run.run_id, spend)
        workspace: TripWorkspace | None = None
        dropped = 0
        rejection: PatchRejected | None = None
        try:
            workspace, dropped = apply_patch_checked(base, frame.patch)  # a copy: not stored yet
        except PatchRejected as rejected:
            rejection = rejected
        if workspace is None or rejection is not None:
            reason = rejection.reason if rejection else telemetry.ValidationReason.OTHER
            self._reject(reason, telemetry.Phase.PATCH)
            return _rejected()
        warnings: tuple[TurnWarning, ...] = (TurnWarning.FACT_DROPPED,) if dropped else ()
        if frame.intent == FrameIntent.OTHER:
            return PipelineOutcome(workspace, REPLY_OTHER, None, None, warnings)
        # One wall-clock deadline for FRAME plus the tool call (the run budget's seconds): the
        # tool gets what is left as its timeout, and under a floor it is skipped.
        remaining = budget.seconds_remaining - (self._monotonic() - started)
        if remaining < MIN_TOOL_SECONDS:
            return PipelineOutcome(workspace, REPLY_PARTIAL, None, None,
                                   (*warnings, TurnWarning.TOOL_BUDGET_EXHAUSTED))
        outcome = self._discover(principal, message, frame, workspace, allowed, tool_run,
                                 cache_key, spend, remaining)
        if isinstance(outcome, PipelineOutcome) and warnings:
            return PipelineOutcome(outcome.workspace, outcome.reply, outcome.discovery,
                                   outcome.ref, (*warnings, *outcome.warnings))
        return outcome

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
        if turn is None:
            raise _fault()
        spend.paid = True
        self._record_gateway_turn(run_id, request, turn, spend)
        frame = self._checked_frame(turn)
        if frame is None:
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.MODEL)
            raise _rejected()
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

    @staticmethod
    def _trusted_usage(reported: Usage, request: GenerateRequest, calls: int = 1) -> Usage:
        """Provider usage is not taken at face value: input is at least half of the request's own
        estimate (a deflated body cannot make a call free) and every figure is capped at the
        request's worst case PER CALL SENT (an inflated body cannot blow the ledger)."""
        worst = request.worst_case_usage
        sent = max(1, calls)
        floor = request.estimated_input_tokens // 2
        return Usage(
            input_tokens=min(max(reported.input_tokens, floor), worst.input_tokens * sent),
            output_tokens=min(reported.output_tokens, worst.output_tokens * sent),
        )

    def _record_gateway_turn(
        self, run_id: str, request: GenerateRequest, turn: ModelTurn, spend: _Spend
    ) -> None:
        meta = turn.telemetry
        usage = self._trusted_usage(turn.usage, request, turn.model_calls)
        self._trace(
            telemetry.GatewayCall,
            model_id=meta.model_id, provider=meta.provider,
            finish_reason=telemetry.FinishReason(meta.finish_reason.value),
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            latency_ms=int(meta.latency_ms),
            cost_microusd=int(meta.estimated_cost_usd * 1_000_000),
            call_role=telemetry.CallRole(meta.call_role.value),
            model_calls=min(meta.model_calls, 100), repairs=meta.repairs,
            price_table_version=meta.price_table_version,
        )
        _ = run_id
        spend.gateway.append((meta.call_role.value, usage, turn.model_calls,
                              turn.estimated_cost_usd))

    def _charge_for(self, error: GatewayError, request: GenerateRequest) -> tuple[Usage, int] | None:
        """What a failed gateway call costs. Trustworthy usage from the gateway is kept (capped at
        the worst case); a rejection before processing costs no tokens but is a call; any other
        failed attempt whose usage is unknown (or reported as zero) is charged at the request's
        worst case with at least one call, even when the adapter counted none. Usage(0, 0) is
        never recorded for a call that was sent. A refusal before anything was sent (calls == 0
        and no usage, as the quota latch does) is not charged."""
        usage = error.usage
        if usage is not None and (usage.input_tokens or usage.output_tokens):
            worst = request.worst_case_usage
            sent = max(1, error.calls)
            return Usage(input_tokens=min(usage.input_tokens, worst.input_tokens * sent),
                         output_tokens=min(usage.output_tokens, worst.output_tokens * sent)), error.calls
        if usage is not None and error.rejected_before_processing:
            return usage, max(error.calls, 1)
        if error.calls > 0 or isinstance(error, _ATTEMPTED):
            worst = request.worst_case_usage
            sent = max(error.calls, 1)
            return Usage(input_tokens=worst.input_tokens * sent,
                         output_tokens=worst.output_tokens * sent), sent
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
            spend.gateway.append(("FRAME", usage, calls, None))

    def _discover(
        self, principal: Principal, message: str, frame: FrameResult, workspace: TripWorkspace,
        allowed: frozenset[ToolId], tool_run: ToolRun, cache_key: str, spend: _Spend,
        remaining: float,
    ) -> PipelineOutcome | TurnFailure:
        tool = frame.tool
        if tool is None or tool not in allowed or tool != ToolId.SEARCH_PLACES:
            self._reject(telemetry.ValidationReason.TOOL_NOT_ALLOWED, telemetry.Phase.TOOL)
            if tool is not None:
                self._deny(telemetry.ToolName(tool.value))
            return _rejected()
        if self._discovery is None or self._cache is None or frame.search is None:
            return _rejected(503)
        try:
            plan = plan_search(frame.search, message)
        except ArgumentRejected:
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
            return _rejected()
        outcome = self._search(principal, workspace, tool_run, plan.arguments, cache_key, spend,
                               remaining)
        if plan.area_dropped:  # forgiving: noted, never a failure
            self._reject(telemetry.ValidationReason.AREA_DROPPED, telemetry.Phase.TOOL)
            if isinstance(outcome, PipelineOutcome):
                return PipelineOutcome(outcome.workspace, outcome.reply, outcome.discovery,
                                       outcome.ref, (*outcome.warnings, TurnWarning.AREA_DROPPED))
        return outcome

    def _search(
        self, principal: Principal, workspace: TripWorkspace, run: ToolRun,
        arguments: dict[str, object], cache_key: str, spend: _Spend, remaining: float,
    ) -> PipelineOutcome | TurnFailure:
        if self._discovery is None:
            return _fault()
        failure: DiscoveryError | None = None
        result = None
        try:
            result = self._discovery.call_tool(
                run, ToolId.SEARCH_PLACES.value, arguments, timeout_seconds=remaining)
        except DiscoveryError as error:
            failure = error
        if failure is not None:
            return self._tool_failed(principal, workspace, run, failure, spend)
        if result is None:
            return _fault()
        spend.paid = True
        spend.tools.append((ToolId.SEARCH_PLACES.value, run.calls_used, result.billable_units))
        return self._validated(principal, workspace, run, result, cache_key)

    def _tool_failed(
        self, principal: Principal, workspace: TripWorkspace, run: ToolRun, failure: DiscoveryError,
        spend: _Spend,
    ) -> PipelineOutcome | TurnFailure:
        if failure.calls:
            spend.paid = True
            spend.tools.append((ToolId.SEARCH_PLACES.value, failure.calls, 0))
        if isinstance(failure, ToolBudgetExceeded):
            # Best validated partial result: the patch was validated, discovery was refused.
            return PipelineOutcome(workspace, REPLY_PARTIAL, None, None,
                                   (TurnWarning.TOOL_BUDGET_EXHAUSTED,))
        mapped = failure_from_discovery(failure)
        if isinstance(failure, ToolNotAllowed):
            self._reject(telemetry.ValidationReason.TOOL_NOT_ALLOWED, telemetry.Phase.TOOL)
        elif isinstance(failure, InvalidToolArguments):
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
        return mapped

    def _validated(
        self, principal: Principal, workspace: TripWorkspace, run: ToolRun, result: object,
        cache_key: str,
    ) -> PipelineOutcome | TurnFailure:
        """Parse, validate, render and cache the tool result. Returns a failure instead of
        raising it, so the Maps-carrying locals of this frame are gone before anyone raises."""
        cache = self._cache
        if cache is None:
            return _fault()
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
            cache.put(principal.uid, cache_key, validated)
            block = to_api_block(rendered, validated.answer.cache_expires_at,
                                 validated.answer.retrieved_at)
            ref = DiscoveryRef(cache_key, framing, validated.content_hash, validated.display_hash,
                               validated.answer.cache_expires_at)
        except GroundedParseError:
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
            failure = _rejected()
        except (GroundedAnswerRejected, VerbatimViolation):
            failure = _rejected()
        except CacheError:
            failure = _fault()
        except ValueError:  # includes pydantic ValidationError from the block or a renderer check
            self._reject(telemetry.ValidationReason.SCHEMA, telemetry.Phase.TOOL)
            failure = _rejected()
        if failure is not None:
            return failure
        if block is None or ref is None:
            return _fault()
        return PipelineOutcome(workspace, FRAMING_TEXT[framing], block, ref)
