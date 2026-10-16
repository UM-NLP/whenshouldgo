"""Phase 0 grounded discovery (docs/data-and-history-design.md): contracts, the Grounding
Lite client, typed tool results, the verbatim render contract, the validator and the cache."""
from ._validated import ValidatedAnswer, is_validated
from .cache import DisplayCache, DisplayCacheStore
from .client import (
    ACCESS_DENIED_TRIP_THRESHOLD,
    ALLOWED_HEADERS,
    API_KEY_HEADER,
    GROUNDING_LITE_ENDPOINT,
    GROUNDING_LITE_HOST,
    TOOL_SPECS,
    CallRecord,
    CallTicket,
    GroundingLiteClient,
    GroundingLiteMcpClient,
    HttpxMcpTransport,
    KillSwitch,
    KillSwitchProbe,
    LedgerInfo,
    McpRequest,
    McpResponse,
    McpTransport,
    ToolResult,
    ToolRun,
    parse_single_sse_event,
    payload_ledger_info,
    validate_endpoint,
)
from .contracts import (
    GroundedAnswer,
    GroundedParseError,
    GroundedPlace,
    citation_indexes,
    cited_flags,
    content_hash,
    display_hash,
    grounded_evidence,
    parse_grounded_answer,
    payload_digest,
)
from .errors import (
    CacheError,
    DiscoveryAccessDenied,
    DiscoveryBadRequest,
    DiscoveryConfigError,
    DiscoveryDisabledError,
    DiscoveryError,
    DiscoveryProtocolError,
    DiscoveryRateLimited,
    DiscoverySessionRequired,
    DiscoveryTimeout,
    DiscoveryToolError,
    DiscoveryUnavailable,
    GroundedAnswerRejected,
    InvalidToolArguments,
    MalformedToolResult,
    OversizeResult,
    RejectionReason,
    ToolBudgetExceeded,
    ToolNotAllowed,
)
from .ids import ToolId
from .render import (
    FRAMING_TEXT,
    FramingKind,
    GroundedTextPart,
    RenderOption,
    RenderedBlock,
    ServerTextPart,
    SourcePart,
    VerbatimViolation,
    html_sink_escape,
    render_grounded_block,
    verify_verbatim,
)
from .results import (
    ItemFailure,
    ItemFailureCode,
    ResolvedPlace,
    ResolveResult,
    WALKING_WARNING_TEXT,
    RouteResult,
    WeatherResult,
    parse_resolve_names,
    parse_resolve_urls,
    parse_route,
    parse_weather,
)
from .uris import LINK_HOSTS, LINK_RULES, LinkField, check_input_maps_url, check_link, data_token
from .validator import GroundedAnswerValidator

__all__ = [name for name in dir() if not name.startswith("_")]
