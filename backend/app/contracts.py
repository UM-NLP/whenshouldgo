"""Frozen Phase 0 contracts from docs/trip-assistant-architecture.md and agent-conversation-policy.md."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrincipalKind(StrEnum):
    GUEST = "GUEST"
    REGISTERED_FREE = "REGISTERED_FREE"
    DISABLED = "DISABLED"


class DomainClass(StrEnum):
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


class Fact(Strict):
    id: str
    key: str
    value: str | float | int | bool | None = None
    source: FactSource
    confirmation: Confirmation
    hard: bool
    scope: str
    evidence_ids: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    depends_on: list[str] = Field(default_factory=list)


class Evidence(Strict):
    id: str
    tool: str
    tool_version: str
    retrieved_at: datetime
    expires_at: datetime
    precision: str


class WorkspacePatch(Strict):
    """Untrusted model proposal; must pass validation before it touches state."""

    workspace_id: str
    base_version: int
    next_action: NextAction
    upsert_facts: list[Fact] = Field(default_factory=list)
    remove_fact_ids: list[str] = Field(default_factory=list)
    status: WorkspaceStatus | None = None


class PendingAction(Strict):
    id: str
    workspace_id: str
    plan_version: int
    effect: str
    target: str
    nonce: str
    idempotency_key: str
    expires_at: datetime
    confirmed_at: datetime | None = None


class TripWorkspace(Strict):
    id: str
    owner_id: str  # set from the verified token only
    status: WorkspaceStatus = WorkspaceStatus.CAPTURED
    version: int = 0
    timezone: str = "America/Toronto"
    facts: list[Fact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    active_plan_version: int | None = None
