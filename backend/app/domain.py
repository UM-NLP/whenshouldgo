"""Phase 0 domain contracts (docs/trip-assistant-architecture.md,
docs/agent-conversation-policy.md, docs/access-tiers.md).

Pydantic models reject unknown fields so that client input and model output can
never smuggle identity, tier or state through extra keys.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


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

    @model_validator(mode="after")
    def _inference_is_never_a_confirmed_hard_fact(self) -> Fact:
        if (
            self.source == FactSource.INFERENCE
            and self.hard
            and self.confirmation == Confirmation.CONFIRMED
        ):
            raise ValueError("an inference cannot be a confirmed hard constraint")
        return self


class Evidence(Strict):
    id: str
    tool: str
    tool_version: str
    source: str
    retrieved_at: datetime
    expires_at: datetime
    precision: str

    def is_fresh(self, now: datetime) -> bool:
        return self.expires_at > now


class WorkspacePatch(Strict):
    """Untrusted model proposal; must pass validation before it touches state."""

    workspace_id: str
    base_version: int
    next_action: NextAction
    upsert_facts: list[Fact] = Field(default_factory=list)
    remove_fact_ids: list[str] = Field(default_factory=list)
    status: WorkspaceStatus | None = None


class PendingAction(Strict):
    """Two-step side effect: shown as ``preview``, committed only on explicit confirmation.

    No expiry duration is specified in docs/; the creator must set ``expires_at``.
    """

    id: str
    workspace_id: str
    plan_version: int
    effect: str
    target: str
    preview: str
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
    goal: str | None = None
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
