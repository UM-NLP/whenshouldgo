"""Phase 0 grounded discovery (docs/data-and-history-design.md): contracts, the Grounding
Lite client, typed tool results, the verbatim render contract, the validator and the cache."""
from .cache import DisplayCache, DisplayCacheStore
from .client import (
    ACCESS_DENIED_TRIP_THRESHOLD,
    ALLOWED_HEADERS,
    API_KEY_HEADER,
    GROUNDING_LITE_ENDPOINT,
    GROUNDING_LITE_HOST,
    TOOL_SPECS,
    CallRecord,
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
    content_hash,
    display_hash,
    grounded_evidence,
    parse_grounded_answer,
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
    RouteResult,
    WeatherResult,
    parse_resolve_names,
    parse_resolve_urls,
    parse_route,
    parse_weather,
)
from .uris import MAPS_LINK_HOSTS, check_attribution_url, check_input_maps_url, check_maps_link
from .validator import GroundedAnswerValidator

__all__ = [name for name in dir() if not name.startswith("_")]
