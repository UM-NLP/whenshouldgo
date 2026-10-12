from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4


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


class Intent(StrEnum):
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


@dataclass(frozen=True)
class Evidence:
    id: str
    source: str
    retrieved_at: datetime
    expires_at: datetime
    claim: str

    @property
    def fresh(self) -> bool:
        return self.expires_at > datetime.now(UTC)


@dataclass(frozen=True)
class Fact:
    key: str
    value: str
    source: str
    confirmed: bool = False
    hard: bool = False
    evidence_id: str | None = None


@dataclass(frozen=True)
class PendingAction:
    id: str
    kind: str
    summary: str
    plan_version: int
    nonce: str
    expires_at: datetime
    confirmed: bool = False

    @classmethod
    def create(cls, kind: str, summary: str, plan_version: int) -> "PendingAction":
        return cls(
            id=str(uuid4()),
            kind=kind,
            summary=summary,
            plan_version=plan_version,
            nonce=str(uuid4()),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )


@dataclass
class TripWorkspace:
    id: str
    owner_id: str
    timezone: str = "America/Toronto"
    status: WorkspaceStatus = WorkspaceStatus.CAPTURED
    goal: str | None = None
    assumptions: list[Fact] = field(default_factory=list)
    constraints: list[Fact] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    plan_version: int = 0
    pending_actions: list[PendingAction] = field(default_factory=list)
