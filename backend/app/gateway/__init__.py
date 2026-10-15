"""Provider-neutral model gateway and the OpenRouter adapter (docs/model-hosting-decision.md)."""
from .errors import (
    BudgetExceededError,
    CircuitOpenError,
    ContentFilterError,
    GatewayConfigError,
    GatewayError,
    GatewayErrorKind,
    MalformedOutputError,
    PrivacyViolationError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    TaintedContextError,
    ToolNotAllowedError,
    TransportFailure,
    TransportTimeout,
    TruncationError,
    UsageMismatchError,
)
from .openrouter import (
    OPENROUTER_CHAT_URL,
    Credential,
    HttpxTransport,
    OpenRouterGateway,
    QuotaLatch,
    Transport,
    TransportRequest,
    TransportResponse,
    assert_privacy_body,
    build_body,
    validate_body_shape,
    validate_url,
)
from .policy import (
    FRAME_STATE_SCHEMA_ALLOWLIST,
    MODEL_ID_PATTERN,
    STATE_BEARING_TYPES,
    GatewayPolicy,
    ResiliencePolicy,
    check_request,
    is_pinned_model_id,
    self_check,
)
from .pricing import ModelPrice, PriceTable, UnitPrice, placeholder_price_table
from .resilience import BreakerState, CircuitBreaker, ResilientGateway
from .types import (
    CallRole,
    ContextMessage,
    FinishReason,
    GatewayTelemetry,
    GenerateRequest,
    ModelGateway,
    ModelTurn,
    OutputSchema,
    PrivacyClass,
    RunBudget,
    ToolCall,
    ToolProvenance,
    ToolSpec,
    Usage,
)

__all__ = [name for name in dir() if not name.startswith("_")]
