"""Phase 0 domain contracts (docs/trip-assistant-architecture.md,
docs/agent-conversation-policy.md, docs/access-tiers.md,
docs/data-and-history-design.md).

Pydantic models reject unknown fields so that client input and model output can
never smuggle identity, tier or state through extra keys.

Every persisted document and API payload carries ``schema_version`` and ``kind``
(docs/data-and-history-design.md, "Versioning"). Changes are additive within a version.

Mutation rule: persisted contracts are immutable (``Frozen``). State changes build a
new, fully revalidated copy through ``evolve`` or a named server function; the code
base never uses ``model_construct`` or ``model_copy(update=...)`` (a test enforces it).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1
MAX_MAPS_CACHE = timedelta(days=30)
# Single source of truth for the size caps of a grounded answer. discovery/contracts.py and
# discovery/uris.py import these; the API block (DiscoveryBlock) derives its caps from them, so
# a maximal valid GroundedAnswer always converts.
GROUNDED_TEXT_MAX_CHARS = 8_000
MAX_PLACES = 20
MAX_TITLE_CHARS = 200
MAX_URI_CHARS = 2_048
MAX_MAPS_CACHE_HOURS = 24 * 30
# Owner decision (2026-10-15): a pending action lives at most 15 minutes.
MAX_PENDING_ACTION_TTL = timedelta(minutes=15)
# Owner decision (2026-10-15): history retention is hard-capped at 180 days. Users may
# shorten it but not extend it past 180 days until the owner decides otherwise.
MAX_OUTING_RETENTION_DAYS = 180
MAX_OUTING_RETENTION = timedelta(days=MAX_OUTING_RETENTION_DAYS)

_T = TypeVar("_T", bound=BaseModel)

# --- Bounded identifier and text types ----------------------------------------------
Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
PlaceId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,300}$")]
OpaqueToken = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
ShortText = Annotated[str, StringConstraints(max_length=100)]
VersionText = Annotated[str, StringConstraints(min_length=1, max_length=40)]

_COORDINATES = re.compile(r"-?\d{1,3}\.\d+\s*[,;/ ]\s*-?\d{1,3}\.\d+")
_STREET_ADDRESS = re.compile(
    r"\b\d{1,6}\s+[\w.' -]{1,40}\b(street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|"
    r"lane|ln|way|court|ct|crescent|cres|place|pl|highway|hwy)\b",
    re.IGNORECASE,
)
_POSTAL_CODE = re.compile(r"\b[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d\b")


def _reject_location_text(value: str | None) -> str | None:
    """Heuristic backstop only. Real provenance is enforced at the write path, which
    accepts a label solely from text the user typed, never from Maps content."""
    if value is not None and (
        _COORDINATES.search(value) or _STREET_ADDRESS.search(value) or _POSTAL_CODE.search(value)
    ):
        raise ValueError("label looks like a coordinate or an address")
    return value


UserLabel = Annotated[str, StringConstraints(max_length=80), AfterValidator(_reject_location_text)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Frozen(BaseModel):
    """Immutable contract: no unknown fields and no mutation after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def evolve(model: _T, **changes: object) -> _T:
    """Return a new, fully revalidated copy of ``model`` with ``changes`` applied."""
    fields = type(model).model_fields
    unknown = set(changes) - set(fields)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    return type(model)(**{**{name: getattr(model, name) for name in fields}, **changes})


class PrincipalKind(StrEnum):
    GUEST = "GUEST"
    REGISTERED_FREE = "REGISTERED_FREE"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class Principal:
    """Server-derived identity. Built only from a verified token, never from a body."""

    uid: str
    kind: PrincipalKind
    policy_version: str


class DomainClass(StrEnum):
    """Travel-domain gate result (docs/voice-and-domain-controls.md)."""

    TRAVEL = "TRAVEL"
    APP_HELP = "APP_HELP"
    MIXED = "MIXED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    SAFETY = "SAFETY"


class WorkspaceStatus(StrEnum):
    CAPTURED = "CAPTURED"
    FRAMING = "FRAMING"
    EXPLORING = "EXPLORING"
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    ADAPTING = "ADAPTING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNSUPPORTED = "UNSUPPORTED"


class NextAction(StrEnum):
    ASK = "ASK"
    SEARCH = "SEARCH"
    LOOK_UP_DETAILS = "LOOK_UP_DETAILS"
    ESTIMATE_TRAVEL = "ESTIMATE_TRAVEL"
    VALIDATE = "VALIDATE"
    SOLVE = "SOLVE"
    PRESENT_SHORTLIST = "PRESENT_SHORTLIST"
    CONFIRM = "CONFIRM"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    EXPLAIN_CONFLICT = "EXPLAIN_CONFLICT"
    STOP_UNSUPPORTED = "STOP_UNSUPPORTED"


class FactSource(StrEnum):
    USER_EXPLICIT = "USER_EXPLICIT"
    USER_PROSE = "USER_PROSE"
    INFERENCE = "INFERENCE"
    TOOL = "TOOL"
    MEMORY = "MEMORY"


class Confirmation(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"


class Fact(Frozen):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["fact"] = "fact"
    id: Ident
    key: Ident
    value: Annotated[str, StringConstraints(max_length=500)] | float | int | bool | None = None
    source: FactSource
    confirmation: Confirmation
    hard: bool
    scope: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    evidence_ids: tuple[Ident, ...] = Field(default=(), max_length=50)
    expires_at: AwareDatetime | None = None
    depends_on: tuple[Ident, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def _inference_is_never_a_confirmed_hard_fact(self) -> Fact:
        if (
            self.source == FactSource.INFERENCE
            and self.hard
            and self.confirmation == Confirmation.CONFIRMED
        ):
            raise ValueError("an inference cannot be a confirmed hard constraint")
        return self


class EvidenceKind(StrEnum):
    GROUNDED_ANSWER = "GROUNDED_ANSWER"
    PLACES_DIRECT = "PLACES_DIRECT"
    ROUTE_DIRECT = "ROUTE_DIRECT"
    USER = "USER"
    WEATHER = "WEATHER"


class ContentClass(StrEnum):
    """Storage class of the content a piece of evidence points at."""

    MAPS_GROUNDED = "MAPS_GROUNDED"
    MAPS_PLACES = "MAPS_PLACES"
    MAPS_ROUTE = "MAPS_ROUTE"
    USER = "USER"
    NONE = "NONE"


class EvidenceStorageClass(StrEnum):
    EPHEMERAL = "EPHEMERAL"
    DISPLAY_CACHE = "DISPLAY_CACHE"
    PLACE_ID_ONLY = "PLACE_ID_ONLY"


_S = EvidenceStorageClass
_C = ContentClass
# Bidirectional: each kind allows only these classes, and each Maps class only its kind.
# Owner decision (2026-10-15): weather obtained through Grounding Lite is Maps content.
# It stays MAPS_GROUNDED (attribution, display-cache-only storage, 30-day cap) until
# Google answers the question in writing. NONE is reserved for weather from a non-Google
# source. MAPS_GROUNDED is allowed only for GROUNDED_ANSWER and WEATHER.
_KIND_CLASSES = {
    EvidenceKind.GROUNDED_ANSWER: {_C.MAPS_GROUNDED},
    EvidenceKind.PLACES_DIRECT: {_C.MAPS_PLACES},
    EvidenceKind.ROUTE_DIRECT: {_C.MAPS_ROUTE},
    EvidenceKind.USER: {_C.USER, _C.NONE},
    EvidenceKind.WEATHER: {_C.MAPS_GROUNDED, _C.NONE},
}
# Explicit allowed storage per content class. Only grounded answers may be display-cached
# (24 h default, 30 days max); Places payloads are re-fetched by place ID and Routes
# payloads stay in memory.
_CLASS_STORAGE = {
    _C.MAPS_GROUNDED: {_S.EPHEMERAL, _S.DISPLAY_CACHE, _S.PLACE_ID_ONLY},
    _C.MAPS_PLACES: {_S.EPHEMERAL, _S.PLACE_ID_ONLY},
    _C.MAPS_ROUTE: {_S.EPHEMERAL},
    _C.USER: {_S.EPHEMERAL},
    _C.NONE: {_S.EPHEMERAL},
}
_NEVER_LLM_VISIBLE = {_C.MAPS_PLACES, _C.MAPS_ROUTE}  # until decision 17 changes


class Evidence(Frozen):
    """Provenance and handling metadata for a tool result. Holds no content, only a hash."""

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    id: Ident
    tool: Ident
    tool_version: VersionText
    source: ShortText
    retrieved_at: AwareDatetime
    expires_at: AwareDatetime  # freshness
    precision: Annotated[str, StringConstraints(max_length=40)]
    # No defaults for the handling fields: a producer must state them explicitly.
    kind: EvidenceKind
    content_class: ContentClass
    llm_visible: bool
    storage_class: EvidenceStorageClass
    attribution_required: bool
    cache_expires_at: AwareDatetime | None = None  # storage; separate from freshness
    place_id: PlaceId | None = None
    tool_call_id: Ident | None = None  # provenance within the run
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _handling_rules(self) -> Evidence:
        if self.content_class not in _KIND_CLASSES[self.kind]:
            raise ValueError(f"{self.kind} evidence cannot use content_class {self.content_class}")
        if self.storage_class not in _CLASS_STORAGE[self.content_class]:
            raise ValueError(f"{self.content_class} content cannot use {self.storage_class}")
        if self.llm_visible and (
            self.content_class in _NEVER_LLM_VISIBLE
            or self.kind in (EvidenceKind.PLACES_DIRECT, EvidenceKind.ROUTE_DIRECT)
        ):
            raise ValueError("direct Places/Routes evidence must not be LLM-visible")
        if self.content_class.value.startswith("MAPS_") and not self.attribution_required:
            raise ValueError("Maps content requires attribution")
        if (
            self.kind == EvidenceKind.WEATHER
            and self.content_class == _C.NONE
            and self.attribution_required
        ):
            raise ValueError("non-Google weather cannot be tagged as attribution-required Maps content")
        if self.expires_at <= self.retrieved_at:
            raise ValueError("expires_at must follow retrieved_at")
        if self.storage_class == _S.DISPLAY_CACHE:
            if self.cache_expires_at is None:
                raise ValueError("DISPLAY_CACHE evidence requires cache_expires_at")
            if self.cache_expires_at < self.retrieved_at:
                raise ValueError("cache_expires_at precedes retrieved_at")
            if self.cache_expires_at > self.retrieved_at + MAX_MAPS_CACHE:
                raise ValueError("cache_expires_at exceeds retrieved_at + 30 days")
        elif self.cache_expires_at is not None:
            raise ValueError("cache_expires_at is only valid for DISPLAY_CACHE evidence")
        if self.storage_class == _S.PLACE_ID_ONLY:
            if not self.place_id:
                raise ValueError("PLACE_ID_ONLY evidence requires place_id")
            if self.content_hash is not None:
                raise ValueError("PLACE_ID_ONLY evidence must not carry a content_hash")
        if self.kind != EvidenceKind.USER and not self.tool_call_id:
            raise ValueError("tool evidence requires tool_call_id")
        return self

    def is_fresh(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return self.expires_at > now


class WorkspacePatch(Strict):
    """Untrusted model proposal; must pass validation before it touches state."""

    workspace_id: str
    base_version: int
    next_action: NextAction
    upsert_facts: list[Fact] = Field(default_factory=list)
    remove_fact_ids: list[str] = Field(default_factory=list)
    status: WorkspaceStatus | None = None


class PendingActionEffect(StrEnum):
    # CREATE_ROUTINE is added in Phase 2 (decision 20) and ENABLE_NOTIFICATIONS in
    # Phase 3 (decision 21), together with their handlers. Not before.
    SET_REMINDER = "SET_REMINDER"
    LAUNCH_HANDOFF = "LAUNCH_HANDOFF"


class PendingAction(Frozen):
    """Two-step side effect: shown as ``preview``, committed only on explicit confirmation.

    Confirmation produces a new copy via ``confirm_pending_action``; the field is never
    assigned. The lifetime ceiling is MAX_PENDING_ACTION_TTL (owner decision, 15 minutes).
    """

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["pending_action"] = "pending_action"
    id: Ident
    workspace_id: Ident
    plan_version: int = Field(ge=1)
    effect: PendingActionEffect
    target: Ident  # opaque reference or place_id; never an address or coordinates
    preview: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    nonce: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,128}$")]
    idempotency_key: Annotated[str, StringConstraints(min_length=8, max_length=200)]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    confirmed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _lifetime(self) -> PendingAction:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at")
        if self.expires_at - self.created_at > MAX_PENDING_ACTION_TTL:
            raise ValueError("pending action lifetime exceeds MAX_PENDING_ACTION_TTL")
        if self.confirmed_at is not None and not (
            self.created_at <= self.confirmed_at <= self.expires_at
        ):
            raise ValueError("confirmed_at must fall between created_at and expires_at")
        return self


def confirm_pending_action(action: PendingAction, now: datetime) -> PendingAction:
    """Server-side confirmation: returns a new copy; refuses expired or repeat confirmation."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if action.confirmed_at is not None:
        raise ValueError("pending action is already confirmed")
    if not action.created_at <= now <= action.expires_at:
        raise ValueError("pending action has expired or is not yet valid")
    return evolve(action, confirmed_at=now)


class TripWorkspace(Strict):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["trip_workspace"] = "trip_workspace"
    id: Ident
    owner_id: Ident  # set from the verified token only
    status: WorkspaceStatus = WorkspaceStatus.CAPTURED
    version: int = 0
    timezone: str = "America/Toronto"
    goal: Annotated[str, StringConstraints(max_length=20_000)] | None = None
    facts: list[Fact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    active_plan_version: int | None = None

    @property
    def assumptions(self) -> list[Fact]:
        """Inferred, unconfirmed facts the user can edit."""
        return [
            fact
            for fact in self.facts
            if fact.source == FactSource.INFERENCE
            and fact.confirmation == Confirmation.UNCONFIRMED
        ]


# --- Plan versions and history (docs/data-and-history-design.md) -------------------
# These contracts hold IDs, enums, timestamps and text the user typed. They have no
# field that can carry a Maps name, address, category, hours, rating or coordinates.


class PlaceRef(Frozen):
    place_id: PlaceId
    id_checked_at: AwareDatetime | None = None


class PlanDestination(Frozen):
    place_id: PlaceId
    user_label: UserLabel | None = None  # only what the user typed


class ReplacedReason(StrEnum):
    USER_EDIT = "USER_EDIT"
    CONSTRAINT_CHANGED = "CONSTRAINT_CHANGED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    ADAPTATION = "ADAPTATION"


class PlanWarning(StrEnum):
    """Warnings are codes, never free text, so provider content cannot ride in them."""

    ASSUMPTIONS_UNCONFIRMED = "ASSUMPTIONS_UNCONFIRMED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    ROUTE_UNVERIFIED = "ROUTE_UNVERIFIED"
    DETAILS_UNAVAILABLE = "DETAILS_UNAVAILABLE"
    HARD_NEED_UNVERIFIED = "HARD_NEED_UNVERIFIED"


class PlanVersion(Frozen):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["plan_version"] = "plan_version"
    id: Ident
    workspace_id: Ident
    version: int = Field(ge=1)
    parent_version: int | None = Field(default=None, ge=1)
    replaced_reason: ReplacedReason | None = None
    destination: PlanDestination
    origin_ref: OpaqueToken  # opaque token, never coordinates or an address
    user_constraint_fact_ids: tuple[Ident, ...] = Field(default=(), max_length=50)
    grounded_evidence_id: Ident | None = None  # display-cache pointer that may expire
    route_evidence_id: Ident | None = None  # ephemeral
    assumption_fact_ids: tuple[Ident, ...] = Field(default=(), max_length=50)
    warnings: tuple[PlanWarning, ...] = Field(default=(), max_length=20)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _lineage(self) -> PlanVersion:
        if self.parent_version is not None and self.parent_version >= self.version:
            raise ValueError("parent_version must precede version")
        if self.replaced_reason is not None and self.parent_version is None:
            raise ValueError("replaced_reason requires parent_version")
        return self


class IntentTag(StrEnum):
    """Classified from the user's own request, never from Maps content."""

    FOOD_DRINK = "FOOD_DRINK"
    OUTDOOR = "OUTDOOR"
    KIDS = "KIDS"
    ERRAND = "ERRAND"
    OTHER = "OTHER"


class Daypart(StrEnum):
    MORNING = "MORNING"
    AFTERNOON = "AFTERNOON"
    EVENING = "EVENING"
    NIGHT = "NIGHT"


class OutingEventType(StrEnum):
    SELECTED = "SELECTED"
    HANDOFF_LAUNCHED = "HANDOFF_LAUNCHED"
    REMINDER_SET = "REMINDER_SET"
    VISITED_CONFIRMED = "VISITED_CONFIRMED"
    SKIPPED = "SKIPPED"


class OutingEvent(Frozen):
    type: OutingEventType
    at: AwareDatetime


class Thumb(StrEnum):
    UP = "UP"
    DOWN = "DOWN"


class FeedbackReason(StrEnum):
    NOT_RELEVANT = "NOT_RELEVANT"
    TOO_FAR = "TOO_FAR"
    CLOSED = "CLOSED"
    WRONG_VIBE = "WRONG_VIBE"
    OTHER = "OTHER"


class OutingFeedback(Frozen):
    thumb: Thumb | None = None
    reason_enum: FeedbackReason | None = None
    note: Annotated[str, StringConstraints(max_length=500)] | None = None  # excluded from logs


class OutingStrength(StrEnum):
    """A handoff is not proof of a visit."""

    HANDOFF = "HANDOFF"
    SELF_REPORTED_VISIT = "SELF_REPORTED_VISIT"


class OutingRecord(Frozen):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["outing_record"] = "outing_record"
    id: Ident
    owner_id: Ident
    workspace_id: Ident
    plan_version: int = Field(ge=1)
    occurred_at: AwareDatetime  # UTC
    tz: Annotated[str, StringConstraints(max_length=64)]
    local_dow: int = Field(ge=0, le=6)  # Monday=0, derived at write
    local_daypart: Daypart
    place_ref: PlaceRef
    user_label: UserLabel | None = None  # never a Maps name
    intent_tag: IntentTag
    events: tuple[OutingEvent, ...] = Field(default=(), max_length=50)
    feedback: OutingFeedback = OutingFeedback()
    strength: OutingStrength
    consent_id: Ident
    retention_expires_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> OutingRecord:
        if self.occurred_at.utcoffset() != timedelta(0):
            raise ValueError("occurred_at must be UTC")
        try:
            zone = ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError, OSError) as error:
            raise ValueError("tz must be an IANA time zone") from error
        if self.occurred_at.astimezone(zone).weekday() != self.local_dow:
            raise ValueError("local_dow does not match occurred_at in tz")
        if self.retention_expires_at <= self.occurred_at:
            raise ValueError("retention_expires_at must follow occurred_at")
        if self.retention_expires_at > self.occurred_at + MAX_OUTING_RETENTION:
            raise ValueError("retention_expires_at exceeds MAX_OUTING_RETENTION")
        visited = any(event.type == OutingEventType.VISITED_CONFIRMED for event in self.events)
        if visited != (self.strength == OutingStrength.SELF_REPORTED_VISIT):
            raise ValueError("a visit exists only when the user confirmed it")
        return self


class ConsentPurpose(StrEnum):
    LOCATION_FOREGROUND = "LOCATION_FOREGROUND"
    HISTORY_STORE = "HISTORY_STORE"
    PATTERN_INFERENCE = "PATTERN_INFERENCE"
    PROACTIVE_NOTIFY = "PROACTIVE_NOTIFY"
    CLOUD_SYNC = "CLOUD_SYNC"
    CALENDAR_READ = "CALENDAR_READ"  # later phase


class ConsentProcessor(StrEnum):
    """Processors the notice must name (docs/data-and-history-design.md)."""

    GOOGLE = "GOOGLE"
    OPENROUTER = "OPENROUTER"
    UPSTREAM_MODEL_HOST = "UPSTREAM_MODEL_HOST"


class ConsentChannel(StrEnum):
    WEBPUSH = "WEBPUSH"
    FCM_ANDROID = "FCM_ANDROID"  # later
    IN_APP = "IN_APP"


class ConsentScope(Frozen):
    retention_days: int | None = Field(default=None, ge=1)
    channels: tuple[ConsentChannel, ...] = Field(default=(), max_length=5)


class ConsentRecord(Frozen):
    """Append-only ledger entry; storage and the withdrawal cascade are out of scope here."""

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["consent_record"] = "consent_record"
    id: Ident
    owner_id: Ident
    purpose: ConsentPurpose
    notice_version: VersionText
    processors: tuple[ConsentProcessor, ...] = Field(min_length=1, max_length=5)
    scope: ConsentScope
    granted_at: AwareDatetime
    withdrawn_at: AwareDatetime | None = None
    context: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_.-]{1,64}$")]

    @model_validator(mode="after")
    def _withdrawal_follows_grant(self) -> ConsentRecord:
        if self.withdrawn_at is not None and self.withdrawn_at < self.granted_at:
            raise ValueError("withdrawn_at precedes granted_at")
        return self


# --- Runtime configuration (docs/model-hosting-decision.md) ------------------------


class TokenCeilings(Frozen):
    run_input_tokens: int = Field(default=40_000, ge=1)
    run_output_tokens: int = Field(default=4_000, ge=1)
    run_model_calls: int = Field(default=6, ge=1)
    run_tool_calls: int = Field(default=12, ge=1)
    run_escalations: int = Field(default=1, ge=0)
    interactive_budget_seconds: int = Field(default=20, ge=1)
    # Per planning call (decision 10): 12,000 compiled input and 1,200 output tokens.
    planner_input_tokens: int = Field(default=12_000, ge=1)
    planner_output_tokens: int = Field(default=1_200, ge=1)


class RuntimeTTLs(Frozen):
    # Never above the Evidence cache cap, so a config cannot out-live the legal maximum.
    grounded_cache_hours: int = Field(default=24, ge=1, le=MAX_MAPS_CACHE_HOURS)
    guest_workspace_inactive_days: int = Field(default=7, ge=1)
    outing_retention_days: int = Field(default=180, ge=1, le=MAX_OUTING_RETENTION_DAYS)
    pending_action_ttl_minutes: int | None = Field(
        default=None, ge=1, le=int(MAX_PENDING_ACTION_TTL.total_seconds() // 60)
    )  # unspecified in docs


class PhaseFlags(Frozen):
    phase0_discovery: bool = True
    phase1_history: bool = False
    phase2_patterns: bool = False
    phase3_notifications: bool = False


class PromptHash(Frozen):
    name: Ident
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ToolSchemaVersion(Frozen):
    name: Ident
    version: VersionText


class RuntimeConfig(Frozen):
    """Versioned bundle recorded on every turn; change the version to audit or roll back.

    Sub-objects are required (no shared default instances); collections are tuples so the
    bundle is deeply immutable and hashable.
    """

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["runtime_config"] = "runtime_config"
    version: VersionText
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    provider: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    # Privacy settings are part of the audited bundle (docs/model-hosting-decision.md). The
    # Literal types make a weakened value unrepresentable; the gateway re-checks anyway.
    provider_allowlist: Annotated[
        tuple[Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")], ...],
        Field(min_length=1, max_length=10),
    ]
    zdr: Literal[True] = True
    data_collection: Literal["deny"] = "deny"
    allow_fallbacks: Literal[False] = False
    price_table_version: VersionText
    prompt_version_hashes: tuple[PromptHash, ...]
    tool_schema_versions: tuple[ToolSchemaVersion, ...]
    classifier_version: VersionText
    policy_version: VersionText
    entitlement_version: VersionText
    token_ceilings: TokenCeilings
    ttls: RuntimeTTLs
    flags: PhaseFlags
    # OWNER-TUNABLE PLACEHOLDER (not a product decision): the most PAID FAILED turns (a turn
    # that fails after at least one provider call was sent) one principal may cause per rolling
    # hour. Failures refund the user allowance (documented decision), so this limiter and the
    # daily token ceiling in the entitlement policy are what bound abuse of paid calls. Enforced
    # before the model is called; over the limit is a typed 429 QUOTA / OVER_BUDGET.
    max_paid_failed_turns_per_hour: int = Field(default=10, ge=1, le=1_000)
    # OWNER-TUNABLE PLACEHOLDER: the most FREE turns one principal may cause per rolling hour. A
    # turn that ends without a result for the user (intent OTHER, or a discovery proposal degraded
    # to the fixed clarification) does not consume the allowance, but it costs a model call, so
    # this caps it. Over the limit is a typed 429 RATE_LIMITED with Retry-After, before FRAME. It
    # is separate from the paid-failed-turn limit (free turns are completed 200s, not failures).
    max_free_turns_per_hour: int = Field(default=30, ge=1, le=1_000)
    # Decision 17 and legal open question 2 (docs/legal/open-questions.md): in Phase 0 the
    # model never sees Google Maps content. Prompt caching at OpenRouter's upstream hosts is
    # therefore not a concern, and the server shows Google's grounded text verbatim. True
    # would let DISCOVER calls carry MAPS_GROUNDED context (gateway support exists) but that
    # path is deliberately NOT implemented: the orchestrator raises a typed not-implemented
    # error when this is True. Flip it only with Google's written answer to question 2.
    maps_content_to_model: bool = False

    @model_validator(mode="after")
    def _unique_names(self) -> RuntimeConfig:
        if self.provider not in self.provider_allowlist:
            raise ValueError("provider must be on provider_allowlist")
        if len(set(self.provider_allowlist)) != len(self.provider_allowlist):
            raise ValueError("duplicate provider in provider_allowlist")
        for items in (self.prompt_version_hashes, self.tool_schema_versions):
            names = [item.name for item in items]
            if len(names) != len(set(names)):
                raise ValueError("duplicate name in runtime config")
        return self


# --- API payloads -------------------------------------------------------------------


class ErrorCode(StrEnum):
    QUOTA = "QUOTA"  # an over-budget run (429); allowance exhaustion is still a 200 with a flag
    POLICY = "POLICY"
    PROVIDER = "PROVIDER"  # also used for unhandled server faults until a code exists
    VALIDATION = "VALIDATION"
    CONFLICT = "CONFLICT"


class ErrorReason(StrEnum):
    """Stable machine-readable refinement of an ``ErrorCode``. Clients switch on this, never on
    the message. Additive: a client must treat an unknown reason like its ``ErrorCode``."""

    RESULT_EXPIRED = "RESULT_EXPIRED"  # replay of a discovery turn whose cached block is gone
    TURN_IN_PROGRESS = "TURN_IN_PROGRESS"  # another turn for this tenant is still running
    SERVER_NOT_CONFIGURED = "SERVER_NOT_CONFIGURED"  # no model gateway and no dev scaffold
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    OUTPUT_TRUNCATED = "OUTPUT_TRUNCATED"
    # ONE public reason for every validation-stage refusal (a rejected patch, bad or disallowed
    # tool arguments, an invalid tool result, unusable model output, discovery not configured).
    # The fine distinction stays in telemetry (ValidationRejection / ToolCall events).
    OUTPUT_REJECTED = "OUTPUT_REJECTED"
    OVER_BUDGET = "OVER_BUDGET"  # one run exceeded its own processing budget
    RATE_LIMITED = "RATE_LIMITED"  # a daily ceiling or the paid-failure limiter (Retry-After)
    DISCOVERY_UNAVAILABLE = "DISCOVERY_UNAVAILABLE"  # includes a tripped kill switch


class ErrorBody(Frozen):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["error"] = "error"
    code: ErrorCode
    message: str = Field(max_length=500)  # server-authored, never provider or user content
    reason: ErrorReason | None = None  # omitted from the JSON when absent


class FramingKind(StrEnum):
    """Fixed server copy shown above a grounded block, selected by enum: free text can never be
    framing. Lives here (not in discovery.render, which re-exports it) so the API contract can
    name it."""

    RESULTS_INTRO = "RESULTS_INTRO"
    NO_RESULTS = "NO_RESULTS"


class _ApiFrozen(Frozen):
    """API contract that may hold Maps text: validation errors never echo input."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class DiscoveryPlace(_ApiFrozen):
    """One source, exactly as Google provided it (shown as text, linked to its urls)."""

    title: str = Field(max_length=MAX_TITLE_CHARS, repr=False)
    attribution_url: str = Field(max_length=MAX_URI_CHARS, repr=False)
    place_url: str = Field(max_length=MAX_URI_CHARS, repr=False)

    @field_validator("attribution_url", "place_url")
    @classmethod
    def _google_link(cls, value: str, info: ValidationInfo) -> str:
        """https and the pinned Google host and path shape of the field (discovery/uris rules).
        Imported lazily: the discovery package imports this module."""
        from .discovery.uris import LinkField, check_link

        field = (
            LinkField.PLACE_ATTRIBUTION_URL if info.field_name == "attribution_url"
            else LinkField.PLACE_URL
        )
        check_link(field, value)
        return value


class DiscoveryBlock(_ApiFrozen):
    """Google's grounded answer, verbatim: the raw summary text, then every place's attribution
    in order. ``render_mode`` "text" means a client inserts each field with textContent, never as
    HTML or Markdown. The server framing sits OUTSIDE this block (``TurnResponse.reply``)."""

    render_mode: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=GROUNDED_TEXT_MAX_CHARS, repr=False)  # raw
    places: tuple[DiscoveryPlace, ...] = Field(min_length=1, max_length=MAX_PLACES)
    branding: Literal["Google Maps"] = "Google Maps"
    translate_no: Literal[True] = True  # the name "Google Maps" must not be translated
    framing: FramingKind
    retrieved_at: AwareDatetime  # when Google answered: a replay shows its age
    cache_expires_at: AwareDatetime


class TurnWarning(StrEnum):
    """Typed, content-free warnings on a partial result."""

    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
    FACT_DROPPED = "FACT_DROPPED"  # one proposed fact had unacceptable text and was left out
    AREA_DROPPED = "AREA_DROPPED"  # the model's search area was unusable and was left out
    QUERY_REJECTED = "QUERY_REJECTED"  # no usable search query: a clarification, no lookup


class TurnResponse(Frozen):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    kind: Literal["turn_response"] = "turn_response"
    reply: str = Field(max_length=5_000)
    domain: DomainClass | None
    allowed_tools: tuple[str, ...]
    workspace_id: Ident | None
    allowance_exhausted: bool
    policy_version: str = Field(max_length=100)
    # Additive (schema_version stays 1): present only for a turn that ran discovery.
    discovery: DiscoveryBlock | None = None
    warnings: tuple[TurnWarning, ...] = Field(default=(), max_length=10)
