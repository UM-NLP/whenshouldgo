"""Provider failure ladder: bounded retry, then a circuit breaker. No fallback.

The ladder never changes the model, the provider or the privacy settings: a request that
cannot be served by the pinned, reviewed provider fails (docs/model-hosting-decision.md).
Thresholds come from ``ResiliencePolicy``; time and sleep are injected for tests.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from threading import Lock

from ..domain import evolve
from .errors import CircuitOpenError, GatewayError, PrivacyViolationError
from .policy import ResiliencePolicy
from .pricing import PriceTable
from .types import ZERO_USAGE, GenerateRequest, ModelGateway, ModelTurn, Usage


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Thread-safe. Opens after N consecutive retryable failures; after the open window
    exactly one probe is admitted (half-open). A success closes it; a failed probe
    reopens it; a probe that ends without a verdict is released, never wedged."""

    def __init__(self, policy: ResiliencePolicy, clock: Callable[[], float] = time.monotonic):
        self._policy = policy
        self._clock = clock
        self._lock = Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state()

    def _state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self._policy.breaker_open_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def before_call(self) -> bool:
        """Admit a call. Returns True when this call is the half-open probe."""
        with self._lock:
            state = self._state()
            if state == BreakerState.OPEN or (state == BreakerState.HALF_OPEN and self._probing):
                raise CircuitOpenError()
            if state == BreakerState.HALF_OPEN:
                self._probing = True
                return True
            return False

    def record_success(self, probe: bool = False) -> None:
        with self._lock:
            if probe:
                self._probing = False
                self._failures = 0
                self._opened_at = None
            elif self._opened_at is None:
                self._failures = 0

    def record_failure(self, probe: bool = False) -> None:
        with self._lock:
            if probe:
                self._probing = False
                self._opened_at = self._clock()
                return
            if self._opened_at is None:
                self._failures += 1
                if self._failures >= self._policy.breaker_failure_threshold:
                    self._opened_at = self._clock()

    def release(self, probe: bool = False) -> None:
        """Give back an admitted call that produced no provider verdict."""
        if probe:
            with self._lock:
                self._probing = False


class ResilientGateway:
    """Wraps a gateway with retry and a per-provider circuit breaker."""

    def __init__(
        self,
        inner: ModelGateway,
        policy: ResiliencePolicy,
        *,
        price_table: PriceTable,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._prices = price_table
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._breakers: dict[str, CircuitBreaker] = {}
        self._guard = Lock()

    @property
    def latched(self) -> bool:
        return bool(getattr(self._inner, "latched", False))

    def breaker_for(self, provider: str) -> CircuitBreaker:
        with self._guard:
            if provider not in self._breakers:
                self._breakers[provider] = CircuitBreaker(self._policy, self._clock)
            return self._breakers[provider]

    def generate(self, request: GenerateRequest) -> ModelTurn:
        breaker = self.breaker_for(request.runtime_config.provider)
        failed_calls = 0
        charged = ZERO_USAGE  # usage of failed attempts, carried onto the returned turn
        current = request
        for attempt in range(1, self._policy.max_attempts + 1):
            probe = breaker.before_call()
            settled = False
            started = self._clock()
            try:
                try:
                    turn = self._inner.generate(current)
                except GatewayError as error:
                    if error.retryable:
                        failure = error  # `error` is unbound when the block ends
                        breaker.record_failure(probe)
                        settled = True
                        calls = max(1, error.calls)
                    elif error.calls > 0 and not isinstance(error, PrivacyViolationError):
                        # The pinned provider answered; a non-retryable fault is not an
                        # outage. An off-allowlist answer is a privacy incident instead:
                        # it proves nothing about the pinned host, so it stays neutral.
                        breaker.record_success(probe)
                        settled = True
                        raise
                    else:
                        raise  # refused locally before sending: says nothing about the provider
                else:
                    breaker.record_success(probe)
                    settled = True
                    total = charged.plus(turn.usage)
                    config = current.runtime_config
                    return evolve(
                        turn,
                        usage=total,
                        estimated_cost_usd=self._prices.estimate(
                            config.model_id, config.provider, total
                        ),
                        model_calls=turn.model_calls + failed_calls,
                    )
            finally:
                if not settled:
                    breaker.release(probe)  # any other exit (TypeError, RecursionError ...)
            # Retryable failure: decide whether a retry fits the run budget.
            failed_calls += calls
            # A call went out: charge what came back, else assume the worst case.
            spent: Usage
            if failure.usage is not None:
                spent = failure.usage
            elif failure.rejected_before_processing:
                spent = ZERO_USAGE  # rejected before processing: free, but the call counted
            else:
                spent = current.worst_case_usage
            charged = charged.plus(spent)
            if attempt == self._policy.max_attempts:
                raise failure
            delay = min(
                self._policy.backoff_cap_seconds,
                self._policy.backoff_base_seconds * 2 ** (attempt - 1),
            )
            budget = current.budget.consume(
                calls=calls, usage=spent, seconds=self._clock() - started + delay
            )
            if budget.calls_remaining < 1 or budget.seconds_remaining <= 0:
                raise failure  # a retry would break the run budget; never extend it
            self._sleep(delay)
            current = evolve(current, budget=budget)
        raise AssertionError("unreachable")  # pragma: no cover
